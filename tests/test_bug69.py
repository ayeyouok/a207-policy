# -*- coding: utf-8 -*-
"""BUG-69 回归测试：SingleColumnCondition 参数顺序 + OTSError 基类捕获。

背景（2026-08-22，用户实测"昨天能写、今天第 5 次写失败 NUTR_UNKNOWN"）：
1. storage._put_row_conditioned 此前写成
   `SingleColumnCondition(_REV_COL, ComparatorType.EQUAL, rev)`——SDK 6.4.8 签名
   (column_name, column_value, comparator)，错位后条件语义变成
   "_rev 列 vs 值0，比较符=rev"。rev 从 0 递增时逐档碰巧通过（0==0/1!=0/2>0/3>=0），
   rev=4 起比较符=LESS_THAN(4) → `_rev<0` 恒不成立 → 第 5 次行更新必 ConditionCheckFail。
2. except 只捕 OTSClientError，但 SDK 条件写失败抛 OTSServiceError（同属 OTSError
   基类）→ 乐观锁重试从未生效、条件冲突直接冒泡 NUTR_UNKNOWN。

本测试用 Fake client 捕获条件对象断言参数语义；并验证 OTSServiceError 条件冲突
进入重试、非冲突错误继续抛出。零 pytest 依赖（直接运行模式，CI 逐文件 python 跑）。

CI 兼容（2026-08-22 二次修复）：a207-policy 的 publish.yml 只跑 `python tests/test_*.py`、
**不安装任何依赖**（pyproject 无 tablestore）。storage.py **函数内**也有延迟
`from tablestore import ...`（_put_row_conditioned/_get_client/_save_row_locked 等），
测试真实调用这些方法时若无 SDK 仍会 ModuleNotFoundError——因此 SDK 缺失时**注入
完整 Fake tablestore 模块到 sys.modules**（等价类，含 Condition/Row/RowExistence
Expectation/UpdateType/ComparatorType/异常体系），storage 函数内 import 拿到的是
Fake 模块。验证用 `python -S`（不加载 site-packages）模拟 CI 无 SDK 环境。
"""
import json
import os
import sys

os.environ.setdefault("A207_ENV", "test")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# ---- SDK 可选导入：缺失时注入 Fake tablestore 模块（storage 函数内延迟 import 需要）----
try:
    import tablestore  # noqa: F401
    _HAS_SDK = True
except ImportError:  # pragma: no cover - 仅无 SDK 的 CI 环境
    _HAS_SDK = False
    import types as _types

    class OTSError(Exception):
        pass

    class OTSClientError(OTSError):
        def __init__(self, message, http_status=None):
            # 模拟真实 SDK：message 属性供 is_condition_conflict 读取
            super().__init__(message)
            self.message = message
            self.http_status = http_status

    class OTSServiceError(OTSError):
        def __init__(self, status, code, message, request_id):
            super().__init__(message)
            self.status = status
            self.code = code
            self.message = message

    class ComparatorType:  # 与 SDK 枚举值一致（EQUAL=0...LESS_EQUAL=5）
        EQUAL = 0
        NOT_EQUAL = 1
        GREATER_THAN = 2
        GREATER_EQUAL = 3
        LESS_THAN = 4
        LESS_EQUAL = 5

    class SingleColumnCondition:
        def __init__(self, column_name, column_value, comparator,
                     pass_if_missing=True, latest_version_only=True):
            self.column_name = column_name
            self.column_value = column_value
            self.comparator = comparator
            self.pass_if_missing = pass_if_missing
            self.latest_version_only = latest_version_only

    class RowExistenceExpectation:
        IGNORE = "IGNORE"
        EXPECT_EXIST = "EXPECT_EXIST"
        EXPECT_NOT_EXIST = "EXPECT_NOT_EXIST"

    class Condition:
        def __init__(self, row_existence_expectation, column_condition=None):
            self.row_existence_expectation = row_existence_expectation
            self.column_condition = column_condition

    class Row:
        def __init__(self, primary_key, attribute_columns=None):
            self.primary_key = primary_key
            self.attribute_columns = attribute_columns or []

    class UpdateType:
        PUT = "PUT"
        DELETE = "DELETE"
        DELETE_ALL = "DELETE_ALL"
        INCREMENT = "INCREMENT"

    class UpdateRowItem:
        def __init__(self, row, condition, return_type=None):
            self.row = row
            self.condition = condition

    class OTSClient:  # pragma: no cover - 测试注入 client，不真连
        def __init__(self, *a, **k):
            raise AssertionError("测试不应构造真实 OTSClient（注入 Fake client）")

    INF_MAX = object()
    INF_MIN = object()

    _fake = _types.ModuleType("tablestore")
    _fake.OTSError = OTSError
    _fake.OTSClientError = OTSClientError
    _fake.OTSServiceError = OTSServiceError
    _fake.ComparatorType = ComparatorType
    _fake.SingleColumnCondition = SingleColumnCondition
    _fake.RowExistenceExpectation = RowExistenceExpectation
    _fake.Condition = Condition
    _fake.Row = Row
    _fake.UpdateType = UpdateType
    _fake.UpdateRowItem = UpdateRowItem
    _fake.OTSClient = OTSClient
    _fake.INF_MAX = INF_MAX
    _fake.INF_MIN = INF_MIN
    sys.modules["tablestore"] = _fake

