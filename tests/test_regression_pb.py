"""P-B1~P-B6 回归测试（2026-08-14 修复后固化）。pytest + 直接运行双模式。"""
import os

os.environ.setdefault("A207_ENV", "test")  # N-SEC-1（2026-08-14）：测试进程显式声明测试环境（守卫 fail-closed 默认拒绝）
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")  # 生产护栏（2026-08-15）：测试进程显式确认 json 后端为开发模式
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def test_pb1_forged_caller_rejected():
    """P-B1：enforce_nutrition_tool caller 参数与进程身份不一致 → 拒绝（防伪造）。"""
    from a207_policy import as_caller, exceptions, gate

    with as_caller("doctor_assistant"):
        # 伪造身份（parent 冒充 doctor 跑临床工具）
        try:
            gate.enforce_nutrition_tool("doctor_assistant", "calc_prnt_targets")
        except exceptions.PermissionDenied:
            raise AssertionError("真实身份不应被拒") from None
        try:
            gate.enforce_nutrition_tool("parent_assistant", "calc_prnt_targets")
        except exceptions.PermissionDenied:
            pass
        else:
            raise AssertionError("伪造 caller 应被拒绝")


def test_pb2_enforce_write_read_rejected():
    """P-B2：enforce_write('read') 拒绝（保留字不得静默降级为读检查）。"""
    from a207_policy import as_caller, exceptions, gate

    with as_caller("doctor_assistant"):
        try:
            gate.enforce_write("CKDNutri-care-mcp", "read")
        except exceptions.PermissionDenied:
            pass
        else:
            raise AssertionError("enforce_write('read') 应被拒绝")


def test_pb3_retired_tool_rejected():
    """P-B3：push_to_emr 中英文一律拒绝（退役工具，结论一致）。"""
    from a207_policy import as_caller, exceptions, gate

    with as_caller("doctor_assistant"):
        for action in ("push_to_emr", "写回病历", "写入病历"):
            try:
                gate.enforce_write("CKDNutri-content-mcp", action)
            except exceptions.PermissionDenied:
                continue
            raise AssertionError(f"退役工具 {action} 被放行")


def test_pb4_write_action_word_boundary():
    """P-B4：is_write_action 词边界（dialog_analysis 非写，log_search 是写）。"""
    from a207_policy import gate

    assert gate.is_write_action("dialog_analysis") is False
    assert gate.is_write_action("log_search") is True
    assert gate.is_write_action("upsert_food_diary") is True


def test_pb6_non_string_token_fail_closed():
    """P-B6：非字符串 guardian_token 返回 False 不抛 TypeError。"""
    from a207_policy import gate

    for bad in (None, 12345, ["a"], 3.14):
        assert gate.verify_guardian_token("P0001", bad) is False, bad


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"POLICY P-B1~P-B6 REGRESSION OK（{len(fns)} 个用例）")
