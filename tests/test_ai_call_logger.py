"""AI call cost observability — pricing engine, call log persistence, and metrics rollup."""

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.ai_call_logger import (
    GEMINI_PRICING,
    bind_ai_logger_db,
    compute_cost,
    log_ai_call,
    schedule_ai_call_log,
)

# ------------------------------------------------------------------
# Fakes
# ------------------------------------------------------------------


class FakeInsertCollection:
    def __init__(self):
        self.docs: list[dict] = []

    async def insert_one(self, doc: dict):
        self.docs.append(doc)
        return SimpleNamespace(inserted_id="fake-id")


class ExplodingCollection:
    async def insert_one(self, doc: dict):
        raise RuntimeError("mongo is down")


class FakeLogDB:
    def __init__(self, collection=None):
        self.ai_call_logs = collection or FakeInsertCollection()


@pytest.fixture
def logger_db():
    db = FakeLogDB()
    bind_ai_logger_db(db)
    yield db
    bind_ai_logger_db(None)


# ------------------------------------------------------------------
# compute_cost
# ------------------------------------------------------------------


def test_flash_cost_uses_published_per_million_rates():
    # 1M input @ $0.30 + 1M output @ $2.50
    assert compute_cost("gemini-2.5-flash", 1_000_000, 1_000_000) == pytest.approx(2.80)


def test_flash_lite_is_cheaper_than_flash():
    lite = compute_cost("gemini-2.5-flash-lite", 100_000, 100_000)
    flash = compute_cost("gemini-2.5-flash", 100_000, 100_000)

    assert lite < flash
    assert lite == pytest.approx(0.05)  # 0.1M*0.10 + 0.1M*0.40


def test_pro_uses_standard_rates_below_the_context_threshold():
    # 1k input @ $1.25/1M + 1k output @ $10/1M
    assert compute_cost("gemini-2.5-pro", 1_000, 1_000) == pytest.approx(0.01125)


def test_pro_switches_to_long_context_rates_above_the_threshold():
    threshold = GEMINI_PRICING["gemini-2.5-pro"]["context_threshold"]

    # 250k input @ $2.50/1M + 1k output @ $15/1M — both legs use the long rate
    assert compute_cost("gemini-2.5-pro", threshold + 50_000, 1_000) == pytest.approx(0.625 + 0.015)


def test_pro_exactly_at_the_threshold_still_uses_standard_rates():
    """The long tier is for prompts *above* the threshold, not at it."""
    threshold = GEMINI_PRICING["gemini-2.5-pro"]["context_threshold"]

    assert compute_cost("gemini-2.5-pro", threshold, 0) == pytest.approx(threshold / 1_000_000 * 1.25)


def test_preview_model_uses_its_own_published_rates():
    """gemini-3-flash-preview was once proxied to flash rates; it now has published pricing."""
    # 10k input @ $0.50/1M + 10k output @ $3.00/1M
    assert compute_cost("gemini-3-flash-preview", 10_000, 10_000) == pytest.approx(0.005 + 0.03)
    assert compute_cost("gemini-3-flash-preview", 10_000, 10_000) != compute_cost("gemini-2.5-flash", 10_000, 10_000)


def test_unknown_model_costs_zero_rather_than_raising():
    assert compute_cost("some-unreleased-model", 10_000, 5_000) == 0.0


def test_failed_call_with_no_tokens_costs_zero():
    assert compute_cost("gemini-2.5-pro", 0, 0) == 0.0


# ------------------------------------------------------------------
# log_ai_call
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_ai_call_persists_one_document_per_call(logger_db):
    await log_ai_call("retention_analysis", "gemini-2.5-pro", 5_420, 1_200, 4231.5, True)

    assert len(logger_db.ai_call_logs.docs) == 1
    doc = logger_db.ai_call_logs.docs[0]
    assert doc["task"] == "retention_analysis"
    assert doc["model"] == "gemini-2.5-pro"
    assert doc["input_tokens"] == 5_420
    assert doc["output_tokens"] == 1_200
    assert doc["total_tokens"] == 6_620
    assert doc["cost_usd"] == pytest.approx(compute_cost("gemini-2.5-pro", 5_420, 1_200))
    assert doc["duration_ms"] == pytest.approx(4231.5)
    assert doc["success"] is True
    assert doc["timestamp"].tzinfo is not None


@pytest.mark.asyncio
async def test_failed_call_is_logged_with_zero_cost(logger_db):
    await log_ai_call("comment_reply", "gemini-2.5-pro", 0, 0, 120.0, False)

    doc = logger_db.ai_call_logs.docs[0]
    assert doc["success"] is False
    assert doc["cost_usd"] == 0.0