# 从（真或 Fake）tablestore 取符号供本测试断言使用
from tablestore import (  # noqa: E402
    ComparatorType,
    OTSClientError,
    OTSError,
    OTSServiceError,
    RowExistenceExpectation,
    SingleColumnCondition,
)

from a207_policy.storage import (  # noqa: E402
    TablestoreBase,
    _merge_row,
    is_condition_conflict,
)
from a207_policy.exceptions import ConflictError  # noqa: E402  # claim5 断言


class _FakeRow:
    def __init__(self, attrs):
        self.attribute_columns = [(k, v, 0) for k, v in attrs.items()]


class _CaptureClient:
    """Fake client：记录 put_row 收到的 Condition，get_row 返回固定行。"""

    def __init__(self, current_row=None, fail_with=None):
        self.current_row = current_row
        self.fail_with = fail_with  # 非 None：put_row 抛此异常（每次）
        self.captured_conditions = []
        self.put_calls = 0

    def get_row(self, table, pk):
        if self.current_row is None:
            return (None, None, None)
        return (None, _FakeRow(dict(self.current_row)), None)

    def put_row(self, table, row, condition):
        self.put_calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        # 成功：回写 row（模拟服务端写成功）
        if self.current_row is not None:
            for k, v in row.attribute_columns:
                self.current_row[k] = v
        else:
            self.current_row = {k: v for k, v in row.attribute_columns}
        self.captured_conditions.append(condition)

    def list_table(self):  # pragma: no cover
        return []

    def get_range(self, *a, **k):  # pragma: no cover
        return None, None, [], None


def _call_put_row_conditioned(client, rev, expect_exists):
    base = TablestoreBase(client=client)
    base._put_row_conditioned("t", [("patient_id", "P0020")],
                              {"_rev": rev + 1}, rev,
                              expect_exists=expect_exists)


def test_bug69_condition_argument_order():
    """BUG-69：SingleColumnCondition 参数顺序——column_value=rev、comparator=EQUAL。

    错位前：column_value=EQUAL(=0) + comparator=rev → rev=4 时 `_rev<0` 恒失败。
    修复后：column_value=rev + comparator=EQUAL → 语义为 `_rev == rev`。
    """
    client = _CaptureClient(current_row={"_rev": 4, "entries": "[]"})
    _call_put_row_conditioned(client, rev=4, expect_exists=True)

    # 捕获最后构造的条件对象
    cond = client.captured_conditions[-1]
    cc = cond.column_condition
    assert isinstance(cc, SingleColumnCondition)
    assert cc.column_name == "_rev"
    assert cc.column_value == 4, f"column_value 应为 rev(4)，实际 {cc.column_value!r}"
    assert cc.comparator == ComparatorType.EQUAL, \
        f"comparator 应为 EQUAL({ComparatorType.EQUAL})，实际 {cc.comparator!r}"
    print("  [ok] 条件参数顺序正确: _rev == 4 (column_value=rev, comparator=EQUAL)")


