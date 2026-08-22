# -*- coding: utf-8 -*-
"""2026-08-22 策略核心文件审查回归：Claim 2/3/6/7/8 修复验证。

对照用户给定设计边界（demo_parent_assistant 对 P0007/P0010/P0020 免令牌；
child_assistant 仅 get_patient_profile 受限视图 + get_food_diary_summary +
record_child_food 写；record_child_food 仅 child 可写）逐条核实后落地修复，本测试
锁定修复不回归：

- Claim 2（state.py 路径 off-by-one）：A207_DATA_DIR 指向 src 下合法工程子目录不再
  被误判为"安装目录"拒绝；指向包目录或其子目录仍 fail-closed 拒绝。
- Claim 3（matrix.py HIS_LIMITED 遗漏 child）：child_assistant 已并入 HIS_LIMITED/HIS_READ，
  与矩阵 P1×child=ACCESS_LIMITED 一致。
- Claim 6（caller.py get_child_patient_id 无校验）：补 ^P[0-9]{4,}$ 契约校验（复用
  gate.validate_patient_id），畸形/缺失 fail-closed 拒绝。
- Claim 7（exceptions.py 非原子重定向）：全局包部分初始化（缺任一类）时整体不替换，
  不得留下"3 全局 + 1 本地"分裂态。
- Claim 8（errors.py 重复注释 + RuntimeError 脱敏）：重复注释已删；RuntimeError→
  INTERNAL_ERROR(脱敏) 的**有意设计**保持不变（守卫 content 数据加载 fail-closed）。

零 pytest 依赖（直接运行模式，CI 逐文件 python 跑，与 test_bug69.py 同约定）。
"""
import importlib
import logging
import os
import shutil
import sys
import types
from pathlib import Path

os.environ.setdefault("A207_ENV", "test")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from a207_policy import matrix as _matrix  # noqa: E402
from a207_policy import state as _state  # noqa: E402


def test_claim2_state_path_off_by_one():
    """Claim 2：resolve_state_path 仅拦截"包目录及其子目录"，放行 src 下合法工程子目录。

    旧实现 `_installed = __file__.parent.parent`（= src），`_installed in parents` 会把
    `src/data` 等合法工程子目录误判为安装目录拒绝。修复后 `_installed = __file__.parent`
    （= a207_policy 包目录），用 is_relative_to 精确拦截包及其子目录。

    注意：比对基准是 state.py 的**真实** `__file__` 位置，故直接以真实包目录/真实 src
    下子目录为判定对象（不再用临时假树模拟，否则与真实 __file__ 永远不相等）。
    """
    import tempfile
    _pkg = Path(_state.__file__).resolve().parent   # 真实 a207_policy 包目录
    _src = _pkg.parent                              # 真实 src（包目录父级）
    _ext = Path(tempfile.mkdtemp(prefix="a207_state_ext_"))
    try:
        # 1. 完全外部路径：放行
        p = _state.resolve_state_path("s.json", base=str(_ext))
        assert p.name == "s.json", f"外部路径应放行: {p}"
        # 2. 包目录本身：拒绝（写进包=污染）
        try:
            _state.resolve_state_path("s.json", base=str(_pkg))
            raise AssertionError("包目录本身应被拒绝")
        except ValueError:
            pass
        # 3. 包子目录：拒绝
        try:
            _state.resolve_state_path("s.json", base=str(_pkg / "sub"))
            raise AssertionError("包子目录应被拒绝")
        except ValueError:
            pass
        # 4. off-by-one 回归：真实 src 下但**非包目录**的合法工程子目录 → 必须放行
        #    （旧 bug：_installed=src 会误拒；修复后 _installed=a207_policy 放行）
        _eng = _src / "eng_data_claim2_test"
        try:
            p2 = _state.resolve_state_path("s.json", base=str(_eng))
            assert p2.parent == _eng, f"src 下合法工程子目录应放行，实际 {p2}"
        finally:
            shutil.rmtree(_eng, ignore_errors=True)
    finally:
        shutil.rmtree(_ext, ignore_errors=True)
    print("  [ok] Claim2 状态目录仅拦截包目录及其子目录，放行 src 下合法工程子目录")


def test_claim3_his_limited_includes_child():
    """Claim 3：HIS_LIMITED 已并入 child_assistant，与矩阵 P1×child=ACCESS_LIMITED 对齐。"""
    assert "child_assistant" in _matrix.HIS_LIMITED, \
        f"HIS_LIMITED 应含 child_assistant，实际 {sorted(_matrix.HIS_LIMITED)}"
    assert "child_assistant" in _matrix.HIS_READ, \
        f"HIS_READ 应含 child_assistant，实际 {sorted(_matrix.HIS_READ)}"
    # 矩阵侧 child 在 P1 确为 ACCESS_LIMITED（派生一致性的根因）
    assert _matrix.PERMISSION_MATRIX["CKDNutri-clinical-data-mcp"]["child_assistant"] \
        == _matrix.ACCESS_LIMITED
    print("  [ok] Claim3 HIS_LIMITED/HIS_READ 含 child_assistant（与矩阵 child=RL 一致）")


