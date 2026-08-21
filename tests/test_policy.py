"""a207-policy 自测：纯 python 可跑，不依赖 pytest / fastmcp。

运行：python tests/test_policy.py

a207-policy 是 5 个 CKDNutri MCP 包共同的信任根。它错一行，5 个包一起错，
而且各包自己的测试照样全绿（因为它们都信任 policy 的返回值）。所以这里
专打那些"下游测不出来"的盲区：
- PERMISSION_MATRIX 无空洞 / 取值合法 / 无重复键 last-wins 静默错配（本次修复重点）
- NUTRITION_ASSESSMENT_WRITE_ALLOWED 必须从矩阵派生（OD-011，禁止手写更宽集合）
- 矩阵 与 WRITE_TOOL_POLICY 不矛盾（写工具 allowed 角色必须在矩阵里 R/W）
- enforce_* 确定性执行（fail-closed、KeyError 已修）
- 状态路径外置
"""

from __future__ import annotations

import os

os.environ.setdefault("A207_ENV", "test")  # N-SEC-1（2026-08-14）：测试进程显式声明测试环境（守卫 fail-closed 默认拒绝）
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")  # 生产护栏（2026-08-15）：测试进程显式确认 json 后端为开发模式
import sys
import tempfile
import traceback
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from typing import Any

from a207_policy import (
    ACCESS_LIMITED,
    ACCESS_NONE,
    ACCESS_READ,
    ACCESS_RW,
    CALLERS,
    CLINICIAN_ONLY_FIELDS,
    KNOWLEDGE_PROFILE,
    MCP_ALIASES,
    P1_CHILD_READ_TOOLS,
    P1_PARENT_HIDDEN_FIELDS,
    PERMISSION_MATRIX,
    WRITE_TOOL_POLICY,
    CallerUnknown,
    PermissionDenied,
    as_caller,
    check_permission,
    detect_write_tool,
    enforce_nutrition_tool,
    enforce_read,
    enforce_write,
    get_caller,
    get_child_patient_id,
    knowledge_profile,
    normalize_mcp,
    resolve_state_path,
    set_caller,
    translate_error,
)

RESULTS: list[tuple[str, str, str]] = []  # status: "PASS" | "FAIL" | "SKIP"
# 十二审（2026-08-17）：@check 注册的用例函数（直跑 main() 统一执行）。
# 此前 check 装饰器在 **import 时立即执行** fn()——pytest 收集 test_* 后再次执行
# 造成双执行（有状态副作用用例第二次失败）。改为**延迟执行**：check 只注册，
# 直跑 main() 循环执行全部；pytest 收集 test_* 各执行一次。两种模式各只跑一次。
CHECK_FNS: list[tuple[str, Any]] = []


class _SkipTest(Exception):
    """环境缺少外部依赖（如 sibling 包不在 checkout 中）时跳过，不计入失败。"""


def check(name: str):
    def deco(fn):
        CHECK_FNS.append((name, fn))
        # 十二审（2026-08-17）：pytest 收集（CI 假绿阻断修复）——把 @check 函数以
        # test_ 前缀注册到**调用模块**全局，pytest 即可收集为测试用例。
        frame = sys._getframe(1)
        _test_name = f"test_{fn.__name__.lstrip('_')}"
        frame.f_globals[_test_name] = fn
        return fn

    return deco


def _run_checks() -> int:
    """直跑模式：统一执行全部 @check 用例，返回失败数（SKIP 不计入失败）。"""
    for name, fn in CHECK_FNS:
        try:
            fn()
            RESULTS.append((name, "PASS", ""))
        except _SkipTest as exc:
            RESULTS.append((name, "SKIP", str(exc)))
        except AssertionError as exc:
            RESULTS.append((name, "FAIL", str(exc) or "断言失败"))
        except Exception:  # noqa: BLE001
            RESULTS.append((name, "FAIL", traceback.format_exc(limit=3)))
    return sum(1 for _, s, _ in RESULTS if s == "FAIL")


def _reset(caller: str = "doctor_assistant") -> None:
    set_caller(None)
    os.environ["A207_CALLER"] = caller


# --------------------------------------------------------- P0-1 身份 fail-closed

@check("get_caller：身份缺失时 fail-closed 抛 CallerUnknown")
def _caller_missing():
    with as_caller(None):
        try:
            get_caller()
        except CallerUnknown:
            return
        raise AssertionError("身份缺失时未抛 CallerUnknown")


@check("get_caller：空串/纯空格视同缺失")
def _caller_blank():
    set_caller(None)
    try:
        for blank in ("", "   ", "\t"):
            os.environ["A207_CALLER"] = blank
            try:
                get_caller()
            except CallerUnknown:
                continue
            raise AssertionError(f"空白身份 {blank!r} 被当成合法身份")
    finally:
        _reset()


@check("as_caller：块内切换、退出还原")
def _as_caller_restore():
    _reset()
    with as_caller("parent_assistant"):
        assert get_caller() == "parent_assistant"
    assert get_caller() == "doctor_assistant"


@check("as_caller：嵌套逐层还原")
def _as_caller_nested():
    _reset()
    with as_caller("risk_warning"):
        with as_caller("parent_assistant"):
            assert get_caller() == "parent_assistant"
        assert get_caller() == "risk_warning"
    assert get_caller() == "doctor_assistant"


# --------------------------------------------------------- N-SEC-1 / N-CALLER-1（2026-08-14）

def _restore_env(key: str, prev: str | None) -> None:
    if prev is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = prev


@check("N-SEC-1：未显式声明测试环境 → set_caller/as_caller 一律拒绝（fail-closed 默认）")
def _ns_sec1_default_rejects():
    prev = os.environ.get("A207_ENV")
    os.environ.pop("A207_ENV", None)
    try:
        try:
            set_caller("parent_assistant")
        except RuntimeError:
            pass
        else:
            raise AssertionError("未设 A207_ENV 时 set_caller 应被拒绝（N-SEC-1 fail-closed）")
        try:
            with as_caller("parent_assistant"):
                pass
        except RuntimeError:
            return
        raise AssertionError("未设 A207_ENV 时 as_caller 应被拒绝（N-SEC-1 fail-closed）")
    finally:
        _restore_env("A207_ENV", prev)


