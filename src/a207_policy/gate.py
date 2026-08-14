"""权限执行点（确定性，非 LLM 可选）。

- check_permission：保留 M13 兼容的返回形态（dict），供路由层查表。
- enforce_read / enforce_write：供各 MCP 工具在入口处调用，放行返回 access 字符串，
  拒绝抛 PermissionDenied（确定性，模型无法绕过 —— 解决 P0-2 门禁虚设的根因：
  校验不再依赖 LLM「自觉」调用某个 gate 工具，而是每个工具内强制）。
- detect_write_tool / is_write_action：MX-3 写工具识别。
"""

from __future__ import annotations

import hmac
import json
import os
import re
from datetime import datetime, timezone

from .caller import get_caller
from .exceptions import PermissionDenied
from .matrix import (
    ACCESS_RW,
    CALLERS,
    KNOWLEDGE_PROFILE,
    NUTRITION_ASSESSMENT_CLINICAL_ROLES,
    NUTRITION_ASSESSMENT_CLINICAL_TOOLS,
    NUTRITION_ASSESSMENT_DATA_ROLES,
    NUTRITION_ASSESSMENT_DATA_TOOLS,
    NUTRITION_ASSESSMENT_WRITE_ALLOWED,
    PERMISSION_MATRIX,
    RETIRED_WRITE_TOOLS,
    WRITE_TOOL_POLICY,
    WRITE_TOOL_ALIASES,
    normalize_mcp,
    resolve_access,
)
from .state import resolve_state_path

_ALIASES = WRITE_TOOL_ALIASES  # 别名引用，保持 detect_write_tool 可读

_NOTIFY_RE = re.compile(r"notify_[a-z_]+")
_UNDERSCORE_PREFIXES = tuple(p for p in (
    "write", "create", "update", "delete", "insert",
    "upsert_", "notify_", "log_", "award_", "push_", "trigger_",
    "close_", "schedule_", "save_",
) if p.endswith("_"))

# S6 修复（2026-08-13）：统一 patient_id 契约校验（单一事实源，与 P1 models 同款
# 正则）。各 MCP 工具入口调用本函数，畸形 patient_id 不得进入存储/查询层——
# 此前部分 HIS 工具入口未校验（P1 models 有，his.py 的 get_patient_profile /
# verify_guardian_binding 等无），畸形 id 直插存储层。
PATIENT_ID_PATTERN = re.compile(r"^P[0-9]{4,}$")


def validate_patient_id(patient_id: Any) -> str:
    """校验并规范化 patient_id（^P[0-9]{4,}$）。非法抛 ValueError（fail-closed）。

    返回 strip 后的字符串；None/非字符串/不匹配正则一律拒绝。所有 MCP 工具入口
    （含 HIS 只读、写库、令牌签发/核验）应统一调用，替代各自手写校验。
    """
    if not isinstance(patient_id, str):
        raise ValueError(f"patient_id 必须为字符串，收到 {patient_id!r}")
    pid = patient_id.strip()
    if not PATIENT_ID_PATTERN.match(pid):
        raise ValueError("patient_id 需匹配 ^P[0-9]{4,}$（如 P0007）")
    return pid

# OD-010 选项 A（已落地）：M3 是「计算+数据」混合包，工具级 ACL（enforce_nutrition_tool）
# 在矩阵（MCP 粒度）之下做细分授权：
#   - upsert_food_diary（写日记）：家长/医生（2026-08-12 需求对齐，临床=✔ 家庭=✔），
#     比矩阵 M3×临床=READ 更宽 → 豁免矩阵 R/W 校验
# record_pew_risk 曾列于豁免但**不在 WRITE_TOOL_POLICY**（2026-08-13 审查发现：detect_write_tool
# 识别不到 → 通用 enforce_write 矩阵 RW 回退使 parent 可越权）。已登记进
# WRITE_TOOL_POLICY（allowed=doctor）并从本豁免移除——登记后通用闸门即可收口，豁免不再必要。
_MATRIX_EXEMPT_WRITE_TOOLS: frozenset[str] = frozenset(
    {"upsert_food_diary"})


