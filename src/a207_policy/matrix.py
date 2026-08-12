"""权限矩阵 / 写权策略 / 各包放行集合 —— A207 唯一事实源（Plan A 收口位置）。

v2.3 阶段 0（2026-08-11）：角色从 6 瘦身为 2 + 1 管线身份。
  - Agent 身份（2）：doctor_assistant（CKD 临床助手）、parent_assistant（CKD 家庭助手）
  - 管线身份（1）：risk_warning（仅用于 notify 写权与自动化管线，非 Agent）
  - 退役：orchestrator / nutritionist / child_companion（3 角色）
  - 退役包：M11（a207-gamification-mcp）→ 打卡并入 M3 upsert_food_diary
  - 网关退出 MCP 统计：M13（a207-router-mcp）→ P0 危急词回归 HTTP 中间件（原型暂保留 MCP 形态）
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping, TypedDict, cast

# ---------------------------------------------------------------- 角色与 MCP

CALLERS: tuple[str, ...] = (
    "doctor_assistant",
    "parent_assistant",
    "risk_warning",             # 管线身份（非 Agent，仅 WRITE_TOOL_POLICY notify_* / 风险管线）
)

MCP_REGISTRY: dict[str, str] = {
    "CKDNutri-clinical-data-mcp": "P1",
    "CKDNutri-nutrition-mcp": "P2",
    "CKDNutri-care-mcp": "P3",
    "CKDNutri-assessment-mcp": "P4",
    "CKDNutri-content-mcp": "P5",
}

CLINICAL_CALC_MCP = "CKDNutri-assessment-mcp"
# 注：calc_growth_zscore / calc_prnt_targets 等营养计算工具实际归属 P2（CKDNutri-nutrition-mcp），
# 而非 P4（assessment）。CLINICAL_CALC_MCP 仅指向 P4 的 eGFR/分期纯计算；生长发育 Z 评分等
# 营养域工具的路由应以 NUTRITION_ASSESSMENT_CLINICAL_TOOLS 归属表为准（均归 P2）。

# 包名别名归一（OD-002）：登记表沿用早期命名，实际交付目录/PyPI 包名已变更。
# 任何曾经出现过的 a207-* 废弃包名都在此全量归一，避免上游（旧版路由/编排/调用方）
# 传入旧名时 normalize_mcp 原样返回 → 上层校验 PERMISSION_MATRIX 误报「mcp 未登记」。
# 注意：a207-router-mcp / a207-gateway-mcp 是网关/中间件、不是数据域 MCP，
#       故意不在此表 → 它们正确落回「未登记」。
MCP_ALIASES: dict[str, str] = {
    # --- P1 临床数据（HIS + LIS 合并）---
    "a207-clinical-data-mcp": "CKDNutri-clinical-data-mcp",
    "a207-his-mcp": "CKDNutri-clinical-data-mcp",
    "a207-lis-mcp": "CKDNutri-clinical-data-mcp",
    # --- P2 营养域（营养评估 + 食物养分 + 食谱 + 游戏化并入）---
    "a207-nutrition-mcp": "CKDNutri-nutrition-mcp",
    "a207-nutrition-assessment-mcp": "CKDNutri-nutrition-mcp",
    "a207-nutrition-assessment-mcp-nfyy": "CKDNutri-nutrition-mcp",
    "a207-nutrition-calc-mcp": "CKDNutri-nutrition-mcp",
    "a207-meal-plan-mcp": "CKDNutri-nutrition-mcp",
    # 注：a207-gamification-mcp（M11）已整体退役，不再映射到 P2 —— 彻底阻断旧流量
    # （旧打卡调用应改用 CKDNutri-nutrition-mcp 的 upsert_food_diary；传旧名落回「未登记」fail-closed）
    # --- P3 随访沟通（随访 + 通知合并）---
    "a207-care-mcp": "CKDNutri-care-mcp",
    "a207-followup-mcp": "CKDNutri-care-mcp",
    "a207-notification-mcp": "CKDNutri-care-mcp",
    "a207-notify-mcp": "CKDNutri-care-mcp",
    # --- P4 决策计算（临床计算 + 风险引擎合并）---
    "a207-decision-mcp": "CKDNutri-assessment-mcp",
    "a207-clinical-calc-mcp": "CKDNutri-assessment-mcp",
    "a207-ckd-clinical-calc-mcp": "CKDNutri-assessment-mcp",
    "a207-risk-rules-mcp": "CKDNutri-assessment-mcp",
    # --- P5 内容输出（报告 + 知识库合并）---
    "a207-content-mcp": "CKDNutri-content-mcp",
    "a207-report-mcp": "CKDNutri-content-mcp",
    "a207-knowledge-mcp": "CKDNutri-content-mcp",
}


#: 归一查找总表（别名 + 规范名小写索引合并，O(1) 单次命中）
#: 键空间无冲突：MCP_ALIASES 键均为 a207-* 前缀，规范名索引键均为 ckdnutri-* 前缀。
_MCP_LOOKUP: Mapping[str, str] = MappingProxyType({
    **{k.lower(): v for k, v in MCP_ALIASES.items()},
    **{canonical.lower(): canonical for canonical in MCP_REGISTRY},
})


def normalize_mcp(name: str) -> str:
    """把 MCP 名称归一为登记表内的键。

    处理多级格式变异（大小写/动作后缀/协议前缀），未知名返回空串供上层安全 fail-closed。

    输入示例：
      - "CKDNutri-nutrition-mcp"           → 规范名
      - "CKDNutri-nutrition-mcp:read"      → 剥离 :read 后缀后归一
      - "a207-NUTRITION-CALC-mcp:write"    → 大小写容错 + 动作剥离 + 别名映射
      - "mcp://CKDNutri-care-mcp:execute"  → 剥离协议前缀
      - None / 123 / {}                    → 空串（类型防御，fail-closed）
      - "a207-gamification-mcp"            → 空串（M11 已退役不映射，严格 fail-closed 拒绝）
    """
    if not isinstance(name, str):
        return ""
    key = name.strip()
    if not key:
        return ""
    # 1. 剥离协议前缀与 :action / 路径后缀
    if key.startswith("mcp://"):
        key = key[6:]
    key = key.split(":")[0].split("/")[0].strip()
    if not key:
        return ""

    lower = key.lower()
    # 2. 查归一总表（O(1)，大小写不敏感；别名与规范名同表命中）
    if lower in _MCP_LOOKUP:
        return _MCP_LOOKUP[lower]
    return ""  # 未知名返回空串（严格 fail-closed，契约见 docstring）


# MX-1：分期类问题不由家庭助手判定，改读 M1 已确诊分期
MX1_BLOCKED_CALLERS: frozenset[str] = frozenset({"parent_assistant"})

# ---------------------------------------------------------------- MX-1 字段可见性边界
CLINICIAN_ONLY_FIELDS: frozenset[str] = frozenset({
    "note_to_clinician", "clinician_note", "doctor_note", "doctor_notes", "internal_note", "note_to_doctor",
    "emr_status", "push_to_emr", "physician_confirmed",
    "scr_umol_L", "egfr_ml_min", "k_mmol_L", "p_mmol_L", "ca_mmol_L", "na_mmol_L",
    "cl_mmol_L", "bun_mmol_L", "albumin_g_L", "prealbumin_mg_L", "hb_g_L",
    "ipth_pg_mL", "vitd25oh_nmol_L", "ua_umol_L", "urine_protein_g_24h", "upcr_mg_mmol",
    "critical_value", "critical_flag",
    "prior_level", "level_correction",
    "nutrition_ceiling", "stage_confirmed_by", "z_score_height",
})

# 上述字段「绝不可见」的角色（家庭助手）。M9 报告层据此做受限脱敏（OD-013）。
CLINICIAN_ONLY_HIDDEN_FROM: frozenset[str] = frozenset({"parent_assistant"})

# ---------------------------------------------------------------- 权限矩阵（v2.3 阶段 0：3 角色 × 5 MCP，CKDNutri P1–P5）

ACCESS_NONE = "-"
ACCESS_READ = "R"
ACCESS_LIMITED = "RL"
ACCESS_RW = "R/W"

_READ_OK: frozenset[str] = frozenset({ACCESS_READ, ACCESS_LIMITED, ACCESS_RW})

_PERMISSION_MATRIX: dict[str, dict[str, str]] = {
    "CKDNutri-clinical-data-mcp": {
        # doctor=R/W：临床助手可读写患儿档案与化验
        # parent=RL：家庭助手仅受限视图（脱敏）
        # risk=R：风险引擎读分期/化验
        "doctor_assistant": ACCESS_RW,
        "parent_assistant": ACCESS_LIMITED,
        "risk_warning": ACCESS_READ,
    },
    "CKDNutri-nutrition-mcp": {
        # doctor=R/W：临床角色读营养数据 + 跑计算面工具（CLINICAL_ROLES 单独收口）
        #   + 按需求 P2 工具表（临床=✔）可写饮食日记 upsert_food_diary（2026-08-12 需求对齐）
        # parent=R/W：写饮食日记(upsert_food_diary) 且需回读日记摘要 —— 与 WRITE_TOOL_POLICY 一致
        # risk=-：风险引擎不读营养域
        "doctor_assistant": ACCESS_RW,
        "parent_assistant": ACCESS_RW,
        "risk_warning": ACCESS_NONE,
    },
    "CKDNutri-care-mcp": {
        # doctor=R/W：临床助手可创建/确认通知
        # parent=R：家庭助手读通知（含闭环状态查看）
        # risk=R/W：管线身份写通知（notify_* 系列由 WRITE_TOOL_POLICY 钳制为 {risk_warning}）
        "doctor_assistant": ACCESS_RW,
        "parent_assistant": ACCESS_READ,
        "risk_warning": ACCESS_RW,
    },
    "CKDNutri-assessment-mcp": {
        # doctor=R：读 eGFR / 分期
        # parent=-：分期类问题由家庭助手读 M1 已确诊分期（MX-1），不暴露评估域
        # risk=R：风险引擎读分期
        "doctor_assistant": ACCESS_READ,
        "parent_assistant": ACCESS_NONE,
        "risk_warning": ACCESS_READ,
    },
    "CKDNutri-content-mcp": {
        # doctor=R/W：报告生成 + push_to_emr 需 R/W 回查
        # parent=RL：受限报告视图
        # risk=R：风险引擎读报告上下文
        "doctor_assistant": ACCESS_RW,
        "parent_assistant": ACCESS_LIMITED,
        "risk_warning": ACCESS_READ,
    },
}
PERMISSION_MATRIX: Mapping[str, Mapping[str, str]] = MappingProxyType({
    k: MappingProxyType(v) for k, v in _PERMISSION_MATRIX.items()
})

# M12 按角色切语料 profile（v2.3 阶段 0：删 nutritionist/child_companion）
KNOWLEDGE_PROFILE: dict[str, str] = {
    "doctor_assistant": "full",
    "risk_warning": "full",
    "parent_assistant": "plain_language",
}

# ---------------------------------------------------------------- MX-3 写权（v2.3 阶段 0：删 M11 两条 + 清理退役角色）

class WriteToolPolicy(TypedDict):
    """MX-3 写工具策略条目（类型化，替代裸 dict[str, object]）。"""
    mcp: str
    allowed: frozenset[str]
    requires_confirmation: bool
    note: str


_WRITE_TOOL_POLICY: dict[str, WriteToolPolicy] = {
    "upsert_lab_result": {
        "mcp": "CKDNutri-clinical-data-mcp",
        "allowed": frozenset({"doctor_assistant"}),
        "requires_confirmation": False,
        "note": "写入后触发 HAIP Workflow → P4 assess_clinical_status 重评",
    },
    "upsert_food_diary": {
        "mcp": "CKDNutri-nutrition-mcp",
        # 需求 P2 工具表（2026-08-12 对齐）：临床=✔ 家庭=✔ → parent + doctor 双写；
        # 豁免矩阵回查（_MATRIX_EXEMPT_WRITE_TOOLS），受 enforce_nutrition_tool 工具级 ACL 管辖。
        "allowed": frozenset({"parent_assistant", "doctor_assistant"}),
        "requires_confirmation": False,
        "note": "打卡落点，供 sum_diet_intake 与食谱参考依从性；医生可代录（临床=✔）",
    },
    # M11 log_meal_checkin / award_badge 退役
    "push_to_emr": {
        "mcp": "CKDNutri-content-mcp",
        "allowed": frozenset({"doctor_assistant"}),
        "requires_confirmation": True,
        "note": "需调用方另传 physician_confirmed=true，人在回路",
    },
    "notify_physician": {
        "mcp": "CKDNutri-care-mcp",
        "allowed": frozenset({"risk_warning"}),         # 仅管线身份
        "requires_confirmation": False,
        "note": "推送前须过 24h 同规则去重",
    },
    "notify_parent": {
        "mcp": "CKDNutri-care-mcp",
        "allowed": frozenset({"risk_warning"}),
        "requires_confirmation": False,
        "note": "推送前须过 24h 同规则去重",
    },
    "trigger_warning_event": {
        "mcp": "CKDNutri-care-mcp",
        "allowed": frozenset({"risk_warning"}),
        "requires_confirmation": False,
        "note": "等级须由本轮新数据重评得出，禁止沿用历史等级",
    },
    "close_warning": {
        "mcp": "CKDNutri-care-mcp",
        "allowed": frozenset({"risk_warning"}),
        "requires_confirmation": False,
        "note": "关闭工单需留判定链路",
    },
    "get_adherence_score": {
        "mcp": "CKDNutri-care-mcp",
        "allowed": frozenset({"doctor_assistant"}),     # v2.3: 删 nutritionist / orchestrator
        "requires_confirmation": False,
        "note": "OD-014：依从性评分落库（写），仅临床助手可写；家庭助手 M4=RL 只读",
    },
    # M4 随访写操作（与 FOLLOWUP_WRITE_ALLOWED 收口一致：仅 doctor）
    "schedule_followup": {
        "mcp": "CKDNutri-care-mcp",
        "allowed": frozenset({"doctor_assistant"}),
        "requires_confirmation": False,
        "note": "安排随访计划，仅医生助手可写",
    },
    "add_followup_record": {
        "mcp": "CKDNutri-care-mcp",
        "allowed": frozenset({"doctor_assistant"}),
        "requires_confirmation": False,
        "note": "写入随访记录，仅医生助手可写",
    },
    # M10 通用通知（与 NOTIFY_WRITE_ROLES 收口一致：{doctor, risk}）
    "create_notification": {
        "mcp": "CKDNutri-care-mcp",
        "allowed": frozenset({"doctor_assistant", "risk_warning"}),
        "requires_confirmation": False,
        "note": "通用通知创建，医生助手和风险管线可发",
    },
    # 闭环状态机推移：仅临床助手（需求 §5.1「仅 CKD 临床助手」），risk_warning 管线身份不得推移人工闭环。
    # 2026-08-12 补登记：此前未登记导致回退矩阵 R/W 判定，risk_warning 可越权推移状态机。
    "update_notification_status": {
        "mcp": "CKDNutri-care-mcp",
        "allowed": frozenset({"doctor_assistant"}),
        "requires_confirmation": False,
        "note": "闭环状态机 unacked→confirmed→resolved→closed（严格一步流转），仅临床助手",
    },
    "escalate_notification": {
        "mcp": "CKDNutri-care-mcp",
        "allowed": frozenset({"doctor_assistant"}),
        "requires_confirmation": False,
        "note": "BUG-46：标记通知升级（escalated 独立布尔，与 workflow_status 正交），仅临床助手；HAIP 自动升级也经此登记落审计",
    },
}
# 值实际结构遵循 WriteToolPolicy（内层仍为 mappingproxy 只读代理，深冻结不变）
WRITE_TOOL_POLICY: Mapping[str, WriteToolPolicy] = cast(
    Mapping[str, WriteToolPolicy],
    MappingProxyType({k: MappingProxyType(v) for k, v in _WRITE_TOOL_POLICY.items()}),
)

WRITE_TOOL_ALIASES: dict[str, str] = {
    "写回病历": "push_to_emr", "写入病历": "push_to_emr", "推送病历": "push_to_emr",
    "录入化验": "upsert_lab_result", "写入化验": "upsert_lab_result",
    "新增生化": "upsert_lab_result", "新增检验": "upsert_lab_result",
    "记录饮食日记": "upsert_food_diary", "录入饮食": "upsert_food_diary",
    "写入饮食日记": "upsert_food_diary",
    # 打卡/勋章别名退役（M11 退役）
    "推送医生": "notify_physician", "通知医生": "notify_physician",
    "通知家长": "notify_parent", "发预警": "trigger_warning_event",
    "触发预警": "trigger_warning_event", "关闭预警": "close_warning",
    # 新增写工具中文别名（与 WRITE_TOOL_POLICY 同步）
    "安排随访": "schedule_followup", "预约随访": "schedule_followup",
    "添加随访": "add_followup_record", "写入随访": "add_followup_record",
    "创建通知": "create_notification", "发送通知": "create_notification",
    "计算依从性": "get_adherence_score", "落库依从性": "get_adherence_score",
}

# 注：写工具判定唯一依据为 WRITE_TOOL_POLICY（detect_write_tool 直接查字典），
# 不存在基于前缀或中文提示词的试探性判定。get_adherence_score 虽以 get_ 开头，但已在
# WRITE_TOOL_POLICY 中登记为写工具（OD-014：评分落库），detect_write_tool 可正确识别。
# WRITE_TOOL_ALIASES 中的中文别名（"录入化验"/"记录饮食"/"推送医生"）仅用于
# detect_write_tool 的辅助匹配，不参与 is_write_action 判定。


def resolve_access(access: str, is_write: bool) -> bool:
    """把矩阵格子翻译成布尔放行结果。"""
    if is_write:
        return access == ACCESS_RW
    return access in _READ_OK


# ---------------------------------------------------------------- OD-011：从矩阵派生本地集合

def _matrix_readers(mcp: str) -> frozenset[str]:
    """从权限矩阵派生：对该 mcp 有读权限（R/RL/RW）的 caller 集合。
    mcp 未登记时返回空集合（fail-closed），不抛 KeyError。
    """
    real_mcp = normalize_mcp(mcp)
    entry = PERMISSION_MATRIX.get(real_mcp, {})
    return frozenset(c for c, a in entry.items() if a in _READ_OK)


def _matrix_writers(mcp: str) -> frozenset[str]:
    """从权限矩阵派生：对该 mcp 有写权限（R/W）的 caller 集合。

    OD-011 收口点：各包本地写白名单的唯一事实源就是矩阵，不允许再手写一份更宽的集合。
    内部先 normalize_mcp 防御：若传入未归一化的旧包名（如 a207-care-mcp）也安全落回矩阵键。
    mcp 未登记时返回空集合（fail-closed），不抛 KeyError。
    """
    real_mcp = normalize_mcp(mcp)
    entry = PERMISSION_MATRIX.get(real_mcp, {})
    return frozenset(c for c, a in entry.items() if a == ACCESS_RW)


# ================================================================
# 各包本地放行集合（v2.3 阶段 0：从 3 角色矩阵重新派生）
# ================================================================

# --- M1 HIS ---
HIS_FULL_VIEW: frozenset[str] = frozenset({"doctor_assistant", "risk_warning"})
HIS_LIMITED: frozenset[str] = frozenset({"parent_assistant"})
HIS_READ: frozenset[str] = HIS_FULL_VIEW | HIS_LIMITED
HIS_BLOCKED: frozenset[str] = frozenset()        # 无被完全封锁的角色
HIS_COHORT: frozenset[str] = frozenset({"doctor_assistant", "risk_warning"})
HIS_ALLOWED_FILTER_KEYS: frozenset[str] = frozenset(
    {"age_band", "ckd_stage", "dialysis", "sex", "primary_disease",
     "has_allergies", "min_age_years", "max_age_years"})

# F3（v2.3）：P1 HIS 家长视图专有敏感聚合块，登记到单一事实源，避免与 CLINICIAN_ONLY_FIELDS 双轨漂移。
# 这些块家长绝不可见：
#   biochemistry      —— 原始化验面板（内含 scr_umol_L / k_mmol_L 等 CLINICIAN_ONLY_FIELDS 叶子）
#   food_diary_5d     —— 5 日饮食日记（原始摄入）
#   dialysis_detail   —— 透析明细
#   medical_record_no —— 病案号（PII）
#   bsa_m2            —— 体表面积（临床计算指标，家长视图不展示）
# 注：z_score_height / stage_confirmed_by 等叶子级敏感键已由 CLINICIAN_ONLY_FIELDS 覆盖，无需在此重复。
P1_PARENT_HIDDEN_FIELDS: frozenset[str] = frozenset({
    "biochemistry", "food_diary_5d", "dialysis_detail", "medical_record_no", "bsa_m2",
})

# --- M2 LIS ---
LIS_READ_FULL: frozenset[str] = frozenset({"doctor_assistant", "risk_warning"})
LIS_READ_LIMITED: frozenset[str] = frozenset({"parent_assistant"})
LIS_CRITICAL_CHANNEL: frozenset[str] = frozenset({"risk_warning", "doctor_assistant"})
LIS_WRITE_ALLOWED: frozenset[str] = _matrix_writers("CKDNutri-clinical-data-mcp")   # {doctor}

# --- M4 随访 ---
# 随访写操作（schedule_followup / add_followup_record）仅临床助手可写；
# risk_warning 的写权走 notify_* 单独通道（WRITE_TOOL_POLICY 钳制），
# 不派生自矩阵（矩阵 care×risk=R/W 是为 notify_* 放行，并非常规随访写权）。
FOLLOWUP_WRITE_ALLOWED: frozenset[str] = frozenset({"doctor_assistant"})  # {doctor}
FOLLOWUP_CLINICIAN: frozenset[str] = frozenset({"doctor_assistant", "risk_warning"})

# --- M3 营养评估 工具级 ACL ---
# OD-011 收口：写白名单唯一事实源=矩阵，不允许手写更宽集合（与 LIS/FOLLOWUP 一致）。
# 矩阵 nutrition×parent=R/W、×doctor=R/W（2026-08-12 需求对齐：临床可代录日记）
# ⇒ 这里自然得出 {parent_assistant, doctor_assistant}。
NUTRITION_ASSESSMENT_WRITE_ALLOWED: frozenset[str] = _matrix_writers("CKDNutri-nutrition-mcp")
NUTRITION_ASSESSMENT_DATA_TOOLS: frozenset[str] = frozenset({
    "upsert_food_diary",
    "get_food_diary_summary",
})
NUTRITION_ASSESSMENT_DATA_ROLES: frozenset[str] = frozenset({
    "parent_assistant",
    "doctor_assistant",
})
NUTRITION_ASSESSMENT_CLINICAL_TOOLS: frozenset[str] = frozenset({
    "calc_prnt_targets", "assess_intake_vs_target", "assess_pew_risk",
    "calc_growth_zscore", "record_pew_risk", "get_pew_history",
    # 2026-08-12 补登记：recipe 组与 DAG 一键评估同属临床工具（需求 recipe/clinical 组仅临床 ✔）
    "generate_meal_plan", "get_meal_plan_nutrients",
    "comprehensive_nutrition_assessment",
})
NUTRITION_ASSESSMENT_CLINICAL_ROLES: frozenset[str] = frozenset({"doctor_assistant"})

# --- M10 通知 ---
# NOTIFY_WRITE_ROLES 用于通用通知 CRUD（create_notification），涵盖 {doctor, risk}。
# 注：notify_* 触发器类工具（notify_physician / notify_parent 等）必须通过
# enforce_write → gate._enforce → WRITE_TOOL_POLICY 进行校验（仅 risk_warning），
# 不得使用 NOTIFY_WRITE_ROLES 做本地判定——否则 doctor 可绕过通知自动化管线。
NOTIFY_WRITE_ROLES: frozenset[str] = _matrix_writers("CKDNutri-care-mcp")   # {doctor, risk}
NOTIFY_READ_ROLES: frozenset[str] = _matrix_readers("CKDNutri-care-mcp")    # {doctor, parent, risk}
# M11 游戏化 —— v2.3 阶段 0 退役，相关集合全部移除

# v2.3 阶段 0 兼容空值（M11/router 本地代码仍 import 这些符号，M11 退役后清理）
# GAMIFICATION_ALLOWED 是「白名单」语义：空集 ⇒ 任何调用方都不在名单内 ⇒ fail-closed 拒绝，
#   与退役行为一致（依赖 GAMIFICATION_MCP/GAMIFICATION_ALLOWED 的旧代码一律被拒）。
# GAMIFICATION_MCP 现指向 M11 并入后的新家（M3 / P2）。
# CHILD_FORBIDDEN_MCPS 是「黑名单」语义：空集 ⇒ 无角色被封锁（child_companion 已随角色
#   退役，CALLERS 中已不存在该角色，无需封锁项），与 HIS_BLOCKED 的空集注释一致。
GAMIFICATION_MCP = "CKDNutri-nutrition-mcp"
GAMIFICATION_ALLOWED: frozenset[str] = frozenset()
CHILD_FORBIDDEN_MCPS: frozenset[str] = frozenset()