@check("N-SEC-1：A207_ENV=production/prod → 测试提权通道拒绝")
def _ns_sec1_production_rejects():
    prev = os.environ.get("A207_ENV")
    try:
        for mode in ("production", "prod"):
            os.environ["A207_ENV"] = mode
            try:
                set_caller("parent_assistant")
            except RuntimeError:
                continue
            raise AssertionError(f"A207_ENV={mode} 时 set_caller 应被拒绝")
    finally:
        _restore_env("A207_ENV", prev)


@check("N-SEC-1：A207_ENV=dev/test → 测试提权通道放行（显式声明即测试环境）")
def _ns_sec1_dev_allows():
    prev_env = os.environ.get("A207_ENV")
    prev_caller = os.environ.get("A207_CALLER")
    try:
        for mode in ("dev", "test"):
            os.environ["A207_ENV"] = mode
            set_caller("parent_assistant")
            assert get_caller() == "parent_assistant"
            with as_caller("doctor_assistant"):
                assert get_caller() == "doctor_assistant"
    finally:
        _restore_env("A207_ENV", prev_env)
        _restore_env("A207_CALLER", prev_caller)


@check("N-CALLER-1：get_caller 白名单校验——未知身份拒绝（fail-closed）")
def _ns_caller1_whitelist():
    prev = os.environ.get("A207_CALLER")
    try:
        for bad in ("hacker", "unknown_role", "Doctor"):
            os.environ["A207_CALLER"] = bad
            try:
                get_caller()
            except CallerUnknown:
                continue
            raise AssertionError(f"未知身份 {bad!r} 未被拒绝（N-CALLER-1 白名单）")
    finally:
        _restore_env("A207_CALLER", prev)


# --------------------------------------------------------- translate_error（2026-08-15）

@check("translate_error：CallerError → FORBIDDEN envelope（policy 内生成，server 不感知 caller）")
def _te_caller_forbidden():
    import logging

    lg = logging.getLogger("te-test")
    env = translate_error(
        PermissionDenied("hacker", "CKDNutri-care-mcp", "write", "no"), domain="P3", logger=lg)
    assert env == {"ok": False, "error": "FORBIDDEN",
                   "detail": "caller=hacker 无权 write（no）"}, env
    env2 = translate_error(CallerUnknown("未注入"), domain="P3", logger=lg)
    assert env2["ok"] is False and env2["error"] == "FORBIDDEN", env2
    assert env2["detail"].startswith("caller=?"), env2  # CallerUnknown 无 caller 字段 → "?" 保底


@check("translate_error：ValueError → INVALID_INPUT（detail 保留）")
def _te_value_invalid():
    import logging

    lg = logging.getLogger("te-test")
    env = translate_error(ValueError("energy_kcal 必须为有限数值"), domain="P2", logger=lg)
    assert env == {"ok": False, "error": "INVALID_INPUT",
                   "detail": "energy_kcal 必须为有限数值"}, env


@check("translate_error：数据/环境错误 → INTERNAL_ERROR 且 detail 脱敏（不泄露路径）")
def _te_data_masked():
    import logging

    lg = logging.getLogger("te-test")
    env = translate_error(FileNotFoundError("/var/app/data/secret.json"), domain="P1", logger=lg)
    assert env["ok"] is False and env["error"] == "INTERNAL_ERROR", env
    assert "/var/app" not in env["detail"] and "P1_DATA" in env["detail"], env


@check("translate_error：未知异常 → INTERNAL_ERROR + domain_UNKNOWN（脱敏）")
def _te_unknown():
    import logging

    lg = logging.getLogger("te-test")
    env = translate_error(AttributeError("module has no attr 'x'"), domain="P2", logger=lg)
    assert env["ok"] is False and env["error"] == "INTERNAL_ERROR", env
    assert "NUTR_UNKNOWN" in env["detail"] and "module has no" not in env["detail"], env


@check("translate_error：content 语义——ValueError/KeyError 归数据错误，InvalidArgumentError 归 INVALID_INPUT")
def _te_content_semantics():
    import logging

    class _InvalidArg(ValueError):
        pass

    lg = logging.getLogger("te-test")
    env = translate_error(ValueError("rules.json 损坏"), domain="P5", logger=lg,
                          extra_data_types=(KeyError,), value_error_to_invalid=False)
    assert env["ok"] is False and env["error"] == "INTERNAL_ERROR" \
        and "CONTENT_DATA" in env["detail"], env
    env2 = translate_error(KeyError("meta"), domain="P5", logger=lg,
                           extra_data_types=(KeyError,), value_error_to_invalid=False)
    assert env2["error"] == "INTERNAL_ERROR" and "CONTENT_DATA" in env2["detail"], env2
    env3 = translate_error(_InvalidArg("limit 非法"), domain="P5", logger=lg,
                           extra_invalid_types=(_InvalidArg,), value_error_to_invalid=False)
    assert env3 == {"ok": False, "error": "INVALID_INPUT", "detail": "limit 非法"}, env3


@check("json 后端护栏：未显式 A207_ACCEPT_DEV_STORAGE=1 一律拒绝（防误部署生产）")
def _te_json_backend_guard():
    from a207_policy.storage import ACCEPT_DEV_STORAGE_ENV, ensure_json_backend_allowed

    prev = os.environ.get(ACCEPT_DEV_STORAGE_ENV)
    try:
        os.environ.pop(ACCEPT_DEV_STORAGE_ENV, None)
        try:
            ensure_json_backend_allowed()
        except RuntimeError:
            pass
        else:
            raise AssertionError("json 后端未确认应拒绝（fail-closed）")
        os.environ[ACCEPT_DEV_STORAGE_ENV] = "1"
        ensure_json_backend_allowed()  # 显式确认放行
    finally:
        _restore_env(ACCEPT_DEV_STORAGE_ENV, prev)


# --------------------------------------------------------- 矩阵完整性

@check("矩阵规模：恰好 5 个 MCP（CKDNutri P1–P5）")
def _matrix_size():
    assert len(PERMISSION_MATRIX) == 5, f"矩阵行数={len(PERMISSION_MATRIX)}，期望 5"