def detect_write_tool(text: str) -> str | None:
    """从 intent/action 文本中识别已登记的写工具，识别不到返回 None。"""
    lowered = (text or "").strip().lower()
    if not lowered:
        return None
    for tool in sorted(WRITE_TOOL_POLICY, key=len, reverse=True):
        if tool in lowered:
            return tool
    for alias, tool in sorted(_ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True):
        if alias in lowered:
            return tool
    if _NOTIFY_RE.search(lowered):
        # 2026-08-13（policy 审查）说明：未登记 notify_xxx 收口到 notify_physician 是
        # **刻意 fail-closed**——notify_* 全归风险管线（allowed={risk_warning}），doctor 反被
        # 收紧；risk_warning 对 care 矩阵本就是 RW（映射与否权限不变），无现行越权面。
        # 新增 notify_* 工具必须登记 WRITE_TOOL_POLICY，勿依赖此兜底猜测。
        return "notify_physician"
    return None


def is_write_action(action: str) -> bool:
    lowered = (action or "").strip().lower()
    if any(lowered.startswith(prefix) for prefix in (
        "write", "create", "update", "delete", "insert")):
        return True
    # P-B4 修复（2026-08-14）：下划线前缀须在**词边界**匹配——此前 `prefix in lowered`
    # 子串命中，"log_" 会误命中 "dialog_analysis"（d-ia-LOG_-analysis 内含 log_）等
    # 合法读工具名 → P1×parent 等 R 权限角色被误伤拒绝。加前导 "_" 构造词边界：
    # 仅当前缀出现在开头或前一个字符是下划线时才算命中。
    if any(("_" + prefix) in ("_" + lowered) for prefix in _UNDERSCORE_PREFIXES):
        return True
    return any(hint in lowered for hint in ("写入", "写回", "新增一条", "保存到", "提交写"))


def check_permission(caller: str, mcp: str, action: str = "read") -> dict:
    """M13 兼容查表：返回 {allow, access, resolved_action, reason, ...}。"""
    caller_id = (caller or "").strip()
    mcp_id = normalize_mcp(mcp)
    action_id = (action or "read").strip()

    if caller_id not in CALLERS:
        return _perm(False, "-", "read", f"caller={caller_id or '(空)'} 未在权限矩阵登记")
    if mcp_id not in PERMISSION_MATRIX:
        return _perm(False, "-", "read", f"mcp={mcp or '(空)'} 不在 5 个已登记 MCP 内")

    access = PERMISSION_MATRIX[mcp_id][caller_id]
    tool = detect_write_tool(action_id)
    if tool:
        return _check_write_tool(caller_id, mcp_id, access, tool)

    is_write = is_write_action(action_id)
    allow = resolve_access(access, is_write)
    kind = "write" if is_write else "read"
    verdict = "放行" if allow else "拒绝"
    return _perm(allow, access, kind, f"矩阵 {mcp_id} x {caller_id} = {access}，{kind} 请求{verdict}")


def _check_write_tool(caller_id: str, mcp_id: str, access: str, tool: str) -> dict:
    policy = WRITE_TOOL_POLICY.get(tool)
    if policy is None:
        # P1-1 修复（2026-08-13）：未登记写工具 fail-closed 拒绝（此前按矩阵 R/W 放行
        # ——家长在矩阵 R/W 域拿到未收口写入口）。新增写工具必须显式登记。
        return _perm(False, access, "write",
                     f"写工具 {tool} 未登记 WRITE_TOOL_POLICY，fail-closed 拒绝（P1-1）")
    owner_mcp = str(policy["mcp"])
    allowed: frozenset[str] = policy["allowed"]
    if mcp_id != owner_mcp:
        return _perm(False, access, "write",
                     f"写工具 {tool} 属于 {owner_mcp}，与 mcp={mcp_id} 不一致")
    if caller_id not in allowed:
        return _perm(False, access, "write",
                     f"MX-3 写权收口：{tool} 仅允许 {'/'.join(sorted(allowed))}，"
                     f"当前 caller={caller_id}")
    # 与 _enforce 保持一致：非豁免写工具额外回查矩阵 R/W（矩阵为唯一事实源）
    if tool not in _MATRIX_EXEMPT_WRITE_TOOLS:
        if PERMISSION_MATRIX[owner_mcp][caller_id] != ACCESS_RW:
            return _perm(False, access, "write",
                         f"矩阵 {owner_mcp} x {caller_id} = "
                         f"{PERMISSION_MATRIX[owner_mcp][caller_id]}，无 R/W，拒绝 {tool}")
    # 2026-08-13（policy 审查）：requires_confirmation 仅作**透传提示**，策略层拿不到
    # 确认标志入参（如 physician_confirmed）无法强制——人在回路约束必须在**工具实现层**
    # 校验。当前无 requires_confirmation=True 的登记工具（push_to_emr 幽灵登记已删），
    # 未来登记此类工具时必须在实现层强制，勿依赖调用方自觉。
    return _perm(True, access, "write", f"MX-3 放行：{tool} 由 {caller_id} 执行",
                 tool=tool, requires_confirmation=bool(policy["requires_confirmation"]))


