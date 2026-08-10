"""权限矩阵 / 写权策略 / 各包放行集合 —— A207 唯一事实源（Plan A 收口位置）。

本模块取代原先散落在 M1/M2/M4/M11/M13 的多份权限副本（架构复盘 P1-1 指出的漂移源）。
所有 MCP 从这里 import，不再各自维护一份。

注意：本模块**只固化数据**，不擅自改变「谁能做什么」。各包本地守卫逻辑保留，
只是把 caller 来源改成 env 注入、把放行集合改成从本模块 import。
矩阵值 = 需求文档 §7 + MX-3 写权收口；各包本地集合 = 当前实际生效范围（已核对代码）。
"""

from __future__ import annotations

# ---------------------------------------------------------------- 角色与 MCP

CALLERS: tuple[str, ...] = (
    "orchestrator",
    "doctor_assistant",
    "nutritionist",
    "parent_assistant",
    "child_companion",
    "risk_warning",
)

MCP_REGISTRY: dict[str, str] = {
    "a207-his-mcp": "M1",
    "a207-lis-mcp": "M2",
    "a207-nutrition-assessment-mcp": "M3",
    "a207-followup-mcp": "M4",
    "a207-nutrition-calc-mcp": "M5",
    "a207-ckd-clinical-calc-mcp": "M6",
    "a207-meal-plan-mcp": "M7",
    "a207-risk-rules-mcp": "M8",
    "a207-report-mcp": "M9",
    "a207-notify-mcp": "M10",
    "a207-gamification-mcp": "M11",
    "a207-knowledge-mcp": "M12",
    "a207-router-mcp": "M13",
}

GAMIFICATION_MCP = "a207-gamification-mcp"
CLINICAL_CALC_MCP = "a207-ckd-clinical-calc-mcp"

# 包名别名归一（OD-002）：登记表沿用早期命名，实际交付目录/PyPI 包名已变更。
MCP_ALIASES: dict[str, str] = {
    "a207-clinical-calc-mcp": "a207-ckd-clinical-calc-mcp",
    "a207-notification-mcp": "a207-notify-mcp",
    "a207-nutrition-assessment-mcp-nfyy": "a207-nutrition-assessment-mcp",
}


def normalize_mcp(name: str) -> str:
    """把实际发行/目录包名归一为登记表内的键；未知名原样返回（由上层判未登记）。"""
    key = (name or "").strip()
    return MCP_ALIASES.get(key, key)


# MX-1：分期类问题不由营养师 / 家长助手判定，改读 M1 已确诊分期
MX1_BLOCKED_CALLERS: frozenset[str] = frozenset({"nutritionist", "parent_assistant"})

# MX-2：患儿伙伴的医疗数据禁区（构建规格 §2.1 逐条列出的 8 个包）
CHILD_FORBIDDEN_MCPS: frozenset[str] = frozenset(
    {
        "a207-his-mcp",
        "a207-lis-mcp",
        "a207-nutrition-calc-mcp",
        "a207-ckd-clinical-calc-mcp",
        "a207-meal-plan-mcp",
        "a207-risk-rules-mcp",
        "a207-report-mcp",
        "a207-notify-mcp",
    }
)

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

# 与 CLINICIAN_ONLY_FIELDS 配套：上述字段「绝不可见」的角色（家庭角色）。M9 报告层据此对
# sections / summary_markdown 做受限脱敏（OD-013）。单一事实源放在策略包，避免各 MCP 包
# 再硬编码角色集合（conformance C3 禁止包内硬编码角色集字面量）。
CLINICIAN_ONLY_HIDDEN_FROM: frozenset[str] = frozenset({"parent_assistant", "child_companion"})

# ---------------------------------------------------------------- 权限矩阵（§7）

ACCESS_NONE = "-"
ACCESS_READ = "R"
ACCESS_LIMITED = "RL"
ACCESS_RW = "R/W"