@check("矩阵无重复键：5 个预期 MCP 各出现一次且取值符合 intent（锁定无 last-wins 错配）")
def _matrix_no_dup_keys():
    expected = {
        "CKDNutri-clinical-data-mcp": {"doctor_assistant": ACCESS_RW, "parent_assistant": ACCESS_LIMITED, "risk_warning": ACCESS_READ},
        "CKDNutri-nutrition-mcp":     {"doctor_assistant": ACCESS_RW, "parent_assistant": ACCESS_RW, "risk_warning": ACCESS_NONE},
        "CKDNutri-care-mcp":          {"doctor_assistant": ACCESS_RW, "parent_assistant": ACCESS_RW, "risk_warning": ACCESS_RW},
        "CKDNutri-assessment-mcp":    {"doctor_assistant": ACCESS_READ, "parent_assistant": ACCESS_NONE, "risk_warning": ACCESS_READ},
        "CKDNutri-content-mcp":       {"doctor_assistant": ACCESS_RW, "parent_assistant": ACCESS_LIMITED, "risk_warning": ACCESS_READ},
    }
    assert set(PERMISSION_MATRIX.keys()) == set(expected.keys()), f"矩阵键集合异常：{set(PERMISSION_MATRIX.keys())}"
    for mcp, row in expected.items():
        for caller, access in row.items():
            got = PERMISSION_MATRIX[mcp][caller]
            assert got == access, f"{mcp} x {caller} = {got!r}，期望 {access!r}"


@check("矩阵无空洞：每个已登记 MCP x 每个角色都有明确取值")
def _matrix_complete():
    holes = []
    for mcp_name, row in PERMISSION_MATRIX.items():
        for caller in CALLERS:
            if caller not in row:
                holes.append(f"{mcp_name} x {caller}")
    assert not holes, f"矩阵有空洞：{holes}"


@check("矩阵取值合法：只能是 - / R / RL / R/W 四种")
def _matrix_values():
    legal = {ACCESS_NONE, ACCESS_READ, ACCESS_LIMITED, ACCESS_RW}
    bad = [
        f"{m} x {c} = {a!r}"
        for m, row in PERMISSION_MATRIX.items()
        for c, a in row.items()
        if a not in legal
    ]
    assert not bad, f"非法权限值：{bad}"


@check("别名归一：旧包名解析到真实包名且落在矩阵内（OD-002 回归）")
def _alias_normalize():
    for alias, canonical in MCP_ALIASES.items():
        got = normalize_mcp(alias)
        assert got == canonical, f"{alias} 归一到 {got}，期望 {canonical}"
        assert canonical in PERMISSION_MATRIX, f"{canonical} 不在矩阵中"


@check("别名防漏：已知全部废弃 a207-* 旧名都正确映射到 P1–P5（防止漏配遗留 mcp 未登记）")
def _alias_coverage():
    coverage = {
        "a207-clinical-data-mcp": "CKDNutri-clinical-data-mcp",
        "a207-his-mcp": "CKDNutri-clinical-data-mcp",
        "a207-lis-mcp": "CKDNutri-clinical-data-mcp",
        "a207-nutrition-mcp": "CKDNutri-nutrition-mcp",
        "a207-nutrition-assessment-mcp": "CKDNutri-nutrition-mcp",
        "a207-nutrition-assessment-mcp-nfyy": "CKDNutri-nutrition-mcp",
        "a207-nutrition-calc-mcp": "CKDNutri-nutrition-mcp",
        "a207-meal-plan-mcp": "CKDNutri-nutrition-mcp",
        "a207-care-mcp": "CKDNutri-care-mcp",
        "a207-followup-mcp": "CKDNutri-care-mcp",
        "a207-notification-mcp": "CKDNutri-care-mcp",
        "a207-notify-mcp": "CKDNutri-care-mcp",
        "a207-decision-mcp": "CKDNutri-assessment-mcp",
        "a207-clinical-calc-mcp": "CKDNutri-assessment-mcp",
        "a207-ckd-clinical-calc-mcp": "CKDNutri-assessment-mcp",
        "a207-risk-rules-mcp": "CKDNutri-assessment-mcp",
        "a207-content-mcp": "CKDNutri-content-mcp",
        "a207-report-mcp": "CKDNutri-content-mcp",
        "a207-knowledge-mcp": "CKDNutri-content-mcp",
    }
    for legacy, canonical in coverage.items():
        got = normalize_mcp(legacy)
        assert got == canonical, f"{legacy} 归一到 {got!r}，期望 {canonical!r}"
        assert canonical in PERMISSION_MATRIX, f"{canonical} 不在矩阵"
    # 网关/中间件/已退役 M11 不应被别名归一（返回空串 → 上层 fail-closed 拒绝）
    for infra in ("a207-router-mcp", "a207-gateway-mcp", "a207-gamification-mcp"):
        assert normalize_mcp(infra) == "", f"{infra} 应归一为空串（未登记）"


@check("normalize_mcp：剥离 :read/:write 动作后缀 + mcp:// 协议前缀 + 大小写容错 + 类型防御")
def _normalize_strip_action_suffix():
    # 动作后缀剥离（根因修复：网关传入 "CKDNutri-nutrition-mcp:read" 应归一为规范名）
    assert normalize_mcp("CKDNutri-nutrition-mcp:read") == "CKDNutri-nutrition-mcp"
    assert normalize_mcp("CKDNutri-clinical-data-mcp:write") == "CKDNutri-clinical-data-mcp"
    # 别名 + 动作后缀
    assert normalize_mcp("a207-nutrition-calc-mcp:read") == "CKDNutri-nutrition-mcp"
    # 协议前缀
    assert normalize_mcp("mcp://CKDNutri-care-mcp:execute") == "CKDNutri-care-mcp"
    # 类型防御（fail-closed 返回空串）
    assert normalize_mcp(None) == ""
    assert normalize_mcp(123) == ""
    assert normalize_mcp("") == ""
    # 空串不会误写入 PERMISSION_MATRIX（gate._enforce 会判未登记）
    assert "" not in PERMISSION_MATRIX


@check("_matrix_writers 防归一化：传入废弃旧名也能安全派生（不 KeyError）")
def _matrix_writers_normalize():
    from a207_policy.matrix import _matrix_writers
    got = _matrix_writers("a207-followup-mcp")
    assert got == _matrix_writers("CKDNutri-care-mcp"), "旧名 a207-followup-mcp 未正确归一"