def _perm(allow: bool, access: str, resolved_action: str, reason: str, *,
          tool: str | None = None, requires_confirmation: bool = False) -> dict:
    return {
        "allow": allow,
        "access": access,
        "resolved_action": resolved_action,
        "reason": reason,
        "write_tool": tool,
        "requires_confirmation": requires_confirmation,
        "limited_view": access == "RL",
    }


def enforce_read(mcp_name: str, tool: str | None = None) -> str:
    """入口守卫：caller 是否可读 mcp_name。放行返回 access（R/RL/RW），拒绝抛 PermissionDenied。
    tool 参数预留用于未来细粒度读取工具级控制。
    """
    return _enforce(mcp_name, "read")


def enforce_write(mcp_name: str, tool: str | None = None) -> str:
    """入口守卫：caller 是否可写 mcp_name（tool 为写工具名时走 MX-3）。"""
    # P-B2 修复（2026-08-14）："read" 是动作保留字，不是写工具名——此前
    # enforce_write(mcp, "read") 会经 _enforce 的 `action != "read"` 判定静默降级为
    # **读检查**（写入口按读权限放行，语义反转）。显式 fail-closed 拒绝。
    if tool is not None and str(tool).strip().lower() == "read":
        raise PermissionDenied(
            get_caller(), mcp_name, tool,
            "'read' 是保留动作字，不是写工具名；enforce_write 不接受该值")
    return _enforce(mcp_name, tool or "write")


def enforce_nutrition_tool(caller: str, tool: str) -> str:
    """M3 工具级授权（OD-010 选项 A）：数据面写仅家长/患儿，数据面读 + 计算面仅临床角色。

    M3 是「计算 + 数据」混合包。矩阵对 M3 仅做 MCP 粒度授权（家长=R/W、临床=R/W、
    risk=-），无法表达"同包内某些工具家长能写、某些只能临床角色算"。
    故在此做工具级细分（单一事实源在 matrix 模块）：
    - 数据面·写 upsert_food_diary：家长/医生（需求 2026-08-12 对齐，临床=✔ 家庭=✔；
      与 MX-3 WRITE_TOOL_POLICY / core._WRITE_ALLOWED_CALLERS 一致，单一事实源）
    - 数据面·读 get_food_diary_summary：家长/患儿/临床角色（临床需读日记做评估）
    - 计算面·临床判读/落库（calc_prnt_targets / assess_intake_vs_target / assess_pew_risk /
      calc_growth_zscore / record_pew_risk / get_pew_history / generate_meal_plan /
      get_meal_plan_nutrients / comprehensive_nutrition_assessment）：仅 doctor（临床角色）
    未登记工具 fail-closed 拒绝。caller 为已校验身份字符串（由调用方经 get_caller() 取得）。
    """
    # P-B1 修复（2026-08-14）：caller 必须与进程注入身份一致——本函数接受 caller 参数
    # 是历史契约（调用方经 get_caller() 传入），但公开 API 若被传任意已登记角色名即可
    # 伪造身份（如硬编码 "doctor_assistant"）。与其他入口（enforce_read/write 内部
    # get_caller）对齐：参数与内部身份不一致 → 拒绝（fail-closed）。
    if caller != get_caller():
        raise PermissionDenied(
            caller, "CKDNutri-nutrition-mcp", tool,
            "caller 参数与进程注入身份不一致（身份伪造被拒）")
    if caller not in CALLERS:
        raise PermissionDenied(
            caller, "CKDNutri-nutrition-mcp", tool, "caller 未登记")
    # 数据面·写：饮食日记写入家长/医生（与 MX-3 写权收口一致，单一事实源）
    if tool == "upsert_food_diary":
        if caller in NUTRITION_ASSESSMENT_WRITE_ALLOWED:
            return PERMISSION_MATRIX["CKDNutri-nutrition-mcp"][caller]
        raise PermissionDenied(
            caller, "CKDNutri-nutrition-mcp", tool,
            "饮食日记写入仅限家长/医生")
    # 数据面·读：日记摘要家长/患儿/临床角色可读
    if tool in NUTRITION_ASSESSMENT_DATA_TOOLS:
        if caller in NUTRITION_ASSESSMENT_DATA_ROLES:
            # 2026-08-13（policy 审查）：返回**矩阵值**而非硬编码 "RL"——此前 doctor
            # （矩阵 RW）读 get_food_diary_summary 也被返回 RL，与矩阵语义不一致；下游若
            # 消费返回值会把医生视图误当受限视图。当前 nutrition core 不消费返回值
            # （无实害），修正为矩阵值以保持单一事实源。
            return PERMISSION_MATRIX["CKDNutri-nutrition-mcp"][caller]
        raise PermissionDenied(
            caller, "CKDNutri-nutrition-mcp", tool,
            "日记摘要仅限家长/患儿/临床角色")
    # 计算面·临床判读/落库：仅临床角色
    if tool in NUTRITION_ASSESSMENT_CLINICAL_TOOLS:
        if caller in NUTRITION_ASSESSMENT_CLINICAL_ROLES:
            return PERMISSION_MATRIX["CKDNutri-nutrition-mcp"][caller]
        raise PermissionDenied(
            caller, "CKDNutri-nutrition-mcp", tool,
            "计算面工具仅限临床角色(doctor)")
    raise PermissionDenied(
        caller, "CKDNutri-nutrition-mcp", tool, f"未登记工具: {tool}")