@pytest.mark.asyncio
async def test_logging_is_skipped_when_no_database_is_bound():
    bind_ai_logger_db(None)

    # Must not raise — this is the state during tests and early startup.
    await log_ai_call("channel_analysis", "gemini-2.5-flash", 10, 10, 1.0, True)


@pytest.mark.asyncio
async def test_database_failure_never_propagates_to_the_caller():
    bind_ai_logger_db(FakeLogDB(ExplodingCollection()))
    try:
        await log_ai_call("channel_analysis", "gemini-2.5-flash", 10, 10, 1.0, True)
    finally:
        bind_ai_logger_db(None)


@pytest.mark.asyncio
async def test_schedule_ai_call_log_writes_without_being_awaited_by_the_caller(logger_db):
    task = schedule_ai_call_log("topic_clustering", "gemini-2.5-flash", 100, 20, 50.0, True)

    await task

    assert logger_db.ai_call_logs.docs[0]["task"] == "topic_clustering"


# ------------------------------------------------------------------
# MetricsService rollup
# ------------------------------------------------------------------


def test_metrics_accumulates_cost_and_per_task_breakdown():
    from app.services.metrics import MetricsService

    metrics = MetricsService()
    metrics.record_ai_call("gemini-2.5-flash", 100.0, True, "channel_analysis", 1_000_000, 1_000_000)
    metrics.record_ai_call("gemini-2.5-flash", 200.0, True, "channel_analysis", 0, 0)
    metrics.record_ai_call("gemini-2.5-pro", 300.0, False, "comment_reply", 0, 0)

    assert metrics.ai_total_cost_usd == pytest.approx(2.80)
    assert metrics.ai_task_usage == {"channel_analysis": 2, "comment_reply": 1}
    assert metrics.ai_task_cost["channel_analysis"] == pytest.approx(2.80)
    assert metrics.ai_task_cost["comment_reply"] == 0.0


def test_metrics_recent_call_entries_carry_tokens_and_cost():
    from app.services.metrics import MetricsService

    metrics = MetricsService()
    metrics.record_ai_call("gemini-2.5-flash", 100.0, True, "scorecard_generation", 2_000, 500)

    entry = metrics.ai_last_calls[-1]
    assert entry["task"] == "scorecard_generation"
    assert entry["input_tokens"] == 2_000
    assert entry["output_tokens"] == 500
    assert entry["cost_usd"] == pytest.approx(compute_cost("gemini-2.5-flash", 2_000, 500))


def test_metrics_summary_exposes_cost_fields():
    from app.services.metrics import MetricsService

    metrics = MetricsService()
    metrics.record_ai_call("gemini-2.5-flash", 100.0, True, "content_generation", 1_000_000, 0)

    ai = metrics.get_summary()["ai"]
    assert ai["total_cost_usd"] == pytest.approx(0.30)
    assert ai["task_usage"] == {"content_generation": 1}
    assert ai["task_cost"]["content_generation"] == pytest.approx(0.30)


def test_record_ai_call_still_works_with_only_the_original_arguments():
    """Callers outside gemini.py must not break on the new signature."""
    from app.services.metrics import MetricsService

    metrics = MetricsService()
    metrics.record_ai_call("gemini-2.5-flash", 100.0, True)

    assert metrics.ai_calls == 1
    assert metrics.ai_task_usage == {"unknown": 1}


# ------------------------------------------------------------------
# gemini.py instrumentation
# ------------------------------------------------------------------

EXPECTED_TASK_LABELS = {
    "analyze_videos": "channel_analysis",
    "analyze_single_video": "single_video_analysis",
    "cluster_video_topics": "topic_clustering",
    "generate_video_content": "content_generation",
    "analyze_video_retention": "retention_analysis",
    "generate_platform_packaging": "platform_packaging",
    "classify_comment_sentiment": "comment_sentiment",
    "generate_comment_reply": "comment_reply",
    "analyze_comments": "comment_analysis",
    "generate_scorecard": "scorecard_generation",
    "extract_video_intelligence": "content_intelligence",
    "compare_content_patterns": "pattern_comparison",
}


def _gemini_tree() -> ast.Module:
    import app.services.gemini as gemini_module

    return ast.parse(Path(inspect.getfile(gemini_module)).read_text())


def _enclosing_method_tasks() -> dict[str, set[str]]:
    """Map each method name to the set of task= labels it passes to _generate*."""
    tasks: dict[str, set[str]] = {}
    for node in ast.walk(_gemini_tree()):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call) or not isinstance(inner.func, ast.Attribute):
                continue
            if inner.func.attr not in ("_generate", "_generate_with_video"):
                continue
            for keyword in inner.keywords:
                if keyword.arg == "task" and isinstance(keyword.value, ast.Constant):
                    tasks.setdefault(node.name, set()).add(keyword.value.value)
    return tasks