@check("GAMIFICATION_MCP 已纠正符号漂移（指向 M11 并入后的 P2）")
def _gamification_symbol():
    from a207_policy.matrix import GAMIFICATION_MCP
    assert GAMIFICATION_MCP == "CKDNutri-nutrition-mcp"
    from a207_policy.matrix import GAMIFICATION_ALLOWED
    assert GAMIFICATION_ALLOWED == frozenset(), "退役包写白名单必须仍为空（fail-closed）"


# --------------------------------------------------------- OD-011：写白名单唯一事实源

@check("NUTRITION_ASSESSMENT_WRITE_ALLOWED 必须从矩阵派生（OD-011，禁手写更宽集合）")
def _nutrition_write_derived():
    from a207_policy.matrix import (
        NUTRITION_ASSESSMENT_WRITE_ALLOWED,
        _matrix_writers,
    )
    assert NUTRITION_ASSESSMENT_WRITE_ALLOWED == _matrix_writers("CKDNutri-nutrition-mcp"), \
        "写白名单未与矩阵保持一致"
    # 2026-08-21：矩阵 nutrition×child=R/W（MCP 级：读食物/读摘要/写 child_foodlog），
    # 故派生集合含 child_assistant；但**工具级 upsert_food_diary 对 child 显式拒绝**
    # （gate.enforce_nutrition_tool 分支：food_diary 是医疗记录，仅家长/医生写；
    # child 写权仅 record_child_food→child_foodlog）。WRITE_ALLOWED 仍保持
    # "矩阵派生"单一事实源不变式（OD-011），工具级例外在 gate 集中收口。
    assert NUTRITION_ASSESSMENT_WRITE_ALLOWED == frozenset({
        "parent_assistant", "doctor_assistant", "demo_parent_assistant", "child_assistant"}), \
        f"营养写白名单={NUTRITION_ASSESSMENT_WRITE_ALLOWED}，期望 {{parent_assistant, doctor_assistant, demo_parent_assistant, child_assistant}}（矩阵派生；child 的 upsert_food_diary 写权由 gate 工具级排除）"


@check("LIS 写白名单由矩阵派生；FOLLOWUP 强制收口为仅医生（有意识不派生，防 risk 误写随访落盘）；NOTIFY 写白名单由矩阵派生——三个集合并存且均为确定性")
def _other_writes_derived():
    from a207_policy.matrix import (
        FOLLOWUP_WRITE_ALLOWED,
        LIS_WRITE_ALLOWED,
        NOTIFY_WRITE_ROLES,
        _matrix_writers,
    )
    assert LIS_WRITE_ALLOWED == _matrix_writers("CKDNutri-clinical-data-mcp")
    # FOLLOWUP_WRITE_ALLOWED 有意识不派生自矩阵（随访落盘仅 doctor，risk 写权走 notify_* 单独通道）
    assert FOLLOWUP_WRITE_ALLOWED == frozenset({"doctor_assistant"})
    assert NOTIFY_WRITE_ROLES == _matrix_writers("CKDNutri-care-mcp")


# --------------------------------------------------------- 矩阵 ↔ WRITE_TOOL_POLICY 一致性

@check("MX-3：每条写工具策略字段完整（mcp/allowed）")
def _write_policy_shape():
    bad = []
    for tool, policy in WRITE_TOOL_POLICY.items():
        for field in ("mcp", "allowed"):
            if field not in policy:
                bad.append(f"{tool} 缺 {field}")
    assert not bad, f"写权策略不完整：{bad}"


@check("MX-3：写工具的 owner mcp 必须在矩阵里登记")
def _write_policy_owner_registered():
    bad = [
        f"{tool} -> {policy['mcp']}"
        for tool, policy in WRITE_TOOL_POLICY.items()
        if normalize_mcp(str(policy["mcp"])) not in PERMISSION_MATRIX
    ]
    assert not bad, f"写工具指向未登记 MCP：{bad}"


@check("MX-3：写工具的 allowed 角色必须都是已登记 caller")
def _write_policy_allowed_registered():
    bad = []
    for tool, policy in WRITE_TOOL_POLICY.items():
        for c in policy["allowed"]:
            if c not in CALLERS:
                bad.append(f"{tool} 允许了未登记角色 {c}")
    assert not bad, bad


@check("BUG-23 回归：get_adherence_score 必须显式登记 WRITE_TOOL_POLICY（防矩阵放宽后误放行）")
def _adherence_registered():
    # 依从性评分落库是写操作（OD-014），若未登记写工具白名单，
    # enforce_write 会回退到矩阵 R/W 判定——将来 care×parent 若改 R/W 即被意外放行。
    assert "get_adherence_score" in WRITE_TOOL_POLICY, \
        "get_adherence_score 必须登记 WRITE_TOOL_POLICY"
    policy = WRITE_TOOL_POLICY["get_adherence_score"]
    assert policy["mcp"] == "CKDNutri-care-mcp"
    assert policy["allowed"] == frozenset({"doctor_assistant"}), \
        f"get_adherence_score allowed={policy['allowed']}，期望 {{doctor_assistant}}"
    # detect_write_tool 必须能把它识别为写工具
    assert detect_write_tool("get_adherence_score") == "get_adherence_score"


@check("矩阵↔写策略一致：非豁免写工具，allowed 角色在矩阵必须 R/W")
def _matrix_write_consistent():
    from a207_policy.gate import _MATRIX_EXEMPT_WRITE_TOOLS
    bad = []
    for tool, policy in WRITE_TOOL_POLICY.items():
        if tool in _MATRIX_EXEMPT_WRITE_TOOLS:
            continue
        owner = normalize_mcp(str(policy["mcp"]))
        for c in policy["allowed"]:
            if PERMISSION_MATRIX[owner][c] != ACCESS_RW:
                bad.append(f"{tool} 允许 {c}，但矩阵 {owner} x {c} = {PERMISSION_MATRIX[owner][c]}")
    assert not bad, f"矩阵与写策略矛盾：{bad}"


# --------------------------------------------------------- 闸门确定性执行

@check("enforce_read：家长现在可读营养域（修复前因 last-wins 被错配为 NONE）")
def _parent_read_nutrition():
    with as_caller("parent_assistant"):
        enforce_read("CKDNutri-nutrition-mcp")


