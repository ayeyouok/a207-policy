"""十八审（2026-08-18）a207-policy 回归：storage._merge_row 单调字段合并（care P1-3 联动）。

覆盖：
- 未注册字段行为不变（标量 new 优先）
- 注册 workflow_status 后：stale 低阶状态不覆盖最新高阶状态（防状态机回退）
- 高阶推进合法（new 序 ≥ current 序时覆盖）
- 同值幂等 / 未知状态回退默认行为
- JSON 列表合并不受单调字段影响

运行：pytest tests/test_regression_pb2.py（或 python tests/test_regression_pb2.py 直接运行，
CI 的 publish workflow 逐文件 `python tests/test_*.py`，**不安装 pytest**——本文件不依赖 pytest）。
"""
import os

os.environ.setdefault("A207_ENV", "test")
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
for p in (_SRC,):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from a207_policy.storage import _merge_row, register_monotonic_field

_WS_ORDER = {"unacked": 0, "confirmed": 1, "resolved": 2, "closed": 3}


def _run_all() -> None:
    """直接运行模式：执行本文件全部 test_* 函数（CI 的 `python tests/test_*.py`）。"""
    import sys as _sys

    for _name, _fn in sorted(vars(_sys.modules[__name__]).items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("PB2 REGRESSION OK")


def test_merge_row_plain_scalar_new_wins():
    """未注册字段行为不变：标量 new 优先（回归基线）。"""
    out = _merge_row({"title": "旧", "status": "a"}, {"status": "b"})
    assert out["status"] == "b" and out["title"] == "旧"


def test_merge_row_workflow_status_no_regression():
    """未注册时 workflow_status 保持 new 优先（默认行为基线，防测试间污染）。"""
    # 保证测试确定性：先清空注册再验证默认行为，随后注册
    from a207_policy import storage
    storage._MONOTONIC_FIELDS.pop("workflow_status", None)
    out = _merge_row({"workflow_status": "resolved"}, {"workflow_status": "confirmed"})
    assert out["workflow_status"] == "confirmed"  # 默认 new 优先（无保护时）
    register_monotonic_field("workflow_status", _WS_ORDER)


def test_merge_row_stale_low_state_keeps_high():
    """P1-3 核心：current=resolved + stale new=confirmed → 保留 resolved（状态机不回退）。"""
    register_monotonic_field("workflow_status", _WS_ORDER)
    out = _merge_row({"workflow_status": "resolved", "title": "旧"},
                     {"workflow_status": "confirmed", "status_updated_by": "A"})
    assert out["workflow_status"] == "resolved", out  # 低阶不覆盖高阶
    assert out["status_updated_by"] == "A", out  # 其它字段仍正常合并


def test_merge_row_monotonic_advance_allowed():
    """高阶推进合法：current=confirmed + new=resolved → resolved（正向流转不受阻）。"""
    register_monotonic_field("workflow_status", _WS_ORDER)
    out = _merge_row({"workflow_status": "confirmed"}, {"workflow_status": "resolved"})
    assert out["workflow_status"] == "resolved"
    # 同值幂等
    out2 = _merge_row({"workflow_status": "resolved"}, {"workflow_status": "resolved"})
    assert out2["workflow_status"] == "resolved"


def test_merge_row_unknown_state_defaults():
    """未知状态值（未登记）回退默认 new 优先（异常数据不阻塞写入，保持简单）。"""
    register_monotonic_field("workflow_status", _WS_ORDER)
    out = _merge_row({"workflow_status": "weird_x"}, {"workflow_status": "confirmed"})
    assert out["workflow_status"] == "confirmed"  # current 未知 → new 覆盖


def test_merge_row_list_merge_unaffected():
    """JSON 列表合并不受单调字段影响（care escalated_history 等仍按 id 去重合并）。"""
    register_monotonic_field("workflow_status", _WS_ORDER)
    import json
    cur = {"workflow_status": "resolved",
           "escalated_history": json.dumps([{"id": "h1", "at": "2026-08-01"}])}
    new = {"workflow_status": "confirmed",
           "escalated_history": json.dumps([{"id": "h2", "at": "2026-08-02"}])}
    out = _merge_row(cur, new)
    hist = json.loads(out["escalated_history"])
    assert {h["id"] for h in hist} == {"h1", "h2"}, hist  # 列表仍合并
    assert out["workflow_status"] == "resolved", out  # 状态仍单调


def test_merge_row_corrupt_json_list_rejected():
    """care BUG-4（2026-08-19）：已注册 JSON list 字段旧值损坏/非 list → 拒绝覆盖。

    此前 _merge_row 捕获 JSONDecodeError 后用新值静默覆盖（损坏数据无声消失）；
    注册 JSON list 字段后：旧值非法 JSON / 解析成功但非 list（{}、123、null）一律
    抛 RuntimeError（fail-closed，交由运维修复存储行），不得静默淹没。
    """
    from a207_policy import storage as _storage_mod

    _storage_mod._JSON_LIST_FIELDS.add("records")  # 注册（幂等）
    try:
        for bad_old in ("{broken", "{}", "123", "null", '"hello"'):
            try:
                _merge_row({"records": bad_old}, {"records": '[{"id": "r1"}]'})
            except RuntimeError:
                continue
            raise AssertionError(f"旧值 {bad_old!r} 损坏/非 list 应拒绝覆盖")
        # 合法 list 旧值仍正常合并
        out = _merge_row({"records": '[{"id": "r1"}]'},
                         {"records": '[{"id": "r2"}]'})
        assert '"r1"' in out["records"] and '"r2"' in out["records"], out
    finally:
        _storage_mod._JSON_LIST_FIELDS.discard("records")


if __name__ == "__main__":
    _run_all()
