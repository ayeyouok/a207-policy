"""策略核心第三轮审查回归测试（2026-08-22）。

覆盖本轮裁定为「属实·已修」的 4 项 claim（零 pytest 依赖，直接 `python tests/...py`）：

- Claim 1（gate.verify_guardian_token）：标准 UTC ISO 的末尾 'Z' 时间戳在
  Python 3.10 及更早的 datetime.fromisoformat 下会抛 ValueError，被 except 误判为
  已过期 → 合法令牌全量失效。修复：解析前 .replace("Z","+00:00")（与 repository.py 同款）。
- Claim 2（state.atomic_write_json）：mkstemp 的 0600 权限 + chmod 携带文件类型位 /
  硬编码 0644 击穿 umask。修复：继承用 stat.S_IMODE 过滤类型位；新建尊重 umask。
- Claim 3（gate.is_write_action）：子串包含判定把中名嵌 '_schedule_' 的只读工具
  （get_schedule_followup）误判为写。修复：严格 startswith 前缀匹配。
- Claim 4（gate.verify_guardian_token）：patient_id 未规范化，带空白字符（"P0007 "）
  静默 False 负。修复：入参走 validate_patient_id 规范化（与 issue 端 storage key 同款）。
- Claim 6（errors.translate_error）：extra_data_types / extra_invalid_types 传单个异常类
  （非元组）时 `_DATA_ERROR_TYPES + 单类` 抛 TypeError，使异常中间件自身 500。
  修复：_as_tuple 规整后再做元组加法。

Claim 5（caller.get_child_patient_id 惰性导入）裁定为「非缺陷·驳回」，无需新增测试。
"""

from __future__ import annotations

import os

os.environ.setdefault("A207_ENV", "test")            # N-SEC-1：测试进程显式声明测试环境
os.environ.setdefault("A207_ACCEPT_DEV_STORAGE", "1")  # 生产护栏：确认 json 后端为开发模式
import json
import logging
import shutil
import stat
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
# CI（publish.yml 仅 `python tests/test_*.py`、不安装包本身）必须显式把仓库 src 加入
# sys.path，否则 `from a207_policy import ...` 抛 ModuleNotFoundError
# （2026-08-22 CI 踩坑；对齐 test_policy.py / test_policy_review2 的引导模式）。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from a207_policy import (
    verify_guardian_token,
    is_write_action,
    translate_error,
)
from a207_policy.state import atomic_write_json


def test_claim1_z_timestamp_and_claim4_normalization():
    """Claim 1：'Z' 后缀 ISO 时间戳可解析（低版本 Python 不再误判过期）；
    Claim 4：带空白 patient_id 规范化后命中 storage key。"""
    key = "A207_GUARDIAN_TOKEN_DIR"
    prev = os.environ.get(key)
    d = tempfile.mkdtemp(prefix="a207_claim1_")
    try:
        os.environ[key] = d
        store = os.path.join(d, "guardian_tokens.json")

        # Z 后缀（标准 UTC ISO，Python 3.10 才原生支持，此前低版本 ValueError 误判过期）
        with open(store, "w", encoding="utf-8") as f:
            json.dump({"P0007": {"token": "SEC123",
                                 "expires_at": "2099-01-01T00:00:00Z"}}, f)
        assert verify_guardian_token("P0007", "SEC123") is True, \
            "Z 时间戳应解析为未过期"
        # Claim 4：带空白 patient_id 应规范化命中
        assert verify_guardian_token("P0007 ", "SEC123") is True, \
            "尾部空白 id 应规范化命中"
        assert verify_guardian_token(" P0007", "SEC123") is True, \
            "头部空白 id 应规范化命中"
        assert verify_guardian_token("P0007", "WRONG") is False, \
            "错误令牌应 False"

        # 过期（Z）→ False
        with open(store, "w", encoding="utf-8") as f:
            json.dump({"P0007": {"token": "SEC123",
                                 "expires_at": "2000-01-01T00:00:00Z"}}, f)
        assert verify_guardian_token("P0007", "SEC123") is False, \
            "过期令牌应 False"

        # +00:00 形态仍兼容
        with open(store, "w", encoding="utf-8") as f:
            json.dump({"P0007": {"token": "SEC123",
                                 "expires_at": "2099-01-01T00:00:00+00:00"}}, f)
        assert verify_guardian_token("P0007", "SEC123") is True, \
            "+00:00 形态应兼容"
    finally:
        if prev is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prev
        shutil.rmtree(d, ignore_errors=True)
    print("  [ok] Claim1 Z-ISO 时间戳解析 + Claim4 patient_id 规范化")