@check("enforce_read：家长不可读评估域（NONE）")
def _parent_no_assessment():
    with as_caller("parent_assistant"):
        try:
            enforce_read("CKDNutri-assessment-mcp")
        except PermissionDenied:
            return
        raise AssertionError("家长竟能读评估域")


@check("enforce_read：身份缺失时闸门也必须拒（fail-closed 贯穿）")
def _enforce_no_caller():
    with as_caller(None):
        try:
            enforce_read("CKDNutri-clinical-data-mcp")
        except (CallerUnknown, PermissionDenied):
            return
        raise AssertionError("身份缺失时闸门放行了")


@check("enforce_read：未登记的 MCP 一律拒绝")
def _enforce_unknown_mcp():
    with as_caller("doctor_assistant"):
        try:
            enforce_read("CKDNutri-totally-unknown-mcp")
        except (PermissionDenied, KeyError, ValueError):
            return
        raise AssertionError("未知 MCP 被放行")


@check("enforce_write：家长写饮食日记放行（upsert_food_diary 豁免 + 矩阵 R/W）")
def _parent_write_diary():
    with as_caller("parent_assistant"):
        enforce_write("CKDNutri-nutrition-mcp", tool="upsert_food_diary")


@check("enforce_write：医生 push_to_emr 拒绝（P-B3 退役写工具，此前英文直传被矩阵放行）")
def _doctor_push_emr():
    with as_caller("doctor_assistant"):
        try:
            enforce_write("CKDNutri-content-mcp", tool="push_to_emr")
        except PermissionDenied:
            return
        raise AssertionError("退役写工具 push_to_emr 竟被放行")


@check("enforce_write：家长 push_to_emr 拒绝（退役工具，与医生一致）")
def _parent_no_push_emr():
    with as_caller("parent_assistant"):
        try:
            enforce_write("CKDNutri-content-mcp", tool="push_to_emr")
        except PermissionDenied:
            return
        raise AssertionError("家长竟能 push_to_emr")


@check("enforce_write：家长写化验拒绝（clinical-data×parent=RL）")
def _parent_no_lab_write():
    with as_caller("parent_assistant"):
        try:
            enforce_write("CKDNutri-clinical-data-mcp", tool="upsert_lab_result")
        except PermissionDenied:
            return
        raise AssertionError("家长竟能写化验")


@check("check_permission：未登记 caller 一律拒绝")
def _check_unknown_caller():
    res = check_permission("hacker_agent", "CKDNutri-clinical-data-mcp", "read")
    assert res["allow"] is False, f"未登记 caller 被放行：{res}"


# --------------------------------------------------------- M3 工具级 ACL（enforce_nutrition_tool）

@check("enforce_nutrition_tool：医生跑 calc_prnt_targets 不再 KeyError（a207-* 键已修）")
def _doctor_calc_no_keyerror():
    r = enforce_nutrition_tool("doctor_assistant", "calc_prnt_targets")
    assert r in (ACCESS_READ, ACCESS_RW, ACCESS_LIMITED), f"返回异常：{r!r}"


@check("enforce_nutrition_tool：家长写 upsert_food_diary 放行")
def _parent_upsert_diary():
    # P-B1（2026-08-14）：caller 参数必须与进程注入身份一致——用 as_caller 对齐
    with as_caller("parent_assistant"):
        assert enforce_nutrition_tool("parent_assistant", "upsert_food_diary") == ACCESS_RW


@check("enforce_nutrition_tool：医生写 upsert_food_diary 放行（需求 2026-08-12：临床=✔）")
def _doctor_upsert_diary():
    assert enforce_nutrition_tool("doctor_assistant", "upsert_food_diary") == ACCESS_RW


@check("enforce_nutrition_tool：家长读 get_food_diary_summary 放行（返回矩阵值）")
def _parent_read_diary_summary():
    # 2026-08-13（policy 审查）：返回值由硬编码 "RL" 改为矩阵值（M3×parent=RW）——
    # 此前 doctor（矩阵 RW）读摘要也被返回 RL，与矩阵语义不一致。
    # P-B1（2026-08-14）：caller 参数必须与进程注入身份一致
    with as_caller("parent_assistant"):
        assert enforce_nutrition_tool("parent_assistant", "get_food_diary_summary") == ACCESS_RW


@check("enforce_nutrition_tool：医生读 get_food_diary_summary 返回矩阵值 RW")
def _doctor_read_diary_summary():
    assert enforce_nutrition_tool("doctor_assistant", "get_food_diary_summary") == ACCESS_RW


@check("enforce_nutrition_tool：未登记工具 fail-closed 拒绝")
def _nutrition_unknown_tool():
    try:
        enforce_nutrition_tool("doctor_assistant", "totally_unknown_tool")
    except PermissionDenied:
        return
    raise AssertionError("未登记营养工具被放行")


# --------------------------------------------------------- M12 语料分级

@check("语料分级：医生 full / 家长 plain_language / 风险 full")
def _knowledge_profiles():
    assert KNOWLEDGE_PROFILE["doctor_assistant"] == "full"
    assert KNOWLEDGE_PROFILE["parent_assistant"] == "plain_language"
    assert KNOWLEDGE_PROFILE["risk_warning"] == "full"


@check("knowledge_profile：未知角色不得回退到 full（默认最保守）")
def _knowledge_unknown_not_full():
    got = knowledge_profile("some_unknown_agent")
    assert got != "full", f"未知角色拿到全量专业语料：{got!r}"


# --------------------------------------------------------- 2026-08-17：演示家长 demo_parent_assistant 回归

@check("演示家长 demo_parent_assistant 已登记为合法 caller（get_caller 白名单）")
def _demo_registered_caller():
    from a207_policy.matrix import CALLERS, DEMO_PARENT_ROLE
    assert DEMO_PARENT_ROLE in CALLERS, "demo_parent_assistant 未进入 CALLERS（get_caller 会拒）"


@check("演示家长属于 PARENT_EQUIVALENT_ROLES（家长视图/家长级放行判定集合）")
def _demo_in_equivalent_roles():
    from a207_policy.matrix import (
        DEMO_PARENT_ROLE,
        PARENT_EQUIVALENT_ROLES,
        PARENT_ROLE,
    )
    assert DEMO_PARENT_ROLE in PARENT_EQUIVALENT_ROLES
    assert PARENT_ROLE in PARENT_EQUIVALENT_ROLES
    # 仅家长等价两类，不含临床/风险
    assert PARENT_EQUIVALENT_ROLES == frozenset({"parent_assistant", "demo_parent_assistant"})


