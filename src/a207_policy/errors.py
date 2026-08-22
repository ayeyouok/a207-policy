"""横切异常翻译（2026-08-15）：5 个 server 的 _invalid 复制收敛为单实现。

背景（DRY 违反）：care/assessment/nutrition/clinical-data/content 五个 server.py
各有几乎一样的 _invalid()（约 30 行），只在 error_code=XXX_UNKNOWN / 服务名文案上
不同。改一次脱敏策略要改 5 处，必漏。SOP 的"B2 中心化异常"在此补齐。

行为语义（与原 5 份 _invalid 逐项对齐）：
- CallerError → FORBIDDEN 信封（envelope 由异常类自身在 policy 内生成，
  server 纯透传，不再 getattr 拼文案）；
- 数据/环境错误（FileNotFoundError/OSError/JSONDecodeError/RuntimeError +
  extra_data_types）→ INTERNAL_ERROR，detail **脱敏**（不泄露服务端绝对路径）；
- ValueError → INVALID_INPUT（detail 保留，对调用方有明确语义）——content 例外：
  value_error_to_invalid=False 时 ValueError 归 INTERNAL_ERROR（其 ValueError 全部
  来自数据文件加载期 fail-closed，是服务端数据问题而非客户端入参）；
- extra_invalid_types（如 content 自定义的 InvalidArgumentError）→ INVALID_INPUT；
- 其余未知系统异常（TypeError/KeyError 等内部 Code Bug）→ INTERNAL_ERROR，
  detail 脱敏，完整堆栈仅服务端日志。

每个 server 保留一行薄包装 `_invalid = lambda exc: translate_error(exc, domain=...)`，
调用点零改动。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .exceptions import CallerError, ConflictError

# domain 主键（P1-P5 对齐权限矩阵）：服务名/脱敏 error_code 文案统一在此
DOMAIN_CONFIG: dict[str, dict[str, str]] = {
    "P1": {"service": "临床数据服务", "data_code": "P1_DATA", "unknown_code": "P1_UNKNOWN"},
    "P2": {"service": "营养服务", "data_code": "NUTR_DATA", "unknown_code": "NUTR_UNKNOWN"},
    "P3": {"service": "随访服务", "data_code": "CARE_DATA", "unknown_code": "CARE_UNKNOWN"},
    "P4": {"service": "评估服务", "data_code": "ASSESS_DATA", "unknown_code": "ASSESS_UNKNOWN"},
    "P5": {"service": "内容服务", "data_code": "CONTENT_DATA", "unknown_code": "CONTENT_UNKNOWN"},
}

# 数据/环境错误统一归类（内容/存储损坏 = 服务端内部问题，detail 脱敏）
_DATA_ERROR_TYPES = (FileNotFoundError, OSError, json.JSONDecodeError, RuntimeError)


def translate_error(
    exc: Exception,
    *,
    domain: str,
    logger: logging.Logger,
    tool: str | None = None,
    safe_args: dict[str, Any] | None = None,
    extra_invalid_types: tuple[type, ...] = (),
    extra_data_types: tuple[type, ...] = (),
    value_error_to_invalid: bool = True,
) -> dict[str, Any]:
    """把任意异常翻译成统一信封 {ok, error, detail}（原各包 _invalid 的收敛实现）。

    :param domain: "P1".."P5"（对齐权限矩阵，决定服务名与 error_code 文案）
    :param logger: 调用方（server）logger，日志归属各服务名
    :param tool: 工具名（仅用于服务端日志上下文）
    :param safe_args: 已脱敏的调用参数（care 特有：guardian_token 等敏感值不进日志）
    :param extra_invalid_types: 归 INVALID_INPUT 的额外异常类型（如 content.InvalidArgumentError）
    :param extra_data_types: 归 INTERNAL_ERROR(数据) 的额外异常类型（如 content.KeyError）
    :param value_error_to_invalid: ValueError 是否归 INVALID_INPUT（content=False）
    """
    # L2（2026-08-16，第七轮审查）：domain 非法 → 此前 KeyError 冒泡（服务端 500
    # 且日志无上下文）；fallback 到首个域配置（同 data_code 语义），不崩。
    cfg = DOMAIN_CONFIG.get(domain) or next(iter(DOMAIN_CONFIG.values()))
    ctx = "" if tool is None else f" tool={tool}"
    args_ctx = "" if not safe_args else f" args={safe_args}"
    if isinstance(exc, CallerError):
        # FORBIDDEN 信封由异常类生成（policy 内），server 不感知 caller
        logger.warning("[%s]鉴权拒绝:%s%s exc=%s", domain, ctx, args_ctx, exc)
        return exc.envelope()
    if isinstance(exc, ConflictError):
        # 九审（2026-08-16）：跨包错误码统一——CONFLICT 此前仅 clinical-data 以信封
        # 定义；care/nutrition 同类写冲突（sample_id 撞键/幂等重复）抛 RuntimeError
        # 被归 INTERNAL_ERROR，编排层无法区分"服务端坏了" vs "业务冲突（可重试）"。
        # 现显式映射：detail 保留（业务冲突信息对调用方有明确语义，非内部路径泄漏）。
        logger.info("[%s]业务写冲突:%s%s exc=%s", domain, ctx, args_ctx, exc)
        return {"ok": False, "error": "CONFLICT", "detail": str(exc)}
    if isinstance(exc, extra_invalid_types):
        logger.info("[%s]入参错误（客户端）:%s%s exc=%s", domain, ctx, args_ctx, exc)
        return {"ok": False, "error": "INVALID_INPUT", "detail": str(exc)}
    if isinstance(exc, _DATA_ERROR_TYPES + extra_data_types):
        logger.warning("[%s]内部数据错误:%s%s exc=%s", domain, ctx, args_ctx, exc)
        return {"ok": False, "error": "INTERNAL_ERROR",
                "detail": f"内部数据错误（error_code={cfg['data_code']}），详情见服务端日志"}
    if isinstance(exc, ValueError):
        if value_error_to_invalid:
            logger.info("[%s]参数校验拦截:%s%s exc=%s", domain, ctx, args_ctx, exc)
            return {"ok": False, "error": "INVALID_INPUT", "detail": str(exc)}
        # content 例外：ValueError 来自数据加载 fail-closed，归服务端数据错误
        logger.warning("[%s]内部数据错误（ValueError）:%s%s exc=%s", domain, ctx, args_ctx, exc)
        return {"ok": False, "error": "INTERNAL_ERROR",
                "detail": f"内部数据错误（error_code={cfg['data_code']}），详情见服务端日志"}
    # 未知系统异常 = 内部 Code Bug——归 INTERNAL_ERROR（编排层不应重试/误判入参），
    # detail 脱敏，完整堆栈仅服务端日志。
    logger.error("[%s]未预期异常（内部 bug，error_code=%s）:%s%s",
                 domain, cfg["unknown_code"], ctx, args_ctx, exc_info=exc)
    return {"ok": False, "error": "INTERNAL_ERROR",
            "detail": f"{cfg['service']}内部错误（error_code={cfg['unknown_code']}），请查服务端日志"}
