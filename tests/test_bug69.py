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
"""
import json
import os
import sys

os.environ.setdefault("A207_ENV", "test")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

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
    from tablestore import ComparatorType, SingleColumnCondition

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
    from tablestore import OTSServiceError

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
    from tablestore import OTSServiceError

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
    from tablestore import OTSClientError, OTSServiceError

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
    from tablestore import OTSError  # noqa: F401  (仅确保 SDK 可用)

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