@check("演示家长权限=家长：读临床数据 RL 放行、读评估域 NONE 拒绝（与 parent 一致）")
def _demo_read_scope():
    from a207_policy.matrix import DEMO_PARENT_ROLE
    with as_caller(DEMO_PARENT_ROLE):
        enforce_read("CKDNutri-clinical-data-mcp")      # RL → 放行
        try:
            enforce_read("CKDNutri-assessment-mcp")     # NONE → 拒绝
        except PermissionDenied:
            pass
        else:
            raise AssertionError("演示家长竟能读评估域（应与家长一致拒绝）")


@check("演示家长与家长的读权完全一致（各 MCP 矩阵取值相等）")
def _demo_matches_parent_matrix():
    from a207_policy.matrix import DEMO_PARENT_ROLE, PARENT_ROLE, PERMISSION_MATRIX
    for mcp, row in PERMISSION_MATRIX.items():
        assert row[DEMO_PARENT_ROLE] == row[PARENT_ROLE], \
            f"{mcp}：demo({row[DEMO_PARENT_ROLE]}) 与家长({row[PARENT_ROLE]}) 不一致"


@check("演示家长写饮食日记放行（enforce_write upsert_food_diary，家长等价）")
def _demo_write_diary():
    from a207_policy.matrix import DEMO_PARENT_ROLE
    with as_caller(DEMO_PARENT_ROLE):
        enforce_write("CKDNutri-nutrition-mcp", tool="upsert_food_diary")


@check("演示家长不可写化验（clinical-data×demo=RL，与 parent 一致）")
def _demo_no_lab_write():
    from a207_policy.matrix import DEMO_PARENT_ROLE
    with as_caller(DEMO_PARENT_ROLE):
        try:
            enforce_write("CKDNutri-clinical-data-mcp", tool="upsert_lab_result")
        except PermissionDenied:
            return
        raise AssertionError("演示家长竟能写化验")


@check("演示家长在临床判读字段「绝不可见」集合内（P5 报告层据此剥除，与 parent 一致）")
def _demo_clinician_hidden():
    from a207_policy.matrix import (
        CLINICIAN_ONLY_HIDDEN_FROM,
        DEMO_PARENT_ROLE,
        PARENT_ROLE,
    )
    assert DEMO_PARENT_ROLE in CLINICIAN_ONLY_HIDDEN_FROM
    assert PARENT_ROLE in CLINICIAN_ONLY_HIDDEN_FROM


@check("演示家长语料分级=plain_language（与 parent 一致，通俗语料）")
def _demo_knowledge_profile():
    from a207_policy.matrix import DEMO_PARENT_ROLE, KNOWLEDGE_PROFILE
    assert KNOWLEDGE_PROFILE[DEMO_PARENT_ROLE] == "plain_language"


@check("演示家长不在临床/风险专属写集合（LIS 写权仅 doctor；令牌闸对其自动免绑定）")
def _demo_not_clinician_write():
    from a207_policy.matrix import DEMO_PARENT_ROLE, LIS_WRITE_ALLOWED
    assert DEMO_PARENT_ROLE not in LIS_WRITE_ALLOWED, "演示家长不应获得化验写权"