def test_bug69_otsserviceerror_conflict_retries():
    """BUG-69 第二层：OTSServiceError(ConditionCheckFail) 必须进入重试而非冒泡。

    SDK 条件写失败抛 OTSServiceError（非 OTSClientError）；修复前 except 只捕
    OTSClientError → 冲突直接冒泡（用户实测 NUTR_UNKNOWN）。修复后 except OTSError
    → 条件冲突走 is_condition_conflict=True → 重试。本测试：第一次冲突、第二次成功。
    """
    class _OnceFailClient(_CaptureClient):
        def put_row(self, table, row, condition):
            self.put_calls += 1
            if self.put_calls == 1:
                raise OTSServiceError(
                    400, "OTSConditionCheckFail",
                    "Condition check failed: _rev not match", "req-1")
            for k, v in row.attribute_columns:
                self.current_row[k] = v
            self.captured_conditions.append(condition)

    client = _OnceFailClient(current_row={"_rev": 4, "entries": "[]"})
    base = TablestoreBase(client=client)
    base._save_row_locked("t", [("patient_id", "P0020")],
                          {"entries": "[]", "total_points": 5})
    assert client.put_calls >= 2, "OTSServiceError 条件冲突应重试（至少 2 次 put）"
    assert client.current_row.get("_rev") == 5
    print(f"  [ok] OTSServiceError 条件冲突进入重试，最终 _rev={client.current_row['_rev']}")


def test_bug69_otsserviceerror_nonconflict_raises():
    """非条件冲突 OTSServiceError（如鉴权/表不存在）必须继续抛，不得当冲突重试。"""
    class _AuthFailClient(_CaptureClient):
        def put_row(self, table, row, condition):
            self.put_calls += 1
            raise OTSServiceError(403, "OTSAuthFailed", "Access denied", "req-2")

    client = _AuthFailClient(current_row={"_rev": 4})
    base = TablestoreBase(client=client)
    raised = False
    try:
        base._save_row_locked("t", [("patient_id", "P0020")], {"entries": "[]"})
    except OTSServiceError:
        raised = True
    assert raised, "非条件冲突 OTSServiceError 必须继续抛"
    assert client.put_calls == 1, "非冲突错误不得重试"
    print("  [ok] 非条件冲突 OTSServiceError 立即抛出（未误重试）")


def test_bug69_is_condition_conflict_covers_both():
    """is_condition_conflict 对 OTSClientError 与 OTSServiceError 都能判定。"""
    assert is_condition_conflict(OTSServiceError(
        400, "OTSConditionCheckFail", "not match", "r")) is True
    # OTSClientError 无 code 属性，仅 message 命中 "not match" 才判冲突
    assert is_condition_conflict(OTSClientError("row not match")) is True
    assert is_condition_conflict(OTSClientError("connection reset")) is False
    assert is_condition_conflict(OTSServiceError(
        500, "OTSServerUnavailable", "server busy", "r")) is False
    print("  [ok] is_condition_conflict 双异常类型判定正确")


