import asyncio
import logging

logger = logging.getLogger(__name__)


async def with_retry(coro_fn, max_retries: int = 3, delay: float = 1.0, backoff: float = 2.0):
    """Call coro_fn() with exponential backoff. Raises the last exception if all retries fail."""
    last_exc = None
    wait = delay
    for attempt in range(1, max_retries + 1):
        try:
            return await coro_fn()
        except Exception as exc:
            last_exc = exc
            if not getattr(exc, "retryable", True):
                raise
            if attempt < max_retries:
                retry_after = getattr(exc, "retry_after", None)
                sleep_for = max(wait, retry_after) if retry_after is not None else wait
                logger.warning(f"Attempt {attempt}/{max_retries} failed: {exc}. Retrying in {sleep_for:.1f}s")
                await asyncio.sleep(sleep_for)
                wait *= backoff
    raise last_exc
