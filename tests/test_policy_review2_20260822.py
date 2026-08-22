"""a207-policy 策略核心复核（第二轮，2026-08-22）回归测试。

覆盖本轮 4 条「属实·已修」claim 的防回归：
- Claim 1：NUTRITION_ASSESSMENT_DATA_TOOLS 不再混入写工具 upsert_food_diary
- Claim 4：is_valid_escalation 对 None/""/大小写 归一为 "none"（初始无状态可升级）
- Claim 5：atomic_write_json 继承原权限 / 新建 0644（避免多容器共享卷读被锁死）
- Claim 6：_enforce 跨 MCP 误调用细化错误归因（路由错误 vs 权限不足）

运行：python tests/test_policy_review2_20260822.py
不依赖 pytest / fastmcp；遵循 a207-policy 测试纪律（零外部依赖、可直接跑）。
"""

from __future__ import annotations

import os

os.environ.setdefault("A207_ENV", "test")            # N-SEC-1：测试进程显式声明测试环境
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")  # 生产护栏：确认 json 后端为开发模式
import stat
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from a207_policy import (
    NUTRITION_ASSESSMENT_DATA_TOOLS,
    as_caller,
    enforce_nutrition_tool,
    enforce_write,
    is_valid_escalation,
)
from a207_policy.exceptions import PermissionDenied
from a207_policy.state import atomic_write_json


def test_claim1_data_tools_no_write_tool():
    """Claim 1：NUTRITION_ASSESSMENT_DATA_TOOLS 仅含只读工具，不得混入 upsert_food_diary。

    upsert_food_diary 是写工具，走 gate.enforce_nutrition_tool 专用写分支拦截；
    若被误列入"数据读工具集合"，外部枚举读工具会拿到写工具且分支顺序变动会误报文案。
    """
    assert "upsert_food_diary" not in NUTRITION_ASSESSMENT_DATA_TOOLS, \
        "NUTRITION_ASSESSMENT_DATA_TOOLS 不应含写工具 upsert_food_diary"
    assert NUTRITION_ASSESSMENT_DATA_TOOLS == frozenset({"get_food_diary_summary"}), \
        f"NUTRITION_ASSESSMENT_DATA_TOOLS 应仅含 get_food_diary_summary，实际 {NUTRITION_ASSESSMENT_DATA_TOOLS}"
    print("  [ok] Claim1 NUTRITION_ASSESSMENT_DATA_TOOLS 仅含只读工具（无 upsert_food_diary）")


def test_claim4_escalation_none_tolerant():
    """Claim 4：初始无状态（None/""/大小写）可正常升级到 L1/L2/L3，不抛 ValueError。"""
    # 初始无状态 → 升级
    assert is_valid_escalation(None, "L1") is True, "none→L1 应合法"
    assert is_valid_escalation("", "L2") is True, "''→L2 应合法"
    assert is_valid_escalation("none", "L1") is True, "none→L1 应合法"
    assert is_valid_escalation("NONE", "L3") is True, "NONE(大写)→L3 应合法"
    # 常规升级仍正确
    assert is_valid_escalation("L3", "L1") is True, "L3→L1 应合法"
    assert is_valid_escalation("L2", "L1") is True, "L2→L1 应合法"
    # 降级/持平仍判非法
    assert is_valid_escalation("L1", "L3") is False, "L1→L3 应判降级(非法)"
    assert is_valid_escalation("L1", "L1") is False, "持平应判非法"
    # 未知等级仍 fail-closed 抛错
    try:
        is_valid_escalation("L9", "L1")
        raise AssertionError("未知等级 L9 应抛 ValueError")
    except ValueError:
        pass
    print("  [ok] Claim4 is_valid_escalation 容错 None/''/大小写，未知等级仍 fail-closed")


def test_claim5_atomic_write_preserves_mode():
    """Claim 5：atomic_write_json 继承原文件权限；新建文件给 0644（多容器共享卷可读）。"""
    d = Path(tempfile.mkdtemp(prefix="a207_atomic_mode_"))
    try:
        # 1) 已存在文件：继承原权限（此处原权限 0644）
        p = d / "existing.json"
        p.write_text("{}", encoding="utf-8")
        p.chmod(0o644)
        atomic_write_json(p, {"x": 1})
        if os.name == "posix":
            assert stat.S_IMODE(p.stat().st_mode) == 0o644, \
                f"已存在文件应继承 0644，实际 {oct(stat.S_IMODE(p.stat().st_mode))}"

        # 2) 新建文件：默认 0644（非 mkstemp 的 0600 锁死）
        p2 = d / "new.json"
        atomic_write_json(p2, {"y": 2})
        assert p2.exists(), "新建文件应被写出"
        if os.name == "posix":
            assert stat.S_IMODE(p2.stat().st_mode) == 0o644, \
                f"新建文件应 0644，实际 {oct(stat.S_IMODE(p2.stat().st_mode))}"
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)
    print("  [ok] Claim5 atomic_write_json 继承原权限/新建 0644（POSIX 模式已校验，Windows 跳过）")


def test_claim6_enforce_wrong_mcp_message():
    """Claim 6：跨 MCP 误调用写工具时错误归因区分「路由错误」与「权限不足」。"""
    # doctor 对 upsert_food_diary 有权限（nutrition-mcp），但在 care-mcp 上调用 → 路由错误
    with as_caller("doctor_assistant"):
        try:
            enforce_write("CKDNutri-care-mcp", "upsert_food_diary")
            raise AssertionError("跨 MCP 误调用应被拒绝")
        except PermissionDenied as exc:
            detail = exc.reason
            assert "CKDNutri-nutrition-mcp" in detail, \
                f"路由错误应点明工具所属 MCP，reason={detail!r}"
            assert "CKDNutri-care-mcp" in detail, \
                f"路由错误应点明请求 MCP，reason={detail!r}"
            assert detail.startswith("写工具 upsert_food_diary 属于"), \
                f"路由错误不应是 MX-3 权限文案，reason={detail!r}"

    # parent 在正确 MCP(care) 调用仅 doctor 可写的 schedule_followup → 权限不足（MX-3）
    with as_caller("parent_assistant"):
        try:
            enforce_write("CKDNutri-care-mcp", "schedule_followup")
            raise AssertionError("无权限写工具应被拒绝")
        except PermissionDenied as exc:
            assert "MX-3 写权受限" in exc.reason, \
                f"权限不足应报 MX-3，reason={exc.reason!r}"
    print("  [ok] Claim6 跨 MCP 误调用错误归因区分路由错误/MX-3 权限不足")


def main():
    test_claim1_data_tools_no_write_tool()
    test_claim4_escalation_none_tolerant()
    test_claim5_atomic_write_preserves_mode()
    test_claim6_enforce_wrong_mcp_message()
    print("\nALL POLICY REVIEW-2 (2026-08-22) REGRESSION TESTS PASSED")


if __name__ == "__main__":
    main()