def test_bug70_backfill_rev_preserves_business_columns():
    """BUG-70：历史行（无 _rev 列）补列必须用 UpdateRow 增量写，业务列不得丢。

    此前补列走 _put_row_conditioned（内部 SDK put_row = PutRow **整行覆盖写**）
    只传 {_REV_COL: 0}——PutRow 删除该行所有未在请求中出现的属性列，历史行首次
    更新即清空业务数据（entries/points 等）。修复后走 update_row（UpdateRow PUT
    语义），仅添加 _rev 列，其他列保留。
    """
    class _UpdateRowClient(_CaptureClient):
        def __init__(self):
            # 历史行：无 _rev 列，但有业务数据（2026-08-13 乐观锁上线前落库形态）
            super().__init__(current_row={
                "entries": json.dumps([{"meal": "早餐", "food": "小米粥",
                                        "amount": "400g", "date": "2026-08-21"}],
                                      ensure_ascii=False),
                "total_points": 4,
                "daily_points": 4,
                "last_points_date": "2026-08-21",
            })
            self.update_calls = 0

        def update_row(self, table, row, condition=None):
            self.update_calls += 1
            key = tuple(v for _, v in row.primary_key)
            attrs = dict(self.current_row)
            ac = row.attribute_columns
            if isinstance(ac, dict):
                # 真实 SDK UpdateRow 格式：{'PUT': [(name, value), ...], ...}
                for op_cols in ac.values():
                    for col in op_cols:
                        attrs[col[0]] = col[1]  # col = (name, value) 或 (name, value, ts)
            else:  # 兼容旧 list 三元组（理论不再出现）
                for col in ac:
                    attrs[col[0]] = col[2]
            self.current_row = attrs

        def put_row(self, table, row, condition):
            self.put_calls += 1
            # 补列后下一轮正常 CAS 写走 put_row 是合法路径（此时行已带 _rev）
            key = tuple(v for _, v in row.primary_key)
            attrs = dict(self.current_row)
            for k, v in row.attribute_columns:
                attrs[k] = v
            self.current_row = attrs

    client = _UpdateRowClient()
    base = TablestoreBase(client=client)
    base._save_row_locked(
        "t", [("patient_id", "P0020")],
        {"entries": json.dumps([{"meal": "早餐", "food": "小米粥",
                                 "amount": "400g", "date": "2026-08-21"}],
                               ensure_ascii=False),
         "total_points": 5, "daily_points": 1, "last_points_date": "2026-08-22"})

    assert client.update_calls >= 1, "历史行补列必须走 update_row"
    row = client.current_row
    assert "_rev" in row, "补列后行必须有 _rev"
    assert row["_rev"] == 1, f"_rev 应为 1（0 补列 + 1 次 CAS 递增），实际 {row['_rev']}"
    # 业务列必须保留（PutRow 覆盖写会丢光）
    assert row["total_points"] == 5, f"total_points 丢失: {row}"
    assert row["daily_points"] == 1, f"daily_points 丢失: {row}"
    assert row["last_points_date"] == "2026-08-22", f"last_points_date 丢失: {row}"
    entries = json.loads(row["entries"])
    assert entries and entries[0]["food"] == "小米粥", f"entries 丢失: {row}"
    print("  [ok] 历史行补列走 update_row，业务列全保留（total_points=5/_rev=1/entries 完好）")


def test_bug71_backfill_rev_row_format_and_condition():
    """BUG-71 + 并发安全（Issue 3）：_update_row_add_rev 必须传 dict 格式且带列条件。

    此前写为 Row(pk, [(...UpdateType.PUT, 0)])（列表）——真实 SDK update_row 内部走
    `row.attribute_columns.items()`，列表无 .items() → AttributeError 崩溃（仅历史行
    首次更新触发）。修复后应为 dict {'PUT': [('_rev', 0)]}。同时补列条件必须拒绝 _rev
    已存在（SingleColumnCondition(_rev, 0, EQUAL, pass_if_missing=True)），否则并发两
    线程同时补列会把 _rev 洗回 0（Lost Update）。
    """
    class _CaptureUpdateClient:
        def __init__(self):
            self.captured_row = None
            self.captured_condition = None

        def update_row(self, table, row, condition=None):
            self.captured_row = row
            self.captured_condition = condition

        def get_row(self, *a, **k):
            return (None, None, None)

    client = _CaptureUpdateClient()
    base = TablestoreBase(client=client)
    base._update_row_add_rev("t", [("patient_id", "P0020")])

    row = client.captured_row
    cond = client.captured_condition
    # BUG-71：attribute_columns 必须是 dict（真实 SDK 要求），不能是 list
    assert isinstance(row.attribute_columns, dict), \
        f"UpdateRow attribute_columns 必须是 dict，实际 {type(row.attribute_columns)}"
    assert row.attribute_columns == {'PUT': [('_rev', 0)]}, \
        f"UpdateRow 列格式错误: {row.attribute_columns}"
    # 行存在性条件
    assert cond.row_existence_expectation == RowExistenceExpectation.EXPECT_EXIST
    # 并发安全：拒绝 _rev 已存在（Issue 3）
    cc = cond.column_condition
    assert isinstance(cc, SingleColumnCondition), f"补列必须带列条件，实际 {cc!r}"
    assert cc.column_name == "_rev", cc.column_name
    assert cc.column_value == 0, cc.column_value
    assert cc.comparator == ComparatorType.EQUAL, cc.comparator
    assert cc.pass_if_missing is True, "缺失列须放行（首补列），已存在须拦截"
    print("  [ok] 补列 UpdateRow dict 格式正确 + _rev 存在性列条件（并发安全）")