@pytest.mark.parametrize(("method", "expected_task"), sorted(EXPECTED_TASK_LABELS.items()))
def test_each_generate_call_site_passes_its_task_label(method, expected_task):
    assert _enclosing_method_tasks().get(method) == {expected_task}


def test_no_generate_call_site_anywhere_in_the_app_is_left_untagged():
    """Untagged calls would silently book spend under "unknown".

    Scans the whole package, not just gemini.py — video_service.py also reaches
    into ``_generate`` directly.
    """
    import app

    app_root = Path(inspect.getfile(app)).parent

    untagged = [
        f"{source.relative_to(app_root)}:{node.lineno}"
        for source in sorted(app_root.rglob("*.py"))
        for node in ast.walk(ast.parse(source.read_text()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("_generate", "_generate_with_video")
        and not any(keyword.arg == "task" for keyword in node.keywords)
    ]

    assert untagged == []


@pytest.mark.asyncio
async def test_generate_captures_usage_metadata_and_logs_the_call(logger_db, monkeypatch):
    from app.services.gemini import GeminiService
    from app.services.metrics import metrics_service

    response = SimpleNamespace(
        text='{"ok": true}',
        usage_metadata=SimpleNamespace(prompt_token_count=1_200, candidates_token_count=300),
    )

    async def fake_generate_content(**kwargs):
        return response

    service = object.__new__(GeminiService)
    service._client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate_content))
    )

    scheduled = []
    monkeypatch.setattr(
        "app.services.gemini.schedule_ai_call_log",
        lambda *args, **kwargs: scheduled.append(args) or None,
    )
    before = metrics_service.ai_calls

    result = await service._generate("prompt", specific_model="gemini-2.5-flash", task="channel_analysis")

    assert result == '{"ok": true}'
    assert metrics_service.ai_calls == before + 1
    task, model, input_tokens, output_tokens, _duration, success = scheduled[0]
    assert (task, model, input_tokens, output_tokens, success) == (
        "channel_analysis",
        "gemini-2.5-flash",
        1_200,
        300,
        True,
    )


@pytest.mark.asyncio
async def test_generate_logs_a_failed_call_with_zero_tokens(logger_db, monkeypatch):
    from app.services.gemini import GeminiService

    async def boom(**kwargs):
        raise RuntimeError("model exploded")

    service = object.__new__(GeminiService)
    service._client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=boom)))

    scheduled = []
    monkeypatch.setattr(
        "app.services.gemini.schedule_ai_call_log",
        lambda *args, **kwargs: scheduled.append(args) or None,
    )

    with pytest.raises(RuntimeError):
        await service._generate("prompt", specific_model="gemini-2.5-flash", task="channel_analysis")

    task, model, input_tokens, output_tokens, _duration, success = scheduled[0]
    assert (task, input_tokens, output_tokens, success) == ("channel_analysis", 0, 0, False)


@pytest.mark.asyncio
async def test_missing_usage_metadata_falls_back_to_zero_tokens(logger_db, monkeypatch):
    """Some preview models return no usage_metadata — that must not crash the call."""
    from app.services.gemini import GeminiService

    async def fake_generate_content(**kwargs):
        return SimpleNamespace(text="{}", usage_metadata=None)

    service = object.__new__(GeminiService)
    service._client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate_content))
    )

    scheduled = []
    monkeypatch.setattr(
        "app.services.gemini.schedule_ai_call_log",
        lambda *args, **kwargs: scheduled.append(args) or None,
    )

    await service._generate("prompt", specific_model="gemini-2.5-flash", task="channel_analysis")

    _task, _model, input_tokens, output_tokens, _duration, success = scheduled[0]
    assert (input_tokens, output_tokens, success) == (0, 0, True)


@pytest.mark.asyncio
async def test_thumbnail_analysis_logs_under_its_own_task_label(tmp_path, logger_db, monkeypatch):
    from app.services.gemini import GeminiService

    image = tmp_path / "thumb.jpg"
    image.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")

    async def fake_generate_content(**kwargs):
        return SimpleNamespace(
            text='{"score": 8}',
            usage_metadata=SimpleNamespace(prompt_token_count=800, candidates_token_count=100),
        )

    service = object.__new__(GeminiService)
    service._client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate_content))
    )

    scheduled = []
    monkeypatch.setattr(
        "app.services.gemini.schedule_ai_call_log",
        lambda *args, **kwargs: scheduled.append(args) or None,
    )

    result = await service.analyze_thumbnail(str(image), "Some title")

    assert result == {"score": 8}
    task, _model, input_tokens, output_tokens, _duration, success = scheduled[0]
    assert (task, input_tokens, output_tokens, success) == ("thumbnail_analysis", 800, 100, True)