_READ_OK: frozenset[str] = frozenset({ACCESS_READ, ACCESS_LIMITED, ACCESS_RW})

PERMISSION_MATRIX: dict[str, dict[str, str]] = {
    "a207-his-mcp": {
        "orchestrator": ACCESS_READ, "doctor_assistant": ACCESS_READ,
        "nutritionist": ACCESS_LIMITED, "parent_assistant": ACCESS_LIMITED,
        "child_companion": ACCESS_NONE, "risk_warning": ACCESS_READ,
    },
    "a207-lis-mcp": {
        "orchestrator": ACCESS_NONE, "doctor_assistant": ACCESS_RW,
        "nutritionist": ACCESS_NONE, "parent_assistant": ACCESS_LIMITED,
        "child_companion": ACCESS_NONE, "risk_warning": ACCESS_READ,
    },
    "a207-nutrition-assessment-mcp": {
        "orchestrator": ACCESS_RW, "doctor_assistant": ACCESS_READ,
        "nutritionist": ACCESS_READ, "parent_assistant": ACCESS_LIMITED,
        "child_companion": ACCESS_LIMITED, "risk_warning": ACCESS_READ,
    },
    "a207-followup-mcp": {
        "orchestrator": ACCESS_RW, "doctor_assistant": ACCESS_RW,
        "nutritionist": ACCESS_RW, "parent_assistant": ACCESS_LIMITED,
        "child_companion": ACCESS_LIMITED, "risk_warning": ACCESS_READ,
    },
    "a207-nutrition-calc-mcp": {
        "orchestrator": ACCESS_NONE, "doctor_assistant": ACCESS_READ,
        "nutritionist": ACCESS_READ, "parent_assistant": ACCESS_READ,
        "child_companion": ACCESS_NONE, "risk_warning": ACCESS_NONE,
    },
    "a207-ckd-clinical-calc-mcp": {
        "orchestrator": ACCESS_NONE, "doctor_assistant": ACCESS_READ,
        "nutritionist": ACCESS_NONE, "parent_assistant": ACCESS_NONE,
        "child_companion": ACCESS_NONE, "risk_warning": ACCESS_NONE,
    },
    "a207-meal-plan-mcp": {
        "orchestrator": ACCESS_NONE, "doctor_assistant": ACCESS_READ,
        "nutritionist": ACCESS_READ, "parent_assistant": ACCESS_NONE,
        "child_companion": ACCESS_NONE, "risk_warning": ACCESS_NONE,
    },
    "a207-risk-rules-mcp": {
        "orchestrator": ACCESS_NONE, "doctor_assistant": ACCESS_READ,
        "nutritionist": ACCESS_NONE, "parent_assistant": ACCESS_NONE,
        "child_companion": ACCESS_NONE, "risk_warning": ACCESS_READ,
    },
    "a207-report-mcp": {
        "orchestrator": ACCESS_NONE, "doctor_assistant": ACCESS_RW,
        "nutritionist": ACCESS_RW, "parent_assistant": ACCESS_LIMITED,
        "child_companion": ACCESS_NONE, "risk_warning": ACCESS_READ,
    },
    "a207-notify-mcp": {
        # OD-012 校正（OD-011 派生时误把编排层/家长挡死，导致 notify 单测回归 + 编排层写通知被拒）：
        # - orchestrator=R/W：ADR-010 明确"编排层在 M4/M8/M9 末端事件后调用 build_event_notification 写入"
        # - doctor=R/W：临床角色可创建/确认通知
        # - parent=READ：README 明确"家长读自己孩子的通知"
        # - risk_warning=R/W：风险预警由 risk_warning 推送（WRITE_TOOL_POLICY notify_* 仍限 risk_warning）
        # - nutritionist/child=NONE：营养师不在通知回路；患儿受 MX-2 禁区约束
        "orchestrator": ACCESS_RW, "doctor_assistant": ACCESS_RW,
        "nutritionist": ACCESS_NONE, "parent_assistant": ACCESS_READ,
        "child_companion": ACCESS_NONE, "risk_warning": ACCESS_RW,
    },
    "a207-gamification-mcp": {
        "orchestrator": ACCESS_NONE, "doctor_assistant": ACCESS_NONE,
        "nutritionist": ACCESS_NONE, "parent_assistant": ACCESS_NONE,
        "child_companion": ACCESS_RW, "risk_warning": ACCESS_NONE,
    },
    "a207-knowledge-mcp": {
        "orchestrator": ACCESS_NONE, "doctor_assistant": ACCESS_READ,
        "nutritionist": ACCESS_READ, "parent_assistant": ACCESS_LIMITED,
        "child_companion": ACCESS_LIMITED, "risk_warning": ACCESS_READ,
    },
    "a207-router-mcp": {
        "orchestrator": ACCESS_READ, "doctor_assistant": ACCESS_NONE,
        "nutritionist": ACCESS_NONE, "parent_assistant": ACCESS_NONE,
        "child_companion": ACCESS_NONE, "risk_warning": ACCESS_NONE,
    },
}