def test_bug72_backfill_rev_deadloop_guard():
    """BUG-72：补列写回对读不可见时必须有封顶，拒绝 CPU 死循环。

    Fake client 的 update_row **不**把 _rev 写回 current_row（模拟 DB 同步延迟/
    缓存/测试 Fake 未写回），_get_row 永远读不到 _rev → 旧实现 attempts 不增死循环。
    修复后 init_attempts 独立封顶（_MAX_RETRY），超阈抛 RuntimeError。
    """
    class _NoWritebackClient(_CaptureClient):
        def update_row(self, table, row, condition=None):
            # 故意不写回 _rev：模拟补列写回对读不可见
            self.update_calls = getattr(self, "update_calls", 0) + 1

    client = _NoWritebackClient(current_row={"entries": "[]"})  # 无 _rev
    base = TablestoreBase(client=client)
    raised = False
    try:
        base._save_row_locked(
            "t", [("patient_id", "P0020")], {"entries": "[]", "total_points": 5})
    except RuntimeError as exc:
        raised = True
        assert "补列失败" in str(exc), f"应抛补列失败，实际: {exc}"
    assert raised, "补列写回不可见必须抛 RuntimeError（而非死循环）"
    # 终止性：补列次数应被封顶（不超过 _MAX_RETRY + 1），绝不无限
    assert client.update_calls <= base._MAX_RETRY + 1, \
        f"补列次数未被封顶，疑似死循环: {client.update_calls}"
    assert client.update_calls >= base._MAX_RETRY, \
        f"应耗尽补列预算后抛错，实际仅 {client.update_calls} 次"
    print(f"  [ok] 补列写回不可见时第 {client.update_calls} 次抛 RuntimeError（无死循环）")


def test_bug73_merge_row_invalid_new_json_rejected():
    """BUG-73：已注册 JSON list 字段，新值非法 JSON / 非数组必须 fail-closed 拒绝。

    旧实现只校验 cur，value 非法被 `except ... pass` 吞掉后 `merged[key]=value` 静默
    覆盖，破坏"该字段必为 JSON 数组"契约。修复后两端都校验。
    """
    from a207_policy import storage as _storage_mod

    _storage_mod._JSON_LIST_FIELDS.add("records")  # 注册（幂等）
    try:
        # 新值非法 JSON（如 "{invalid}"）→ 必须拒绝
        try:
            _merge_row({"records": '[{"id": "r1"}]'},
                       {"records": "{invalid}"})
            raise AssertionError("新值非法 JSON 应被拒绝覆盖")
        except RuntimeError:
            pass
        # 新值合法 JSON 但非数组（如 "42" / '"hello"'）→ 必须拒绝
        for bad_new in ("42", '"hello"', "{}"):
            try:
                _merge_row({"records": '[{"id": "r1"}]'},
                           {"records": bad_new})
                raise AssertionError(f"新值 {bad_new!r} 非数组应被拒绝覆盖")
            except RuntimeError:
                pass
        # 合法 list 新值仍正常合并（正例不回归）
        out = _merge_row({"records": '[{"id": "r1"}]'},
                         {"records": '[{"id": "r2"}]'})
        assert '"r1"' in out["records"] and '"r2"' in out["records"], out
    finally:
        _storage_mod._JSON_LIST_FIELDS.discard("records")
    print("  [ok] 新值非法 JSON / 非数组均被拒绝（fail-closed），合法合并不回归")


def test_claim1_cas_pass_if_missing_false():
    """外部审计 claim1：_put_row_conditioned 的 CAS 列条件必须 pass_if_missing=False
    （fail-closed）——缺 _REV_COL 的退化行不得绕过乐观锁被无条件覆写。"""
    client = _CaptureClient(current_row={"_rev": 4, "entries": "[]"})
    _call_put_row_conditioned(client, rev=4, expect_exists=True)
    cond = client.captured_conditions[-1]
    cc = cond.column_condition
    assert isinstance(cc, SingleColumnCondition)
    assert cc.pass_if_missing is False, (
        f"CAS 条件必须 pass_if_missing=False（防缺列绕过乐观锁），"
        f"实际 {cc.pass_if_missing}")
    print("  [ok] CAS 列条件 pass_if_missing=False（fail-closed，缺列即 ConditionCheckFail）")