def test_claim2_atomic_write_json_mode():
    """Claim 2：继承用 stat.S_IMODE 过滤类型位；新建尊重 umask 安全基线。"""
    d = tempfile.mkdtemp(prefix="a207_claim2_")
    try:
        p = os.path.join(d, "state.json")
        atomic_write_json(p, {"a": 1})
        assert os.path.exists(p), "原子写应生成文件"
        if os.name == "posix":
            # 继承：先把目标设为 0o640，再次写入应继承该权限位
            os.chmod(p, 0o640)
            atomic_write_json(p, {"a": 2})
            m = stat.S_IMODE(os.stat(p).st_mode)
            assert m == 0o640, f"继承模式应为 0o640，实际 {oct(m)}"
            # 新建文件尊重 umask（标准 umask 022 → 0o644）
            p2 = os.path.join(d, "state2.json")
            um = os.umask(0)
            os.umask(um)
            expected = 0o666 & ~um
            atomic_write_json(p2, {"b": 1})
            m2 = stat.S_IMODE(os.stat(p2).st_mode)
            assert m2 == expected, \
                f"新建模式应=0o666&~umask={oct(expected)}，实际 {oct(m2)}"
        else:
            # Windows 无 umask 语义，仅验证写入成功且内容正确
            with open(p, encoding="utf-8") as f:
                assert json.load(f)["a"] == 1
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("  [ok] Claim2 atomic_write_json 继承权限位(S_IMODE) + 新建尊重 umask")


def test_claim3_is_write_action_strict_prefix():
    """Claim 3：is_write_action 严格前缀匹配，消除子串假阳性。"""
    # 此前子串判定会把中名嵌 '_schedule_' 的只读工具误判为写
    assert is_write_action("get_schedule_followup") is False, \
        "只读工具(get_schedule_followup)不应是写"
    assert is_write_action("get_followup_schedule") is False
    assert is_write_action("dialog_analysis") is False, \
        "含 log_ 子串不应误判为写"
    assert is_write_action("get_food_diary_summary") is False
    assert is_write_action("get_patient_profile") is False
    # 真实写工具应是写
    assert is_write_action("schedule_followup") is True
    assert is_write_action("upsert_food_diary") is True
    assert is_write_action("notify_physician") is True
    assert is_write_action("create_lab_result") is True
    print("  [ok] Claim3 is_write_action 严格前缀匹配，消除子串假阳性")


def test_claim6_translate_error_nontuple_types():
    """Claim 6：extra_data_types / extra_invalid_types 传单个异常类（非元组）不崩。"""
    class MyDataError(Exception):
        pass

    class MyInvalid(Exception):
        pass

    logger = logging.getLogger("a207_claim6")
    # 单个异常类（非元组）传 extra_data_types → 不应抛 TypeError，归 INTERNAL_ERROR
    env = translate_error(MyDataError("boom"), domain="P2", logger=logger,
                          extra_data_types=MyDataError)
    assert env["error"] == "INTERNAL_ERROR", \
        f"单类 extra_data_types 应归 INTERNAL_ERROR，实际 {env}"
    # 单个异常类传 extra_invalid_types → 应归 INVALID_INPUT
    env2 = translate_error(MyInvalid("bad"), domain="P5", logger=logger,
                           extra_invalid_types=MyInvalid)
    assert env2["error"] == "INVALID_INPUT", \
        f"单类 extra_invalid_types 应归 INVALID_INPUT，实际 {env2}"
    # 元组形式仍正常
    env3 = translate_error(MyDataError("x"), domain="P1", logger=logger,
                           extra_data_types=(MyDataError,))
    assert env3["error"] == "INTERNAL_ERROR", \
        f"元组形式 extra_data_types 应归 INTERNAL_ERROR，实际 {env3}"
    print("  [ok] Claim6 translate_error 单类 extra_*_types 防 TypeError 中间件崩溃")


def main():
    test_claim1_z_timestamp_and_claim4_normalization()
    test_claim2_atomic_write_json_mode()
    test_claim3_is_write_action_strict_prefix()
    test_claim6_translate_error_nontuple_types()
    print("\nALL test_policy_review3_20260822 PASSED")


if __name__ == "__main__":
    main()