# M12 按角色切语料 profile；注意 M12 旧接口用 role 命名空间(doctor/parent/child)，
# 已由各包在转换层把 caller 映射到 role，本表保持 caller 维度。
KNOWLEDGE_PROFILE: dict[str, str] = {
    "doctor_assistant": "full",
    "nutritionist": "full",
    "risk_warning": "full",
    "parent_assistant": "plain_language",
    "child_companion": "child_safe",
}

# ---------------------------------------------------------------- MX-3 写权
WRITE_TOOL_POLICY: dict[str, dict[str, object]] = {
    "upsert_lab_result": {
        "mcp": "a207-lis-mcp",
        "allowed": frozenset({"doctor_assistant"}),
        "requires_confirmation": False,
        "note": "写入后强制触发 M8 重评；命中危急值经 get_critical_values 走 M10",
    },
    "upsert_food_diary": {
        "mcp": "a207-nutrition-assessment-mcp",
        "allowed": frozenset({"parent_assistant", "child_companion"}),
        "requires_confirmation": False,
        "note": "打卡回流的落点，供下次 sum_diet_intake 与食谱参考依从性",
    },
    "log_meal_checkin": {
        "mcp": GAMIFICATION_MCP,
        "allowed": frozenset({"child_companion"}),
        "requires_confirmation": False,
        "note": "只存吃了什么，全程零医疗判定",
    },
    "award_badge": {
        "mcp": GAMIFICATION_MCP,
        "allowed": frozenset({"child_companion"}),
        "requires_confirmation": False,
        "note": "激励侧写入，不得写入任何医疗结论",
    },
    "push_to_emr": {
        "mcp": "a207-report-mcp",
        "allowed": frozenset({"doctor_assistant"}),
        "requires_confirmation": True,
        "note": "需调用方另传 physician_confirmed=true，人在回路",
    },
    "notify_physician": {
        "mcp": "a207-notify-mcp",
        "allowed": frozenset({"risk_warning"}),
        "requires_confirmation": False,
        "note": "推送前须过 24h 同规则去重",
    },
    "notify_parent": {
        "mcp": "a207-notify-mcp",
        "allowed": frozenset({"risk_warning"}),
        "requires_confirmation": False,
        "note": "推送前须过 24h 同规则去重",
    },
    "trigger_warning_event": {
        "mcp": "a207-notify-mcp",
        "allowed": frozenset({"risk_warning"}),
        "requires_confirmation": False,
        "note": "等级须由本轮新数据重评得出，禁止沿用历史等级",
    },
    "close_warning": {
        "mcp": "a207-notify-mcp",
        "allowed": frozenset({"risk_warning"}),
        "requires_confirmation": False,
        "note": "关闭工单需留判定链路",
    },
}