def test_claim2_merge_row_none_skipped():
    """外部审计 claim2：_merge_row 契约"new 非 None 字段覆盖"——None 必须跳过，
    保留 current，不报错、不写 None（避免 PutRow 整行覆盖删列数据丢失）。"""
    from a207_policy import storage as _storage_mod

    _storage_mod._JSON_LIST_FIELDS.add("records")
    try:
        # 注册 JSON 字段遇 None：旧实现抛 RuntimeError 中断；修复后跳过保留 current
        out = _merge_row(
            {"records": '[{"id":"r1"}]', "name": "old"},
            {"records": None, "name": None})
        assert out["records"] == '[{"id":"r1"}]', f"None 应跳过保留 current: {out}"
        assert out["name"] == "old", f"普通字段 None 应跳过保留 current: {out}"
        # 非 None 正常覆盖（正例不回归）
        out2 = _merge_row({"name": "old"}, {"name": "new"})
        assert out2["name"] == "new", out2
    finally:
        _storage_mod._JSON_LIST_FIELDS.discard("records")
    print("  [ok] _merge_row 遇 None 跳过（保留 current，不抛错不写 None）")


def test_claim4_condition_conflict_lowercase_space():
    """外部审计 claim4：is_condition_conflict 必须命中真实服务端消息
    "Condition check failed."（小写 + 空格），且不误判非冲突错误。"""
    # 真实 Tablestore 服务端条件冲突消息（小写 + 空格），code 可能为空
    assert is_condition_conflict(OTSClientError("Condition check failed.")) is True
    # 仅 message 携带冲突（code 为空）也须命中
    assert is_condition_conflict(OTSServiceError(
        None, "", "Condition check failed.", "r")) is True
    # 非冲突不得误判
    assert is_condition_conflict(OTSClientError("connection reset")) is False
    assert is_condition_conflict(OTSServiceError(
        403, "OTSAuthFailed", "Access denied", "r")) is False
    print("  [ok] is_condition_conflict 命中 \"condition check failed.\"（小写+空格），非冲突不误判")


def test_claim5_masked_pk_in_exceptions():
    """外部审计 claim5：_save_row_locked 异常消息必须脱敏 pk（_mask_pk），
    不得落明文 patient_id（医疗合规）。覆盖并发冲突与补列失败两条路径。"""
    # 路径1：并发写冲突 → ConflictError（pk 脱敏）
    class _AlwaysConflictClient(_CaptureClient):
        def put_row(self, table, row, condition):
            self.put_calls += 1
            raise OTSServiceError(
                400, "OTSConditionCheckFail", "Condition check failed", "r")

    c1 = _AlwaysConflictClient(current_row={"_rev": 4, "entries": "[]"})
    base = TablestoreBase(client=c1)
    raised = False
    try:
        base._save_row_locked(
            "t", [("patient_id", "P0020")], {"entries": "[]"})
    except ConflictError as exc:
        raised = True
        msg = str(exc)
        assert "P0020" not in msg, f"明文 patient_id 泄漏: {msg}"
        assert "P002***" in msg, f"未脱敏: {msg}"
    assert raised, "并发冲突应抛 ConflictError"

    # 路径2：历史行补列写回不可见 → RuntimeError 补列失败（pk 脱敏）
    class _NoWritebackClient2(_CaptureClient):
        def update_row(self, table, row, condition=None):
            self.update_calls = getattr(self, "update_calls", 0) + 1

    c2 = _NoWritebackClient2(current_row={"entries": "[]"})  # 无 _rev
    base2 = TablestoreBase(client=c2)
    raised2 = False
    try:
        base2._save_row_locked(
            "t", [("patient_id", "P0020")], {"entries": "[]"})
    except RuntimeError as exc:
        raised2 = True
        msg = str(exc)
        assert "补列失败" in msg
        assert "P0020" not in msg, f"明文 patient_id 泄漏: {msg}"
        assert "P002***" in msg, f"未脱敏: {msg}"
    assert raised2, "补列写回不可见应抛 RuntimeError"
    print("  [ok] 异常消息 pk 已脱敏（ConflictError + 补列失败均掩码，无明文泄漏）")