def _enforce(mcp_name: str, action: str) -> str:
    caller = get_caller()
    if caller not in CALLERS:
        raise PermissionDenied(caller, mcp_name, action, f"caller 未登记: {caller}")
    mcp = normalize_mcp(mcp_name)
    if mcp not in PERMISSION_MATRIX:
        raise PermissionDenied(caller, mcp_name, action, "mcp 未登记")
    access = PERMISSION_MATRIX[mcp][caller]

    wt = detect_write_tool(action) if action != "read" else None
    # P-B3 修复（2026-08-14）：退役写工具 fail-closed——push_to_emr 等已删登记但
    # 中文别名曾保留（中英文结论相反）；现在别名已删 + 此处拦截英文名直传（否则
    # detect_write_tool 未命中会回退矩阵 R/W 放行幽灵工具）。
    if action != "read":
        _act_low = (action or "").strip().lower()
        for _ret in RETIRED_WRITE_TOOLS:
            if _ret in _act_low:
                raise PermissionDenied(
                    caller, mcp, action,
                    f"写工具 {_ret} 已退役（未实现/已下线），拒绝执行")
    if wt:
        policy = WRITE_TOOL_POLICY.get(wt)
        if policy is None:
            if not resolve_access(access, True):
                raise PermissionDenied(caller, mcp, action, f"access={access} 不允许写")
            # P1-1 修复（2026-08-13）：detect_write_tool 命中但 WRITE_TOOL_POLICY 未登记
            # （如新加写工具忘登记）→ **fail-closed 拒绝**，不再按矩阵 R/W 放行。
            # 此前"未登记写工具回退矩阵"会让家长在矩阵 R/W 域（如 P2）拿到未收口的
            # 写入口；新增写工具必须显式登记 WRITE_TOOL_POLICY。
            raise PermissionDenied(
                caller, mcp, action,
                f"写工具 {wt} 未登记 WRITE_TOOL_POLICY（fail-closed）："
                f"新增写工具必须显式登记后再放行（P1-1）")
        elif mcp != policy["mcp"] or caller not in policy["allowed"]:
            raise PermissionDenied(caller, mcp, action, f"MX-3 写权受限: {wt}")
        else:
            # OD-011：登记写工具额外回查矩阵 R/W（OD-010 待决豁免除外），确保矩阵为唯一事实源。
            # 例：notify_* 仅 risk_warning=R/W 可写；push_to_emr 仅 doctor=R/W（即便 nutritionist 同有 R/W 仍按工具白名单收口）。
            if wt not in _MATRIX_EXEMPT_WRITE_TOOLS:
                owner = policy["mcp"]
                if PERMISSION_MATRIX[owner][caller] != ACCESS_RW:
                    raise PermissionDenied(
                        caller, owner, action,
                        f"矩阵 {owner} x {caller} = {PERMISSION_MATRIX[owner][caller]}，无 R/W，拒绝 {wt}")
    else:
        is_write = action != "read"
        if not resolve_access(access, is_write):
            raise PermissionDenied(caller, mcp, action,
                                   f"access={access} 不允许 {action}")
    return access


def knowledge_profile(caller: str) -> str:
    """M12 按 caller 切语料 profile。"""
    return KNOWLEDGE_PROFILE.get((caller or "").strip(), "none")


