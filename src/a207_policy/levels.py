"""儿童 CKD 风险等级严重度排序（单一事实源）。

等级语义（与 CKDNutri-assessment-mcp/rules.json 对齐）：L1=最严重（危急），
L2=中等，L3=最轻（稳定/观察）。数值越大越严重。risk_escalation 事件必须
从更轻等级升到更重等级（to_level 严重度 > from_level），否则为非法降级。

2026-08-21：从 CKDNutri-assessment-mcp.core._LEVEL_RANK 上移共享——assessment /
care 两个域包都需要等级排序（assessment 做规则匹配/趋势对比，care 做
risk_escalation 方向校验），此前各自定义会漂移（实测出现"L2→L3 被当升级"，
根因即 care 漏校验 + 无共享排序）。
"""
from __future__ import annotations

# 风险等级严重度排序（唯一事实源）：值越大越严重。
# L1=3 危急 / L2=2 中等 / L3=1 最轻 / none=0 无。
RISK_LEVEL_RANK: dict[str, int] = {"L1": 3, "L2": 2, "L3": 1, "none": 0}

_VALID_LEVELS = frozenset(RISK_LEVEL_RANK.keys())


def is_valid_escalation(from_level: str, to_level: str) -> bool:
    """risk_escalation 事件语义校验：to_level 必须比 from_level 更严重（L1>L2>L3）。

    返回 True=合法升级；False=降级/持平（非法）。非法等级串抛 ValueError
    （fail-closed：不静默把未知等级当最低档）。
    """
    a = RISK_LEVEL_RANK.get(from_level)
    b = RISK_LEVEL_RANK.get(to_level)
    if a is None or b is None:
        raise ValueError(f"非法风险等级: from={from_level!r} to={to_level!r}")
    return b > a
