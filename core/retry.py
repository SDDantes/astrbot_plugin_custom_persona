"""异步重试，含指数退避与抖动量。

语义与 OpenClaw 的 ``src/infra/retry.ts`` 一致。
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from typing import Any, TypeVar

logger = logging.getLogger("astrbot_plugin_custom_persona")

T = TypeVar("T")

DEFAULT_ATTEMPTS = 3
DEFAULT_MIN_DELAY_MS = 500
DEFAULT_MAX_DELAY_MS = 5_000
DEFAULT_JITTER = 0.2


def _apply_jitter(delay_ms: float, jitter: float) -> float:
    """对称抖动：在基准延迟的 +/- jitter 范围内分散。"""
    if jitter <= 0:
        return delay_ms
    fraction = random.random()
    offset = (fraction * 2 - 1) * jitter
    return max(0, delay_ms * (1 + offset))


async def retry_async(
    fn: Callable[[], Any],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    min_delay_ms: float = DEFAULT_MIN_DELAY_MS,
    max_delay_ms: float = DEFAULT_MAX_DELAY_MS,
    jitter: float = DEFAULT_JITTER,
    should_retry: Callable[[Exception], bool] | None = None,
    label: str = "",
) -> T:
    """调用 *fn*，失败时以指数退避和抖动进行重试。

    参数
    ----------
    fn:
        需要重试的异步可调用对象。
    attempts:
        最大尝试次数（含首次调用），必须 >= 1。
    min_delay_ms:
        首次重试前的基础延迟（毫秒）。
    max_delay_ms:
        单次延迟的上限（毫秒）。
    jitter:
        对计算出的延迟应用对称抖动的比例（0–1）。
    should_retry:
        可选断言函数。接收异常，返回 ``True`` 时才会重试。
        默认：重试所有异常。
    label:
        包含在日志消息中的标签，方便调用方识别操作。
    """
    attempts = max(1, attempts)
    min_delay_ms = max(0, min_delay_ms)
    max_delay_ms = max(min_delay_ms, max_delay_ms)
    jitter = max(0, min(1, jitter))
    check = should_retry if should_retry is not None else lambda _e: True

    last_err: Exception | None = None
    label_prefix = f"[{label}] " if label else ""

    for attempt in range(1, attempts + 1):
        try:
            result = await fn()
            if attempt > 1 and label:
                logger.debug("%sRetry succeeded on attempt %d", label_prefix, attempt)
            return result
        except Exception as exc:
            last_err = exc
            if attempt >= attempts or not check(exc):
                break
            base_delay = min_delay_ms * (2 ** (attempt - 1))
            delay = min(base_delay, max_delay_ms)
            delay = _apply_jitter(delay, jitter)
            delay = max(min_delay_ms, min(delay, max_delay_ms))
            logger.warning(
                "%sAttempt %d/%d failed: %s. Retrying in %.0fms...",
                label_prefix,
                attempt,
                attempts,
                exc,
                delay,
            )
            if delay > 0:
                await asyncio.sleep(delay / 1000)

    raise last_err  # type: ignore[misc]
