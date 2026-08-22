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
    SingleColumnCondition,
)

from a207_policy.storage import (  # noqa: E402
    TablestoreBase,
    is_condition_conflict,
)


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
            for col in row.attribute_columns:
                # 三元组 (name, op, value)
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


def main():
    print("BUG-69 回归（storage 条件写参数顺序 + OTSError 捕获）+ BUG-70（补列 UpdateRow）")
    test_bug69_condition_argument_order()
    test_bug69_otsserviceerror_conflict_retries()
    test_bug69_otsserviceerror_nonconflict_raises()
    test_bug69_is_condition_conflict_covers_both()
    test_bug70_backfill_rev_preserves_business_columns()
    print("BUG69/70 OK（5 用例）")


if __name__ == "__main__":
    main()