def test_claim1_empty_attribute_row_writable():
    """外部审计 claim1：既有行但属性列为空（current={}）必须可正常写入。

    旧实现 `_save_row_locked` 用 `if current:` 判定——空字典 `{}` truthy 为 False，
    跳过 _merge_row 更跳过 `if _REV_COL not in current:` 补列分支 → 行无 _rev 列却走
    `_rev==0` CAS（pass_if_missing=False）→ ConditionCheckFail → 3 次重试全败误报
    ConflictError（无法写入）。修复后 `if current is not None:` 对空字典判 True，
    正常补列 _rev 后 CAS 写成功。
    """
    class _EmptyRowClient(_CaptureClient):
        def update_row(self, table, row, condition=None):
            # 补列成功：把 _rev=0 写回（模拟服务端 UpdateRow 生效），后续 CAS 可读到 _rev
            ac = row.attribute_columns
            if isinstance(ac, dict):
                for op_cols in ac.values():
                    for col in op_cols:
                        self.current_row[col[0]] = col[1]
            else:
                for col in ac:
                    self.current_row[col[0]] = col[2]

    # 既有行但属性列为空（current_row={} → _get_row 返回 {}，非 None）
    client = _EmptyRowClient(current_row={})
    base = TablestoreBase(client=client)
    base._save_row_locked(
        "t", [("patient_id", "P0020")],
        {"entries": "[]", "total_points": 5})
    row = client.current_row
    assert "_rev" in row, "空属性行必须补列 _rev"
    assert row["_rev"] == 1, f"空属性行 CAS 后 _rev 应为 1，实际 {row.get('_rev')}"
    assert row["total_points"] == 5, f"业务列丢失: {row}"
    print("  [ok] 既有空属性行可正常补列+CAS 写入（无死锁误报冲突）")


def test_claim2_merge_row_dirty_nonstr_old_rejected():
    """外部审计 claim2：注册 JSON list 字段，旧值为非 str 脏数据（int/bool/bytes）
    必须 fail-closed 拒绝合并，不得静默用新值覆盖损坏数据；仅 cur_value is None
    （首次写该字段）才允许直接赋新值。"""
    from a207_policy import storage as _storage_mod

    _storage_mod._JSON_LIST_FIELDS.add("records")
    try:
        # 旧值 int（非 str 脏数据）→ 必须拒绝
        try:
            _merge_row({"records": 123}, {"records": '[{"id":"r2"}]'})
            raise AssertionError("旧值 int 应被拒绝覆盖")
        except RuntimeError:
            pass
        # 旧值 bool（非 str 脏数据）→ 必须拒绝
        try:
            _merge_row({"records": True}, {"records": '[{"id":"r2"}]'})
            raise AssertionError("旧值 bool 应被拒绝覆盖")
        except RuntimeError:
            pass
        # 首次写（cur_value is None）→ 允许直接用新值
        out = _merge_row({}, {"records": '[{"id":"r2"}]'})
        assert out["records"] == '[{"id":"r2"}]', out
        # 合法 str 旧值仍正常合并（不回归）
        out2 = _merge_row({"records": '[{"id":"r1"}]'},
                          {"records": '[{"id":"r2"}]'})
        assert '"r1"' in out2["records"] and '"r2"' in out2["records"], out2
    finally:
        _storage_mod._JSON_LIST_FIELDS.discard("records")
    print("  [ok] 旧值非 str 脏数据拒绝覆盖（fail-closed）；首次写/合法合并不回归")


def main():
    print("BUG-69/70/71/72/73 回归 + 外部审计 claim1/2/4/5 + claim1/2(续) 修复验证")
    test_bug69_condition_argument_order()
    test_bug69_otsserviceerror_conflict_retries()
    test_bug69_otsserviceerror_nonconflict_raises()
    test_bug69_is_condition_conflict_covers_both()
    test_bug70_backfill_rev_preserves_business_columns()
    test_bug71_backfill_rev_row_format_and_condition()
    test_bug72_backfill_rev_deadloop_guard()
    test_bug73_merge_row_invalid_new_json_rejected()
    test_claim1_cas_pass_if_missing_false()
    test_claim2_merge_row_none_skipped()
    test_claim4_condition_conflict_lowercase_space()
    test_claim5_masked_pk_in_exceptions()
    test_claim1_empty_attribute_row_writable()
    test_claim2_merge_row_dirty_nonstr_old_rejected()
    print("BUG69/70/71/72/73 + claim1/2/4/5 + claim1/2(续) OK（14 用例）")


if __name__ == "__main__":
    main()