WRITE_TOOL_ALIASES: dict[str, str] = {
    "写回病历": "push_to_emr", "写入病历": "push_to_emr", "推送病历": "push_to_emr",
    "录入化验": "upsert_lab_result", "写入化验": "upsert_lab_result",
    "新增生化": "upsert_lab_result", "新增检验": "upsert_lab_result",
    "记录饮食日记": "upsert_food_diary", "录入饮食": "upsert_food_diary",
    "写入饮食日记": "upsert_food_diary",
    "打卡": "log_meal_checkin", "颁发勋章": "award_badge", "发勋章": "award_badge",
    "推送医生": "notify_physician", "通知医生": "notify_physician",
    "通知家长": "notify_parent", "发预警": "trigger_warning_event",
    "触发预警": "trigger_warning_event", "关闭预警": "close_warning",
}

WRITE_ACTION_PREFIXES: tuple[str, ...] = (
    "write", "create", "update", "delete", "insert",
    "upsert_", "notify_", "log_", "award_", "push_", "trigger_",
    "close_", "schedule_", "save_",
)

CN_WRITE_HINTS: tuple[str, ...] = ("写入", "写回", "新增一条", "保存到", "提交写")


def resolve_access(access: str, is_write: bool) -> bool:
    """把矩阵格子翻译成布尔放行结果。"""
    if is_write:
        return access == ACCESS_RW
    return access in _READ_OK


# ---------------------------------------------------------------- OD-011：从矩阵派生本地集合
# 本地「放行集合」不再独立声明，改由 PERMISSION_MATRIX 实时派生，杜绝第二副本漂移。
def _matrix_readers(mcp: str) -> frozenset[str]:
    """从权限矩阵派生：对该 mcp 有读权限（R/RL/RW）的 caller 集合。"""
    return frozenset(c for c, a in PERMISSION_MATRIX[mcp].items() if a in _READ_OK)


def _matrix_writers(mcp: str) -> frozenset[str]:
    """从权限矩阵派生：对该 mcp 有写权限（R/W）的 caller 集合。

    OD-011 收口点：各包本地写白名单的唯一事实源就是矩阵，不允许再手写一份更宽的集合。
    """
    return frozenset(c for c, a in PERMISSION_MATRIX[mcp].items() if a == ACCESS_RW)


# ================================================================
# 各包本地放行集合（原散落副本，集中到此处作为单一事实源）
# 命名带包前缀，避免与矩阵概念混淆。值 = 当前代码实际生效范围。
# ================================================================

# --- M1 HIS（core.py 原 READ_CALLERS / BLOCKED_CALLERS / COHORT_CALLERS 等）---
HIS_FULL_VIEW: frozenset[str] = frozenset({"orchestrator", "doctor_assistant", "risk_warning"})
HIS_LIMITED: frozenset[str] = frozenset({"nutritionist", "parent_assistant"})
HIS_READ: frozenset[str] = HIS_FULL_VIEW | HIS_LIMITED
HIS_BLOCKED: frozenset[str] = frozenset({"child_companion"})
HIS_COHORT: frozenset[str] = frozenset(
    {"orchestrator", "doctor_assistant", "nutritionist", "risk_warning"})
HIS_ALLOWED_FILTER_KEYS: frozenset[str] = frozenset(
    {"age_band", "ckd_stage", "dialysis", "sex", "primary_disease",
     "has_allergies", "min_age_years", "max_age_years"})

# --- M2 LIS（core.py 原 READ_FULL / READ_LIMITED / CRITICAL_CHANNEL / WRITE_ALLOWED）---
# 读视图分层（full/limited）属视图 tier，与矩阵 RL 一致，保留原声明；写白名单改由矩阵派生（OD-011）。
LIS_READ_FULL: frozenset[str] = frozenset({"doctor_assistant", "risk_warning"})
LIS_READ_LIMITED: frozenset[str] = frozenset({"parent_assistant"})
LIS_CRITICAL_CHANNEL: frozenset[str] = frozenset({"risk_warning", "doctor_assistant"})
LIS_WRITE_ALLOWED: frozenset[str] = _matrix_writers("a207-lis-mcp")  # OD-011: 派生自矩阵（M2 doctor=R/W）