# ---------------------------------------------------------------------------
# 监护人令牌统一校验（BUG-36，2026-08-12）
# ---------------------------------------------------------------------------
# 此前 P1 his.py 与 P2 nutrition/core.py 各自维护一份 _token_matches 副本，
# P1 有 expires_at 过期校验、P2 没有 → 令牌轮换后旧令牌在 P2 仍有效（副本漂移）。
# 收敛到本函数作为唯一实现：恒定时间比对 + 过期 fail-closed + 无过期字段旧令牌向后兼容。
# 令牌状态库（guardian_tokens.json）与 P1 his.issue_guardian_token 共享同一路径。
GUARDIAN_TOKEN_STORE = "guardian_tokens.json"
GUARDIAN_TOKEN_DIR_ENV = "A207_GUARDIAN_TOKEN_DIR"


def _guardian_store_path() -> "os.PathLike[str]":
    base = os.environ.get(GUARDIAN_TOKEN_DIR_ENV)
    return resolve_state_path(GUARDIAN_TOKEN_STORE, base=base)


def _load_guardian_tokens() -> dict:
    p = _guardian_store_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        # S7 修复（2026-08-13）：损坏/读取失败必须 fail-closed 抛错（与 P1 his.py
        # _load_guardian_tokens 同语义）——此前静默返回 {} 会让**所有**家长绑定失效
        # 且无任何提示（verify_guardian_token 全部返回 False，运维无从知晓原因），
        # 而 his.py 端同样数据损坏会抛 RuntimeError → INTERNAL_ERROR。同一份数据的
        # 两种失败语义不一致，且"静默失效"更危险（家长端表现为莫名全部无法绑定）。
        raise RuntimeError(
            f"监护人令牌库 {GUARDIAN_TOKEN_STORE} 读取失败或 JSON 损坏，拒绝加载"
            f"（防止绑定状态静默失效），请检查磁盘/恢复备份: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(
            f"监护人令牌库 {GUARDIAN_TOKEN_STORE} 数据类型错误：期望 dict，"
            f"实际为 {type(data).__name__}，拒绝加载")
    return data


def verify_guardian_token(patient_id: str, guardian_token: str) -> bool:
    """校验监护人令牌是否有效（存在 + 未过期 + 恒定时间比对）。

    - 患儿无令牌 / 令牌过期 / 比对失败 → False（fail-closed）
    - S4 修复（2026-08-13）：**移除 fail-open 后门**——此前「缺条目/过期」分支
      返回 hmac.compare_digest("", guardian_token or "")，空串令牌会命中
      compare_digest("","")==True 被判定为「有效」；当前调用点有前置 if not
      guardian_token 拦截所以未触发，但策略函数自身必须是 fail-closed 的默认
      （未来任一调用点漏前置检查即构成越权绑定/写入口）。
    - 恒定时间比对仅用于「有条目且未过期」的令牌比对（防枚举时序差）；
      无条目/过期不比对（直接 False，无泄漏面）。
    - 无 expires_at 字段的旧令牌 → 向后兼容视为有效
    """
    entry = _load_guardian_tokens().get(patient_id)
    if not isinstance(entry, dict):
        # S4：缺条目直接 False——不得让空串令牌通过
        return False
    expires_at = entry.get("expires_at")
    if expires_at:
        try:
            parsed = datetime.fromisoformat(str(expires_at))
            # P1-8 修复（2026-08-13）：naive 时间戳（无时区，如 his.py 早期签发）与
            # timezone.utc aware 比较会抛 TypeError（此前只捕 ValueError → 家长读路径
            # 500）。naive 统一按 UTC 解释（签发方 his.py 用 timezone.utc 写入；历史
            # 数据无时区视为 UTC），兼容两种形态。
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            expired = datetime.now(timezone.utc) > parsed
        except (ValueError, TypeError):
            expired = True  # 无法解析的过期时间一律视为已过期（fail-closed）
        if expired:
            # S4：过期直接 False——不得让空串令牌通过
            return False
    stored = entry.get("token", "")
    # 无 stored token（数据异常）也 fail-closed，不比对空串
    if not stored:
        return False
    # P-B6 修复（2026-08-14）：guardian_token 非字符串（None/int/list）→ 直接 False——
    # 此前 compare_digest(stored, guardian_token or "") 对非字符串会抛 TypeError →
    # 家长读路径 500（本应 fail-closed 返回 False）。
    if not isinstance(guardian_token, str):
        return False
    return hmac.compare_digest(stored, guardian_token)
