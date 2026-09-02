"""One AI gateway client — the single provider entry point for this service.

Every model call this app makes goes through One AI. The gateway holds the
Vertex credentials, enforces this application's daily budget, and returns what
each call cost, so nothing here has to keep a rate table in step with Google's
published prices — which is exactly what ``ai_call_logger.GEMINI_PRICING`` used
to do, and silently got wrong for any model missing from it.

The SDK is synchronous by design. Call it from async code through
``asyncio.to_thread`` — a bare call would block the event loop for the whole
round trip and stall every other request in flight.
"""

from __future__ import annotations

from one_ai import ApiKey, OneAI

from app.config import get_settings
from app.logger import get_logger

logger = get_logger(__name__)

# The longest any single call site allows (video retention analysis waits 180s).
# Call sites still bound themselves with ``asyncio.wait_for``; this is the
# backstop that stops a worker thread outliving the request that spawned it,
# since cancelling the ``wait_for`` cannot itself kill the thread.
_TIMEOUT_S = 180.0

_client: OneAI | None = None


def get_one_ai() -> OneAI:
    """Return the process-wide One AI client, building it on first use.

    Lazy rather than constructed at import so a missing key surfaces on the
    first AI call — with a message naming the variable — instead of taking the
    whole server down at startup.
    """

    global _client
    if _client is not None:
        return _client

    settings = get_settings()
    if not settings.ONE_AI_API_KEY:
        raise RuntimeError(
            "ONE_AI_API_KEY is not set. Every AI call in this service is routed through the "
            "One AI gateway; set ONE_AI_URL and ONE_AI_API_KEY in the environment."
        )

    logger.info("Initializing One AI client (gateway: %s)", settings.ONE_AI_URL)
    _client = OneAI(settings.ONE_AI_URL, ApiKey(settings.ONE_AI_API_KEY), timeout_s=_TIMEOUT_S)
    return _client