def test_claim6_child_patient_id_validation():
    """Claim 6：get_child_patient_id 校验患儿编号契约（^P[0-9]{4,}$）。"""
    from a207_policy.caller import get_child_patient_id  # noqa: E402
    import a207_policy.exceptions as _exc  # noqa: E402

    _env = "A207_CHILD_PATIENT_ID"
    _saved = os.environ.get(_env)
    try:
        # 合法
        os.environ[_env] = "P0020"
        assert get_child_patient_id() == "P0020", "合法患儿编号应放行"
        # 畸形：非 P 前缀
        os.environ[_env] = "garbage"
        try:
            get_child_patient_id()
            raise AssertionError("畸形 child patient_id 应被拒绝")
        except ValueError:
            pass
        # 畸形：P + 位数不足（需 4+ 位数字）
        os.environ[_env] = "P1"
        try:
            get_child_patient_id()
            raise AssertionError("P1 应被拒绝（需 ^P[0-9]{4,}）")
        except ValueError:
            pass
        # 缺失
        os.environ.pop(_env, None)
        try:
            get_child_patient_id()
            raise AssertionError("缺失 child patient_id 应 fail-closed 拒绝")
        except _exc.CallerUnknown:
            pass
    finally:
        if _saved is not None:
            os.environ[_env] = _saved
        else:
            os.environ.pop(_env, None)
    print("  [ok] Claim6 get_child_patient_id 校验患儿编号契约（合法放行/畸形/缺失拒绝）")


def test_claim7_exceptions_redirect_atomic_partial_global():
    """Claim 7：exceptions.py 全局包重定向必须原子——全局包缺任一类时整体不替换，
    不得留下"3 全局 + 1 本地"分裂态（前 3 类已是全局类、ConflictError 仍是本地类 →
    except 按类身份匹配漏抓）。

    做法：把 exceptions.py 作为**全新副本模块**加载，期间把 sys.modules["a207_policy"]
    指向一个"部分初始化"的 fake（有 CallerError/CallerUnknown/PermissionDenied 但缺
    ConflictError）。副本顶层 `import a207_policy` 拿到 fake → 触发重定向分支。
    """
    import importlib.util

    exceptions = importlib.import_module("a207_policy.exceptions")
    _src_file = exceptions.__file__

    class _FCallerError(Exception):
        pass

    class _FCallerUnknown(_FCallerError):
        pass

    class _FPermissionDenied(_FCallerError):
        pass

    _fake_global = types.ModuleType("a207_policy")
    _fake_global.__path__ = []
    _fake_global.CallerError = _FCallerError
    _fake_global.CallerUnknown = _FCallerUnknown
    _fake_global.PermissionDenied = _FPermissionDenied
    # 故意不设置 ConflictError（模拟旧版/部分初始化全局包）

    _saved = sys.modules.get("a207_policy")
    _copy_name = "a207_policy_exceptions_claim7_test"
    sys.modules["a207_policy"] = _fake_global
    try:
        _spec = importlib.util.spec_from_file_location(_copy_name, _src_file)
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules[_copy_name] = _mod
        _spec.loader.exec_module(_mod)
        # 关键不变量：重定向要么全做、要么全不做 → 4 个类的 __module__ 必须一致
        # （修复后：全不做 → 全部来自本副本模块；旧 bug：前 3 来自测试 fake、ConflictError
        # 来自本副本模块 → 不一致）。
        assert _mod.CallerError.__module__ == _mod.ConflictError.__module__, (
            "全局包部分初始化时重定向非原子：类来源分裂 "
            f"({_mod.CallerError.__module__} vs {_mod.ConflictError.__module__})")
        # 且不应有任何类被替换成 fake（fake 类 __module__ 是本测试模块）
        for _name in ("CallerError", "CallerUnknown", "PermissionDenied", "ConflictError"):
            _cls = getattr(_mod, _name)
            assert _cls.__module__ == _mod.__name__, (
                f"{_name} 被错误替换为 fake 全局类（来自 {_cls.__module__}）")
    finally:
        if _saved is not None:
            sys.modules["a207_policy"] = _saved
        else:
            sys.modules.pop("a207_policy", None)
        sys.modules.pop(_copy_name, None)
    print("  [ok] Claim7 全局包部分初始化时异常类重定向原子（无分裂态）")


def test_claim8_runtimeerror_masked_preserved():
    """Claim 8 第二部分守护：RuntimeError→INTERNAL_ERROR(脱敏) 属**有意设计**
    （content 数据加载 fail-closed / 策略守卫多抛 RuntimeError），不得误改回泄露。"""
    from a207_policy.errors import translate_error  # noqa: E402

    _log = logging.getLogger("test_claim8")
    # RuntimeError → INTERNAL_ERROR（脱敏，不泄露路径）
    out = translate_error(RuntimeError("boom at /secret/path/x.json"),
                          domain="P5", logger=_log, tool="load_kb")
    assert out["error"] == "INTERNAL_ERROR", f"RuntimeError 应归 INTERNAL_ERROR，实际 {out}"
    assert "/secret/path" not in out["detail"], "RuntimeError 必须脱敏（不得泄露路径）"
    # ValueError → INVALID_INPUT（保留，对调用方有明确语义）
    out2 = translate_error(ValueError("bad arg"), domain="P5", logger=_log, tool="x",
                           value_error_to_invalid=True)
    assert out2["error"] == "INVALID_INPUT", f"ValueError 应归 INVALID_INPUT，实际 {out2}"
    print("  [ok] Claim8 RuntimeError→INTERNAL_ERROR(脱敏) / ValueError→INVALID_INPUT 行为保持")


def main():
    print("2026-08-22 策略核心文件审查回归（Claim 2/3/6/7/8）")
    test_claim2_state_path_off_by_one()
    test_claim3_his_limited_includes_child()
    test_claim6_child_patient_id_validation()
    test_claim7_exceptions_redirect_atomic_partial_global()
    test_claim8_runtimeerror_masked_preserved()
    print("策略核心审查回归 OK（5 用例）")


if __name__ == "__main__":
    main()
