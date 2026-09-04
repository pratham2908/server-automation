"""Smoke-test the Gemini service against the live One AI gateway.

Calls this app's own ``GeminiService`` methods — not the One AI SDK — so what it
proves is that the migrated service, its model fallback chain, its prompt
building, its response parsing and its cost accounting all still work end to end.

Each call reports the ``cost_usd`` One AI returned, read back out of the two
places the app records it: ``metrics_service`` and the ``ai_call_logs``
document. A ``None`` there means the gateway could not price the call and is
printed as ``unpriced`` — never as $0.00, which is the bug this migration fixed.

These are real, billed calls. Prompts are deliberately tiny.

    ./.venv/bin/python scripts/smoke_oneai.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.ai_call_logger import _pending_tasks, bind_ai_logger_db  # noqa: E402
from app.services.gemini import GeminiService  # noqa: E402
from app.services.metrics import metrics_service  # noqa: E402


class _CapturedLogs:
    """Stands in for the Mongo collection so a smoke run needs no database."""

    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []

    async def insert_one(self, doc: dict[str, Any]) -> SimpleNamespace:
        self.docs.append(doc)
        return SimpleNamespace(inserted_id="smoke")


def _money(cost: float | None) -> str:
    return "unpriced (None)" if cost is None else f"${cost:.8f}"


async def _report(label: str, response: object, logs: _CapturedLogs) -> float | None:
    """Print one call's response and what the app recorded it as costing."""

    # Call-log writes are deliberately fire-and-forget, so drain them before
    # reading the document back — otherwise this prints the previous call's row.
    if _pending_tasks:
        await asyncio.gather(*list(_pending_tasks))

    call = metrics_service.ai_last_calls[-1]
    doc = logs.docs[-1] if logs.docs else {}

    print(f"\n=== {label} ===")
    print(f"  response      : {str(response)[:220]}")
    print(f"  model         : {call['model']}")
    print(f"  tokens in/out : {call['input_tokens']}/{call['output_tokens']}")
    print(f"  duration_ms   : {call['duration_ms']}")
    print(f"  cost_usd      : {_money(call['cost_usd'])}   <- from metrics_service")
    print(f"  log cost_usd  : {_money(doc.get('cost_usd'))}   <- from ai_call_logs (priced={doc.get('priced')})")
    return call["cost_usd"]  # type: ignore[no-any-return]


async def main() -> int:
    logs = _CapturedLogs()
    bind_ai_logger_db(SimpleNamespace(ai_call_logs=logs))  # type: ignore[arg-type]

    service = GeminiService()
    costs: list[float | None] = []

    # 1 — text path, JSON array out (GeminiService._generate + _loads_json_array)
    sentiments = await service.classify_comment_sentiment(
        [
            {"comment_id": "c1", "text": "Love this, amazing work!", "author": "a"},
            {"comment_id": "c2", "text": "Buy followers at spam-link.example", "author": "b"},
        ]
    )
    costs.append(await _report("classify_comment_sentiment", sentiments, logs))

    # 2 — text path, JSON object out (GeminiService._generate + json.loads)
    reply = await service.generate_comment_reply("This helped a lot, thanks!", "How Gears Work")
    costs.append(await _report("generate_comment_reply", reply, logs))

    # 3 — multimodal path, inline image bytes (GeminiService.analyze_thumbnail)
    thumbnail = Path(__file__).resolve().parent / "fixtures" / "smoke_thumbnail.jpg"
    if thumbnail.exists():
        analysis = await service.analyze_thumbnail(str(thumbnail), "How Gears Work")
        costs.append(await _report("analyze_thumbnail (multimodal)", analysis, logs))
    else:
        print(f"\n=== analyze_thumbnail (multimodal) === SKIPPED, no image at {thumbnail}")

    priced = [c for c in costs if c is not None]
    print("\n" + "-" * 62)
    print(f"  calls            : {len(costs)}")
    print(f"  unpriced         : {len(costs) - len(priced)}")
    print(f"  total cost_usd   : ${sum(priced):.8f}")
    print(
        f"  metrics rollup   : ${metrics_service.ai_total_cost_usd:.8f} "
        f"over {metrics_service.ai_calls} calls, {metrics_service.ai_unpriced_calls} unpriced"
    )
    print("-" * 62)

    # Every log document must carry a cost key, null or not — a missing one is
    # the silent observability loss this migration was meant to rule out.
    assert all("cost_usd" in d for d in logs.docs), "an ai_call_logs document lost its cost field"
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
