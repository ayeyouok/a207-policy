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
import sys
import tempfile
import traceback
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from a207_policy import (  # noqa: E402
    ACCESS_LIMITED,
    ACCESS_NONE,
    ACCESS_READ,
    ACCESS_RW,
    CALLERS,
    CHILD_FORBIDDEN_MCPS,
    KNOWLEDGE_PROFILE,
    MCP_ALIASES,
    PERMISSION_MATRIX,
    WRITE_TOOL_POLICY,
    CallerUnknown,
    PermissionDenied,
    as_caller,
    check_permission,
    enforce_nutrition_tool,
    enforce_read,
    enforce_write,
    get_caller,
    knowledge_profile,
    normalize_mcp,
    resolve_state_path,
    set_caller,
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str):
    def deco(fn):
        try:
            fn()
            RESULTS.append((name, True, ""))
        except AssertionError as exc:
            RESULTS.append((name, False, str(exc) or "断言失败"))
        except Exception:  # noqa: BLE001
            RESULTS.append((name, False, traceback.format_exc(limit=3)))
        return fn

    return deco


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


# --------------------------------------------------------- 矩阵完整性

@check("矩阵规模：恰好 5 个 MCP（CKDNutri P1–P5）")
def _matrix_size():
    assert len(PERMISSION_MATRIX) == 5, f"矩阵行数={len(PERMISSION_MATRIX)}，期望 5"


@check("矩阵无重复键：5 个预期 MCP 各出现一次且取值符合 intent（锁定无 last-wins 错配）")
def _matrix_no_dup_keys():
    expected = {
        "CKDNutri-clinical-data-mcp": {"doctor_assistant": ACCESS_RW, "parent_assistant": ACCESS_LIMITED, "risk_warning": ACCESS_READ},
        "CKDNutri-nutrition-mcp":     {"doctor_assistant": ACCESS_READ, "parent_assistant": ACCESS_RW, "risk_warning": ACCESS_NONE},
        "CKDNutri-care-mcp":          {"doctor_assistant": ACCESS_RW, "parent_assistant": ACCESS_READ, "risk_warning": ACCESS_RW},
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
        "a207-gamification-mcp": "CKDNutri-nutrition-mcp",
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
    # 网关/中间件不应被别名归一（保持「未登记」）
    for infra in ("a207-router-mcp", "a207-gateway-mcp"):
        assert normalize_mcp(infra) == infra, f"{infra} 不应被别名归一"


@check("_matrix_writers 防归一化：传入废弃旧名也能安全派生（不 KeyError）")
def _matrix_writers_normalize():
    from a207_policy.matrix import _matrix_writers  # noqa: E402
    got = _matrix_writers("a207-followup-mcp")
    assert got == _matrix_writers("CKDNutri-care-mcp"), "旧名 a207-followup-mcp 未正确归一"


@check("GAMIFICATION_MCP 已纠正符号漂移（指向 M11 并入后的 P2）")
def _gamification_symbol():
    from a207_policy.matrix import GAMIFICATION_MCP  # noqa: E402
    assert GAMIFICATION_MCP == "CKDNutri-nutrition-mcp"
    from a207_policy.matrix import GAMIFICATION_ALLOWED  # noqa: E402
    assert GAMIFICATION_ALLOWED == frozenset(), "退役包写白名单必须仍为空（fail-closed）"


# --------------------------------------------------------- OD-011：写白名单唯一事实源

@check("NUTRITION_ASSESSMENT_WRITE_ALLOWED 必须从矩阵派生（OD-011，禁手写更宽集合）")
def _nutrition_write_derived():
    from a207_policy.matrix import (  # noqa: E402
        _matrix_writers,
        NUTRITION_ASSESSMENT_WRITE_ALLOWED,
    )
    assert NUTRITION_ASSESSMENT_WRITE_ALLOWED == _matrix_writers("CKDNutri-nutrition-mcp"), \
        "写白名单未与矩阵保持一致"
    assert NUTRITION_ASSESSMENT_WRITE_ALLOWED == frozenset({"parent_assistant"}), \
        f"营养写白名单={NUTRITION_ASSESSMENT_WRITE_ALLOWED}，期望 {{parent_assistant}}"


@check("LIS/FOLLOWUP/NOTIFY 写白名单同样由矩阵派生（OD-011 一致）")
def _other_writes_derived():
    from a207_policy.matrix import (  # noqa: E402
        FOLLOWUP_WRITE_ALLOWED,
        LIS_WRITE_ALLOWED,
        NOTIFY_WRITE_ROLES,
        _matrix_writers,
    )
    assert LIS_WRITE_ALLOWED == _matrix_writers("CKDNutri-clinical-data-mcp")
    assert FOLLOWUP_WRITE_ALLOWED == _matrix_writers("CKDNutri-care-mcp")
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


@check("矩阵↔写策略一致：非豁免写工具，allowed 角色在矩阵必须 R/W")
def _matrix_write_consistent():
    from a207_policy.gate import _MATRIX_EXEMPT_WRITE_TOOLS  # noqa: E402
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


@check("enforce_write：医生 push_to_emr 放行（content×doctor=RW 回查通过）")
def _doctor_push_emr():
    with as_caller("doctor_assistant"):
        enforce_write("CKDNutri-content-mcp", tool="push_to_emr")


@check("enforce_write：家长 push_to_emr 拒绝（不在 allowed 且矩阵非 R/W）")
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
    assert enforce_nutrition_tool("parent_assistant", "upsert_food_diary") == ACCESS_LIMITED


@check("enforce_nutrition_tool：医生写 upsert_food_diary 拒绝（仅家长）")
def _doctor_no_upsert_diary():
    try:
        enforce_nutrition_tool("doctor_assistant", "upsert_food_diary")
    except PermissionDenied:
        return
    raise AssertionError("医生竟能写饮食日记")


@check("enforce_nutrition_tool：家长读 get_food_diary_summary 放行")
def _parent_read_diary_summary():
    assert enforce_nutrition_tool("parent_assistant", "get_food_diary_summary") == ACCESS_LIMITED


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


def main() -> int:
    for name, ok, msg in RESULTS:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            print("       " + msg.strip().replace("\n", "\n       "))
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    print(f"\n合计 {len(RESULTS)} 项：通过 {passed}，失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