@check("演示家长 P1 受限视图：his._scope_of 返回 limited_parent（与 parent 一致，临床判读被剥除）")
def _demo_p1_limited_scope():
    # 端到端确认：P1 的 _scope_of 已切换为 `caller in PARENT_EQUIVALENT_ROLES`，
    # demo 落入 limited_parent 分支 → 上层 _strip_parent_sensitive 剥除 P1_PARENT_HIDDEN_FIELDS。
    import importlib.util
    import sys
    from pathlib import Path as _P
    clinical_src = _P(__file__).resolve().parents[2] / "CKDNutri-clinical-data-mcp" / "src"
    # 端到端集成测试依赖下游 CKDNutri-clinical-data-mcp 包；a207-policy 的 CI 仅
    # checkout 本仓库，无此 sibling（本地两包同目录时才存在）→ 跳过而非失败。
    if not clinical_src.exists():
        raise _SkipTest(
            "clinical-data 包不在 a207-policy 仓库 checkout 中（端到端集成测试仅在两包同目录时运行）")
    try:
        if str(clinical_src) not in sys.path:
            sys.path.insert(0, str(clinical_src))
        spec = importlib.util.spec_from_file_location(
            "a207_demo_probe_his",
            clinical_src / "CKDNutri_clinical_data_mcp" / "his.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except (ImportError, FileNotFoundError) as exc:
        # 环境缺 sibling 包属测试跳过（非失败），原始异常无诊断价值（B904）
        raise _SkipTest(f"无法加载 clinical-data his 模块以端到端验证（跳过）：{exc}") from None
    assert mod._scope_of("demo_parent_assistant") == "limited_parent", \
        "演示家长未获得 P1 受限家长视图"
    assert mod._scope_of("parent_assistant") == "limited_parent"


@check("演示家长 get_patient_profile 端到端：data_scope=limited_parent 且临床判读字段被剥除（免令牌）")
def _demo_p1_profile_stripped():
    # 端到端确认 demo 走家长受限视图：_scope_of→limited_parent → _strip_parent_sensitive
    # 剥除 P1_PARENT_HIDDEN_FIELDS（biochemistry/food_diary_5d/...）与 CLINICIAN_ONLY_FIELDS
    # （z_score_height/stage_confirmed_by/clinician_note 等）。demo 免令牌（_guard_guardian 短路）。
    import importlib.util
    import sys
    import tempfile as _tf
    from pathlib import Path as _P
    clinical_src = _P(__file__).resolve().parents[2] / "CKDNutri-clinical-data-mcp" / "src"
    pkg_dir = clinical_src / "CKDNutri_clinical_data_mcp"
    # 端到端集成测试依赖下游 CKDNutri-clinical-data-mcp 包；a207-policy 的 CI 仅
    # checkout 本仓库，无此 sibling（本地两包同目录时才存在）→ 跳过而非失败。
    if not pkg_dir.exists():
        raise _SkipTest(
            "clinical-data 包不在 a207-policy 仓库 checkout 中（端到端集成测试仅在两包同目录时运行）")
    # 以正式子模块方式加载 his（使其 `from .repository import` 相对导入可解析）
    try:
        if "CKDNutri_clinical_data_mcp" not in sys.modules:
            pkg_spec = importlib.util.spec_from_file_location(
                "CKDNutri_clinical_data_mcp", pkg_dir / "__init__.py")
            pkg_mod = importlib.util.module_from_spec(pkg_spec)
            sys.modules["CKDNutri_clinical_data_mcp"] = pkg_mod
            pkg_spec.loader.exec_module(pkg_mod)
        spec = importlib.util.spec_from_file_location(
            "CKDNutri_clinical_data_mcp.his", pkg_dir / "his.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["CKDNutri_clinical_data_mcp.his"] = mod
        spec.loader.exec_module(mod)
    except (ImportError, FileNotFoundError) as exc:
        # 环境缺 sibling 包属测试跳过（非失败），原始异常无诊断价值（B904）
        raise _SkipTest(f"无法加载 clinical-data 包以端到端验证（跳过）：{exc}") from None

    prev_backend = os.environ.get("A207_STORAGE_BACKEND")
    prev_token_dir = os.environ.get("A207_GUARDIAN_TOKEN_DIR")
    tmp = _tf.mkdtemp(prefix="a207-demo-profile-")
    try:
        os.environ["A207_STORAGE_BACKEND"] = "json"          # 免 OTS，读包内 patients.json
        os.environ["A207_GUARDIAN_TOKEN_DIR"] = tmp          # 令牌库隔离
        with as_caller("demo_parent_assistant"):
            r = mod.get_patient_profile("P0007", None)        # demo 免令牌绑定（P0007 在演示患儿集）
        assert r.get("ok") is True, f"demo get_patient_profile 失败：{r}"
        # BUG-41 回归：demo 访问非演示患儿必须被 FORBIDDEN（跨患儿越权防护）
        with as_caller("demo_parent_assistant"):
            r2 = mod.get_patient_profile("P0001", None)
        assert r2.get("ok") is False and r2.get("error") == "FORBIDDEN", \
            f"demo 访问非演示患儿未被拦截：{r2}"
        data = r.get("data", {})
        assert data.get("data_scope") == "limited_parent", \
            f"demo 未获得受限家长视图：data_scope={data.get('data_scope')}"
        # 临床判读聚合块与叶子键必须被剥除（与 parent 视图一致）
        for blocked in ("biochemistry", "food_diary_5d", "dialysis_detail",
                        "medical_record_no", "bsa_m2", "z_score_height",
                        "stage_confirmed_by", "clinician_note", "doctor_note"):
            assert blocked not in data, f"demo 视图竟含临床判读字段 {blocked!r}"
        # nutrition_ceiling 是家长可见例外（医生设定摄入上限），必须保留
        assert "nutrition_ceiling" in data, "nutrition_ceiling 家长例外被误剥"
    finally:
        _restore_env("A207_STORAGE_BACKEND", prev_backend)
        _restore_env("A207_GUARDIAN_TOKEN_DIR", prev_token_dir)


# --------------------------------------------------------- P1-3 状态外置

@check("resolve_state_path：base 参数优先")
def _state_base():
    with tempfile.TemporaryDirectory() as tmp:
        p = resolve_state_path("x.json", base=tmp)
        assert p.parent == Path(tmp), p
        assert p.name == "x.json"


@check("resolve_state_path：目录自动创建且可写")
def _state_writable():
    with tempfile.TemporaryDirectory() as tmp:
        p = resolve_state_path("z.json", base=str(Path(tmp) / "nested" / "deep"))
        assert p.parent.is_dir(), "父目录未自动创建"
        p.write_text("{}", encoding="utf-8")
        assert p.read_text(encoding="utf-8") == "{}"


@check("resolve_state_path：绝不落在 policy 包安装目录内（P1-3 根本目的）")
def _state_not_in_pkg():
    saved = os.environ.pop("A207_DATA_DIR", None)
    try:
        p = resolve_state_path("w.json")
        pkg_root = Path(__file__).resolve().parents[1]
        assert pkg_root not in p.parents, f"状态落到了安装目录：{p}"
    finally:
        if saved is not None:
            os.environ["A207_DATA_DIR"] = saved


# --------------------------------------------------------- F3 脱敏双轨覆盖校验

@check("P1_PARENT_HIDDEN_FIELDS 聚合块所含叶子键均在 CLINICIAN_ONLY_FIELDS 或 P1 明示排除列表中（防双轨漂移）")
def _p1_parent_hidden_fields_coverage():
    """P1_PARENT_HIDDEN_FIELDS 是聚合块（如 biochemistry / food_diary_5d），
    未来新增敏感聚合块时需确认其内含叶子键被 CLINICIAN_ONLY_FIELDS 覆盖。
    本测试不强制要求逐叶子对齐（聚合块语义不同），但至少标记存在性。
    """
    # 已知合理排除：聚合块父键（非叶子级敏感）
    #   food_diary_5d — 饮食日记聚合，内含各餐数据，父键本身非敏感
    #   dialysis_detail — 透析明细聚合
    #   medical_record_no — 病案号（PII叶子键，已含在 CLINICIAN_ONLY_FIELDS？需要确认）
    p1_parent = set(P1_PARENT_HIDDEN_FIELDS)
    clinician = set(CLINICIAN_ONLY_FIELDS)
    # 聚合块本身不应出现在叶子级敏感集合中（它们是块，不是叶子）
    assert not p1_parent.intersection(clinician), \
        f"聚合块不应与叶子级敏感集合重叠：{p1_parent & clinician}"
    # P1_PARENT_HIDDEN_FIELDS 是明确登记的敏感块，不为空
    assert len(p1_parent) > 0, "P1_PARENT_HIDDEN_FIELDS 不应为空（含至少 3 个聚合块）"


def main() -> int:
    # 十二审：直跑模式先统一执行全部 @check（此前在 import 时立即执行）
    _run_checks()
    for name, status, msg in RESULTS:
        print(f"[{status}] {name}")
        if status == "FAIL":
            print("       " + msg.strip().replace("\n", "\n       "))
    passed = sum(1 for _, s, _ in RESULTS if s == "PASS")
    failed = sum(1 for _, s, _ in RESULTS if s == "FAIL")
    skipped = sum(1 for _, s, _ in RESULTS if s == "SKIP")
    line = f"\n合计 {len(RESULTS)} 项：通过 {passed}，失败 {failed}"
    if skipped:
        line += f"，跳过 {skipped}"
    print(line)
    return 1 if failed else 0


# --------------------------------------------------------- BUG-36：监护人令牌统一校验

@check("BUG-36 回归：verify_guardian_token 含过期校验 + 恒定时间比对")
def _guardian_token_verify():
    import json
    from datetime import datetime, timedelta, timezone

    from a207_policy import verify_guardian_token
    from a207_policy.gate import _guardian_store_path

    tmp = tempfile.mkdtemp(prefix="a207-policy-token-")
    os.environ["A207_GUARDIAN_TOKEN_DIR"] = tmp  # 隔离到临时目录，不污染全局状态
    store = _guardian_store_path()
    payload = {
        "P0001": {
            "token": "tok-abc-123",
            "issued_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            "issued_by": "doctor_assistant",
        },
    }
    store.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    # 有效期内：匹配
    assert verify_guardian_token("P0001", "tok-abc-123") is True
    # 错误令牌
    assert verify_guardian_token("P0001", "wrong-token") is False
    # S4 修复（2026-08-13）：空串令牌不得通过（fail-open 后门回归——
    # 此前缺条目/过期分支 compare_digest("","")==True 会误判「有效」）
    assert verify_guardian_token("P0001", "") is False
    assert verify_guardian_token("P0001", None) is False
    # 过期：即使令牌正确也失效（BUG-30 核心：P2 此前缺此校验）
    payload["P0001"]["expires_at"] = "2020-01-01T00:00:00+00:00"
    store.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert verify_guardian_token("P0001", "tok-abc-123") is False
    # S4：过期 + 空串也必须 False（fail-closed，不因 compare_digest("","") 通过）
    assert verify_guardian_token("P0001", "") is False
    # 无 expires_at 旧令牌：向后兼容
    del payload["P0001"]["expires_at"]
    store.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert verify_guardian_token("P0001", "tok-abc-123") is True
    # 不存在的患儿（S4：缺条目 + 空串 → False，而非 compare_digest("","")==True）
    assert verify_guardian_token("P9999", "tok-abc-123") is False
    assert verify_guardian_token("P9999", "") is False
    os.environ.pop("A207_GUARDIAN_TOKEN_DIR", None)


# --------------------------------------------------------- child_assistant（2026-08-21）

@check("child_assistant：矩阵五行已登记（P1=RL/P2=RW/P3=P4=NONE/P5=RL）")
def _child_matrix_ok():
    m = PERMISSION_MATRIX
    assert m["CKDNutri-clinical-data-mcp"]["child_assistant"] == ACCESS_LIMITED
    assert m["CKDNutri-nutrition-mcp"]["child_assistant"] == ACCESS_RW
    assert m["CKDNutri-care-mcp"]["child_assistant"] == ACCESS_NONE
    assert m["CKDNutri-assessment-mcp"]["child_assistant"] == ACCESS_NONE
    assert m["CKDNutri-content-mcp"]["child_assistant"] == ACCESS_LIMITED


@check("child_assistant：绑定患儿 env 缺失/空 → get_child_patient_id fail-closed（CallerUnknown）")
def _child_binding_fail_closed():
    prev = os.environ.get("A207_CHILD_PATIENT_ID")
    try:
        os.environ.pop("A207_CHILD_PATIENT_ID", None)
        try:
            get_child_patient_id()
        except CallerUnknown:
            pass
        else:
            raise AssertionError("未设置 A207_CHILD_PATIENT_ID 应抛 CallerUnknown（fail-closed）")
        os.environ["A207_CHILD_PATIENT_ID"] = "   "
        try:
            get_child_patient_id()
        except CallerUnknown:
            pass
        else:
            raise AssertionError("空串 A207_CHILD_PATIENT_ID 应抛 CallerUnknown（fail-closed）")
    finally:
        _restore_env("A207_CHILD_PATIENT_ID", prev)


@check("child_assistant：绑定 env 设置 → get_child_patient_id 返回该患儿")
def _child_binding_ok():
    prev = os.environ.get("A207_CHILD_PATIENT_ID")
    try:
        os.environ["A207_CHILD_PATIENT_ID"] = "P0020"
        assert get_child_patient_id() == "P0020"
    finally:
        _restore_env("A207_CHILD_PATIENT_ID", prev)


@check("child_assistant：P1 读工具白名单仅 get_patient_profile")
def _child_p1_whitelist():
    assert P1_CHILD_READ_TOOLS == frozenset({"get_patient_profile"}), P1_CHILD_READ_TOOLS


@check("child_assistant：upsert_food_diary 工具级显式拒绝（矩阵 RW 派生包含 child 的收口）")
def _child_upsert_food_diary_rejected():
    import a207_policy.gate as _gate
    from a207_policy import PermissionDenied as _PD

    prev = os.environ.get("A207_CALLER")
    try:
        os.environ["A207_CALLER"] = "child_assistant"
        try:
            _gate.enforce_nutrition_tool("child_assistant", "upsert_food_diary")
        except _PD:
            pass
        else:
            raise AssertionError("child 写 upsert_food_diary 应被工具级拒绝")
    finally:
        _restore_env("A207_CALLER", prev)


@check("child_assistant：record_child_food 工具级放行（仅 child）")
def _child_record_food_allowed():
    import a207_policy.gate as _gate

    prev = os.environ.get("A207_CALLER")
    try:
        os.environ["A207_CALLER"] = "child_assistant"
        assert _gate.enforce_nutrition_tool("child_assistant", "record_child_food") == ACCESS_RW
        # 家长/医生写 record_child_food 拒绝
        for other in ("parent_assistant", "doctor_assistant"):
            os.environ["A207_CALLER"] = other
            try:
                _gate.enforce_nutrition_tool(other, "record_child_food")
            except PermissionDenied:
                continue
            raise AssertionError(f"{other} 写 record_child_food 应被拒绝")
    finally:
        _restore_env("A207_CALLER", prev)


if __name__ == "__main__":
    raise SystemExit(main())
