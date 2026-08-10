"""a207-policy 自测：纯 python 可跑，不依赖 pytest / fastmcp。

运行：python tests/test_policy.py

为什么这个包必须单独有测试
--------------------------
a207-policy 是 13 个 MCP 包共同的信任根——身份从这里取，权限从这里判，
状态路径从这里算。它错一行，13 个包一起错，而且**各包自己的测试照样全绿**
（因为它们都信任 policy 的返回值）。所以这里专打那些"下游测不出来"的盲区：
矩阵有没有空洞、fail-closed 是否真的关死、写权是否唯一、患儿是否真隔离、
未知角色会不会悄悄回退到全量专业语料。
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


@check("get_caller：空串/纯空格视同缺失（不许当成合法身份）")
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


@check("as_caller：块内抛异常也必须还原（否则污染后续用例）")
def _as_caller_exception():
    _reset()
    try:
        with as_caller("child_companion"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert get_caller() == "doctor_assistant", "异常路径未还原身份"


@check("as_caller(None)：必须同时清 env，否则 fail-closed 测了个寂寞")
def _as_caller_none_clears_env():
    _reset()
    with as_caller(None):
        try:
            get_caller()
        except CallerUnknown:
            pass
        else:
            raise AssertionError("as_caller(None) 内仍取到身份，env 未清")
    assert get_caller() == "doctor_assistant", "退出后 env 未还原"


@check("as_caller：嵌套逐层还原")
def _as_caller_nested():
    _reset()
    with as_caller("nutritionist"):
        with as_caller("risk_warning"):
            assert get_caller() == "risk_warning"
        assert get_caller() == "nutritionist"
    assert get_caller() == "doctor_assistant"


# --------------------------------------------------------- 矩阵完整性（下游测不出）

@check("矩阵无空洞：每个已登记 MCP x 每个角色都有明确取值")
def _matrix_complete():
    holes = []
    for mcp_name, row in PERMISSION_MATRIX.items():
        for caller in CALLERS:
            if caller not in row:
                holes.append(f"{mcp_name} x {caller}")
    assert not holes, f"矩阵有空洞（缺失即行为未定义）：{holes}"


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


@check("矩阵覆盖 13 个 MCP（不多不少）")
def _matrix_size():
    assert len(PERMISSION_MATRIX) == 13, f"矩阵行数={len(PERMISSION_MATRIX)}，期望 13"


@check("别名归一：旧包名解析到真实包名且落在矩阵内（OD-002 回归）")
def _alias_normalize():
    for alias, canonical in MCP_ALIASES.items():
        got = normalize_mcp(alias)
        assert got == canonical, f"{alias} 归一到 {got}，期望 {canonical}"
        assert canonical in PERMISSION_MATRIX, f"{canonical} 不在矩阵中"


# --------------------------------------------------------- MX 硬规则

@check("MX-2 患儿隔离：CHILD_FORBIDDEN_MCPS 在矩阵里确实为 none")
def _child_isolation():
    leaks = [
        m for m in CHILD_FORBIDDEN_MCPS
        if PERMISSION_MATRIX.get(normalize_mcp(m), {}).get("child_companion") != ACCESS_NONE
    ]
    assert not leaks, f"患儿禁入清单与矩阵不一致，存在泄漏：{leaks}"


@check("患儿绝不可触及检验原始数据（M2 LIS 必须为 none）")
def _child_no_lis():
    assert PERMISSION_MATRIX["a207-lis-mcp"]["child_companion"] == ACCESS_NONE


@check("检验写权唯一：M2 LIS 只有 doctor_assistant 是 R/W")
def _lis_write_unique():
    row = PERMISSION_MATRIX["a207-lis-mcp"]
    writers = sorted(c for c, a in row.items() if a == ACCESS_RW)
    assert writers == ["doctor_assistant"], f"检验写权不唯一：{writers}"


@check("家长看检验只能是受限视图 RL（不得是全量 R）")
def _parent_lis_limited():
    assert PERMISSION_MATRIX["a207-lis-mcp"]["parent_assistant"] == ACCESS_LIMITED


# --------------------------------------------------------- 闸门确定性执行

@check("enforce_read：无权角色被拒（PermissionDenied）")
def _enforce_read_denied():
    with as_caller("child_companion"):
        try:
            enforce_read("a207-lis-mcp")
        except PermissionDenied:
            return
        raise AssertionError("child_companion 读 LIS 未被拒")


@check("enforce_read：有权角色放行")
def _enforce_read_allowed():
    with as_caller("doctor_assistant"):
        enforce_read("a207-lis-mcp")


@check("enforce_read：身份缺失时闸门也必须拒（fail-closed 贯穿）")
def _enforce_no_caller():
    with as_caller(None):
        try:
            enforce_read("a207-his-mcp")
        except (CallerUnknown, PermissionDenied):
            return
        raise AssertionError("身份缺失时闸门放行了")


@check("enforce_write：只读角色不得写")
def _enforce_write_denied():
    with as_caller("nutritionist"):
        try:
            enforce_write("a207-lis-mcp", tool="upsert_lab_result")
        except PermissionDenied:
            return
        raise AssertionError("nutritionist 竟能写 LIS")


@check("enforce_write：RW 角色放行")
def _enforce_write_allowed():
    with as_caller("doctor_assistant"):
        enforce_write("a207-lis-mcp", tool="upsert_lab_result")


@check("enforce：未登记的 MCP 一律拒绝（不许悄悄放行未知服务）")
def _enforce_unknown_mcp():
    with as_caller("doctor_assistant"):
        try:
            enforce_read("a207-totally-unknown-mcp")
        except (PermissionDenied, KeyError, ValueError):
            return
        raise AssertionError("未知 MCP 被放行")


@check("check_permission：未登记 caller 一律拒绝")
def _check_unknown_caller():
    res = check_permission("hacker_agent", "a207-lis-mcp", "read")
    assert res["allow"] is False, f"未登记 caller 被放行：{res}"


@check("MX-3 写权收口：每条写工具策略字段完整（mcp/allowed/note）")
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


# --------------------------------------------------------- M12 语料分级

@check("语料分级与矩阵一致：能读 M12 的角色必有 profile，够不着的回退 none")
def _knowledge_matches_matrix():
    # 不是"6 个角色都要有 profile"——orchestrator 在矩阵里对 M12 是 none（够不着），
    # 不给它 profile 才是对的。真正的不变量是这两条：
    row = PERMISSION_MATRIX["a207-knowledge-mcp"]
    missing, leaked = [], []
    for caller, access in row.items():
        readable = access != ACCESS_NONE
        if readable and caller not in KNOWLEDGE_PROFILE:
            missing.append(caller)          # 能读却没分级 → 语料给错风险
        if not readable and knowledge_profile(caller) != "none":
            leaked.append(caller)           # 够不着却拿到 profile → 越权风险
    assert not missing, f"能读 M12 却缺语料分级：{missing}"
    assert not leaked, f"矩阵禁止访问 M12 却拿到语料 profile：{leaked}"


@check("患儿必须 child_safe，家长必须 plain_language")
def _knowledge_safety():
    assert KNOWLEDGE_PROFILE["child_companion"] == "child_safe"
    assert KNOWLEDGE_PROFILE["parent_assistant"] == "plain_language"
    assert knowledge_profile("child_companion") == "child_safe"


@check("knowledge_profile：未知角色不得回退到 full（默认必须最保守）")
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


@check("resolve_state_path：读 A207_DATA_DIR 环境变量")
def _state_env():
    saved = os.environ.get("A207_DATA_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["A207_DATA_DIR"] = tmp
        try:
            p = resolve_state_path("y.json")
            assert Path(tmp) == p.parent or Path(tmp) in p.parents, p
        finally:
            if saved is None:
                os.environ.pop("A207_DATA_DIR", None)
            else:
                os.environ["A207_DATA_DIR"] = saved


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
