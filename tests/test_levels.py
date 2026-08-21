"""风险等级严重度排序单一事实源（a207_policy.levels）回归测试。

BUG-66（2026-08-21）：care 的 risk_escalation 方向校验依赖 RISK_LEVEL_RANK
（L1>L2>L3）。此前 assessment 本地定义 + care 漏校验，实测出现"L2→L3 被当升级"。
本测试钉死共享排序与方向语义，防回归。
"""
import pytest

from a207_policy import RISK_LEVEL_RANK, is_valid_escalation


def test_rank_semantics_l1_most_severe():
    # 等级严重度唯一事实源：L1=3（危急）> L2=2 > L3=1 > none=0
    assert RISK_LEVEL_RANK["L1"] == 3
    assert RISK_LEVEL_RANK["L2"] == 2
    assert RISK_LEVEL_RANK["L3"] == 1
    assert RISK_LEVEL_RANK["none"] == 0
    assert RISK_LEVEL_RANK["L1"] > RISK_LEVEL_RANK["L2"] > RISK_LEVEL_RANK["L3"]


def test_escalation_direction_true():
    # 更轻 → 更重：合法升级
    assert is_valid_escalation("L3", "L1") is True
    assert is_valid_escalation("L3", "L2") is True
    assert is_valid_escalation("L2", "L1") is True


def test_downgrade_rejected():
    # 更重 → 更轻：非法降级（BUG-66 回归点：L2→L3 必须 False）
    assert is_valid_escalation("L2", "L3") is False
    assert is_valid_escalation("L1", "L3") is False
    assert is_valid_escalation("L1", "L2") is False


def test_equal_level_rejected():
    assert is_valid_escalation("L1", "L1") is False
    assert is_valid_escalation("L3", "L3") is False


def test_invalid_level_raises():
    # 未知等级 fail-closed：抛 ValueError 而非静默当最低档
    with pytest.raises(ValueError):
        is_valid_escalation("L9", "L1")
    with pytest.raises(ValueError):
        is_valid_escalation("L3", "XX")