# --- M4 随访（core.py 原 _WRITE_ALLOWED / _CLINICIAN）---
FOLLOWUP_WRITE_ALLOWED: frozenset[str] = _matrix_writers("a207-followup-mcp")  # OD-011: doctor/nutritionist/orchestrator = R/W
FOLLOWUP_CLINICIAN: frozenset[str] = frozenset(
    {"doctor_assistant", "nutritionist", "orchestrator", "risk_warning"})

# --- M3 营养评估 工具级 ACL（OD-010 选项 A）---
# M3 是「计算 + 数据」混合包：calc_prnt_targets / assess_pew_risk / calc_growth_zscore 是临床判读（计算面），
# upsert_food_diary / get_food_diary_summary 是家长/患儿日常记录与聚合（数据面）。
# 权限矩阵是 MCP 粒度，无法表达"同包内某些工具家长能写、某些只能临床角色算"。
# 故在此用工具级白名单作为矩阵之下的精细授权层（单一事实源，集中此处；enforce_nutrition_tool 据此执行）：
#   - 数据面·写 upsert_food_diary：仅家长/患儿（NUTRITION_ASSESSMENT_WRITE_ALLOWED）
#   - 数据面·读 get_food_diary_summary：家长/患儿/临床角色（NUTRITION_ASSESSMENT_DATA_ROLES）
#   - 计算面（6 个临床判读/落库工具）：仅 doctor/nutritionist/orchestrator（NUTRITION_ASSESSMENT_CLINICAL_ROLES）
# 矩阵 M3 各行：家长/患儿=RL、临床=READ、orchestrator=R/W（编排层落 PEW 历史需写权）。
NUTRITION_ASSESSMENT_WRITE_ALLOWED: frozenset[str] = frozenset(
    {"parent_assistant", "child_companion"})
NUTRITION_ASSESSMENT_DATA_TOOLS: frozenset[str] = frozenset({
    "upsert_food_diary",          # 数据面·写：家长/患儿记饮食日记（写权见 WRITE_ALLOWED）
    "get_food_diary_summary",     # 数据面·读：脱敏聚合摘要（家长/患儿/临床角色可读）
})
NUTRITION_ASSESSMENT_DATA_ROLES: frozenset[str] = frozenset({
    "parent_assistant", "child_companion",
    "doctor_assistant", "nutritionist", "orchestrator",
})
NUTRITION_ASSESSMENT_CLINICAL_TOOLS: frozenset[str] = frozenset({
    "calc_prnt_targets", "assess_intake_vs_target", "assess_pew_risk",
    "calc_growth_zscore", "record_pew_risk", "get_pew_history",
})
NUTRITION_ASSESSMENT_CLINICAL_ROLES: frozenset[str] = frozenset({
    "doctor_assistant", "nutritionist", "orchestrator",
})

# --- M10 通知（core.py 原 _WRITE_ROLES / _READ_ROLES）---
# OD-011 收口：原先手写 {全部 6 角色} / {编排+临床 4 角色}，比矩阵宽得多（矩阵 M10 仅 doctor=R、risk_warning=R/W，
# 其余 NONE）。现全部派生自矩阵，杜绝第二副本。
NOTIFY_WRITE_ROLES: frozenset[str] = _matrix_writers("a207-notify-mcp")  # → {risk_warning}
NOTIFY_READ_ROLES: frozenset[str] = _matrix_readers("a207-notify-mcp")   # → {doctor, risk_warning}

# --- M11 游戏化（server.py 原 _guard：仅患儿伙伴）---
GAMIFICATION_ALLOWED: frozenset[str] = _matrix_writers(GAMIFICATION_MCP)  # OD-011: 派生自矩阵（M11 child=R/W）
