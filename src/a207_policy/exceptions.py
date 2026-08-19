"""权限/身份相关异常类（集中定义，单一事实源）。

为什么集中 + 回退复用全局类
---------------------------
a207-policy 会以「全局包」和「随包内置的 _policy 子模块」两种形态并存
（见 deploy/sync_policy.py）。异常类一旦被复制成两份，就是两个不同的类对象，
测试里 `from a207_policy import CallerUnknown` 抓住的是全局那份，而内置 _policy
抛的是另一份 → `except` 按类身份匹配会漏抓。

解法：本模块先定义本地类，再尝试把自身类名「重定向」到全局 a207_policy 的同名类
（取不到再保留本地）。这样只要全局 a207_policy 可导入，两种形态抛出的异常就是
同一个类对象，测试捕获与部署运行都一致；若全局包完全不存在（隔离部署），则退化为
本地自洽副本。
"""

from __future__ import annotations


class CallerError(Exception):
    """权限/身份相关错误的基类。"""

    # 2026-08-15（DRY 收敛）：FORBIDDEN 信封在 policy 内生成——此前 5 个 server 的
    # _invalid 各自 getattr(exc, "caller"/"action"/"reason") 拼文案，server 层"看见"
    # 了 caller（轻微打破"server 不感知身份"）。现由异常类自身产出信封，server 纯透传。
    # caller/action/reason 三重 or 保底：getattr 默认值在属性显式置 None/空串时不生效
    # （六审/七审踩过的坑：曾输出 "caller=None 无权 None" 与 "（）"）。
    def envelope(self) -> dict[str, str]:
        caller = getattr(self, "caller", None) or "?"
        action = getattr(self, "action", None) or "access"
        reason = getattr(self, "reason", None) or str(self) or "无明确原因"
        return {"ok": False, "error": "FORBIDDEN",
                "detail": f"caller={caller} 无权 {action}（{reason}）"}


class CallerUnknown(CallerError):
    """caller 未注入（环境变量缺失或为空）→ fail-closed 拒绝。"""


class PermissionDenied(CallerError):
    """权限不足，确定性拒绝（模型无法绕过）。携带 caller/mcp/action/reason 供工具层转成既有 forbidden 形态。"""

    def __init__(self, caller: str, mcp: str, action: str, reason: str):
        self.caller = caller
        self.mcp = mcp
        self.action = action
        self.reason = reason
        super().__init__(f"DENIED {caller} -> {mcp}:{action} : {reason}")


class ConflictError(Exception):
    """业务写冲突（九审，2026-08-16）：跨包错误码统一——CONFLICT 此前仅
    clinical-data 以信封形式定义；care/nutrition 同类写冲突（sample_id 撞键、
    幂等重复、并发行冲突）抛 RuntimeError 冒泡到 server 被 translate_error 归
    INTERNAL_ERROR，编排层无法区分"服务端坏了" vs "业务冲突（可换 id/重试）"。

    各 repository/core 在确定性冲突处抛本异常（或捕获底层冲突转本异常），
    translate_error 显式映射 {ok, error: "CONFLICT", detail}——三包统一，
    编排层可据此给出"业务冲突"而非"内部错误"提示。
    """


try:
    import a207_policy as _global_policy  # 全局 a207_policy（若存在）

    CallerError = _global_policy.CallerError
    CallerUnknown = _global_policy.CallerUnknown
    PermissionDenied = _global_policy.PermissionDenied
    ConflictError = _global_policy.ConflictError
except (ImportError, AttributeError) as _exc:
    # L2（2026-08-16，第七轮审查）：全局包不可用 → 保留本地副本（隔离部署自洽）；
    # 仅捕获 ImportError/AttributeError（此前裸 except Exception 会把 ImportError
    # 之外的任意异常静默吞掉，重定向脆弱——如 _global_policy 部分符号缺失时静默
    # 用不完整副本）。异常类型显式 + warning 可观测。
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "a207_policy 全局包不可用（%s: %s），异常类回退本地副本", type(_exc).__name__, _exc)
