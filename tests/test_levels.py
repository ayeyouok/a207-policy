"""风险等级严重度排序单一事实源（a207_policy.levels）回归测试。

BUG-66（2026-08-21）：care 的 risk_escalation 方向校验依赖 RISK_LEVEL_RANK
（L1>L2>L3）。此前 assessment 本地定义 + care 漏校验，实测出现"L2→L3 被当升级"。

纯 python 直接运行（CI 逐文件 `python tests/test_*.py`，不依赖 pytest）：
    python tests/test_levels.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from a207_policy import RISK_LEVEL_RANK, is_valid_escalation  # noqa: E402


def test_rank_semantics_l1_most_severe() -> None:
    # 等级严重度唯一事实源：L1=3（危急）> L2=2 > L3=1 > none=0
    assert RISK_LEVEL_RANK["L1"] == 3
    assert RISK_LEVEL_RANK["L2"] == 2
    assert RISK_LEVEL_RANK["L3"] == 1
    assert RISK_LEVEL_RANK["none"] == 0
    assert RISK_LEVEL_RANK["L1"] > RISK_LEVEL_RANK["L2"] > RISK_LEVEL_RANK["L3"]


def test_escalation_direction_true() -> None:
    # 更轻 → 更重：合法升级
    assert is_valid_escalation("L3", "L1") is True
    assert is_valid_escalation("L3", "L2") is True
    assert is_valid_escalation("L2", "L1") is True


def test_downgrade_rejected() -> None:
    # 更重 → 更轻：非法降级（BUG-66 回归点：L2→L3 必须 False）
    assert is_valid_escalation("L2", "L3") is False
    assert is_valid_escalation("L1", "L3") is False
    assert is_valid_escalation("L1", "L2") is False


def test_equal_level_rejected() -> None:
    assert is_valid_escalation("L1", "L1") is False
    assert is_valid_escalation("L3", "L3") is False


def test_invalid_level_raises() -> None:
    # 未知等级 fail-closed：抛 ValueError 而非静默当最低档
    for bad_from, bad_to in (("L9", "L1"), ("L3", "XX")):
        try:
            is_valid_escalation(bad_from, bad_to)
        except ValueError:
            continue
        raise AssertionError(
            f"is_valid_escalation({bad_from!r}, {bad_to!r}) 应抛 ValueError")


def main() -> int:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"LEVELS OK（{len(fns)} 个用例）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
