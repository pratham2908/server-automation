"""Replies should read like a person typed them, and know what the video is about."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.services.comment_reply_text import MAX_DESCRIPTION_CHARS, humanise_reply, video_context
from app.services.gemini import GeminiService

# --- stripping the machine-written tells -------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Thanks so much — that means a lot", "Thanks so much, that means a lot"),
        ("Thanks so much—that means a lot", "Thanks so much, that means a lot"),
        ("2020–2024 was wild", "2020, 2024 was wild"),
        ("Right - that's the trick", "Right, that's the trick"),
        ("Ha! — good catch", "Ha! good catch"),
        ("nice one —", "nice one"),
    ],
)
def test_dashes_are_replaced_with_ordinary_punctuation(raw: str, expected: str):
    assert humanise_reply(raw) == expected


def test_a_hyphen_inside_a_word_is_left_alone():
    assert humanise_reply("it's a step-by-step thing") == "it's a step-by-step thing"


def test_curly_quotes_and_the_ellipsis_character_become_plain_ones():
    assert humanise_reply("it’s “done”…") == 'it\'s "done"...'


def test_a_reply_wrapped_in_quotation_marks_is_unwrapped():
    assert humanise_reply('"love this one"') == "love this one"
    assert humanise_reply("“love this one”") == "love this one"


def test_an_empty_or_dash_only_reply_stays_empty_rather_than_becoming_punctuation():
    assert humanise_reply("") == ""
    assert humanise_reply("  —  ") == ""


def test_no_dash_survives_in_any_of_the_forms_a_model_produces():
    messy = "Great point — really – and honestly - spot on—thanks"
    out = humanise_reply(messy)
    assert "—" not in out and "–" not in out and " - " not in out


# --- what the model is told about the video ----------------------------------


def test_the_context_carries_the_title_and_the_description():
    ctx = video_context(
        {"title": "Load Balancer vs Reverse Proxy", "description": "All three sit in front of a server."}
    )
    assert "Title: Load Balancer vs Reverse Proxy" in ctx
    assert "Description: All three sit in front of a server." in ctx


def test_a_video_with_no_text_gives_no_context_rather_than_empty_labels():
    assert video_context({}) == ""
    assert video_context({"title": "  ", "description": None}) == ""


def test_a_long_description_is_cut_on_a_word_boundary_and_flagged_as_cut():
    ctx = video_context({"title": "t", "description": "word " * 500})
    desc = ctx.split("Description: ", 1)[1]
    assert desc.endswith("...") and len(desc) <= MAX_DESCRIPTION_CHARS + 3
    assert not desc.removesuffix("...").endswith("wor")  # never a half word


def test_description_whitespace_and_newlines_are_collapsed():
    assert "Description: a b c" in video_context({"title": "t", "description": "a\n\n  b \t c"})


# --- the prompts the engine now sends ----------------------------------------


class _Recorder:
    def __init__(self, reply: str) -> None:
        self.prompts: list[str] = []
        self.tasks: list[str] = []
        self._reply = reply

    async def __call__(self, prompt: str, specific_model: str | None = None, task: str = "unknown") -> str:
        self.prompts.append(prompt)
        self.tasks.append(task)
        return self._reply


def _gemini(reply: str) -> tuple[GeminiService, _Recorder]:
    service = object.__new__(GeminiService)  # skips the gateway client; only _generate is exercised
    recorder = _Recorder(reply)
    service._generate = recorder  # type: ignore[method-assign]
    return service, recorder


CONTEXT = "Title: Load Balancer vs Reverse Proxy\nDescription: All three sit in front of a server."


def test_the_reply_prompt_includes_the_video_context():
    service, rec = _gemini('{"reply": "ha yeah"}')
    asyncio.run(service.generate_comment_reply("so cool", "T", "instagram", "positive", video_context=CONTEXT))
    assert "Load Balancer vs Reverse Proxy" in rec.prompts[0] and "All three sit in front" in rec.prompts[0]
    assert rec.tasks == ["comment_reply"]


def test_the_reply_prompt_falls_back_to_the_title_when_there_is_no_context():
    service, rec = _gemini('{"reply": "ok"}')
    asyncio.run(service.generate_comment_reply("so cool", "My Title", "youtube", "positive"))
    assert "Title: My Title" in rec.prompts[0]


def test_the_classification_prompt_includes_the_video_context():
    service, rec = _gemini('[{"comment_id": "1", "sentiment": "positive"}]')
    result = asyncio.run(
        service.classify_comment_sentiment([{"comment_id": "1", "text": "wild"}], video_context=CONTEXT)
    )
    assert "Load Balancer vs Reverse Proxy" in rec.prompts[0]
    assert result == [{"comment_id": "1", "sentiment": "positive"}]


def test_classification_without_context_is_unchanged():
    service, rec = _gemini("[]")
    asyncio.run(service.classify_comment_sentiment([{"comment_id": "1", "text": "wild"}]))
    assert "this video" not in rec.prompts[0]


def test_the_model_is_told_not_to_use_dashes_or_stock_phrases():
    service, rec = _gemini('{"reply": "ok"}')
    asyncio.run(service.generate_comment_reply("x", "T", "instagram"))
    prompt = rec.prompts[0]
    assert "em dash" in prompt and "Thank you for your feedback" in prompt


def test_the_reply_that_comes_back_is_cleaned_even_if_the_model_ignored_the_rules():
    service, _ = _gemini('{"reply": "Thanks \\u2014 love that you noticed"}')
    reply = asyncio.run(service.generate_comment_reply("x", "T", "instagram"))
    assert reply == "Thanks, love that you noticed"


@pytest.mark.parametrize(("sentiment", "forbidden"), [("negative", "subscribe"), ("neutral", "subscribe")])
def test_a_complaint_or_question_is_never_pitched_a_follow(sentiment: str, forbidden: str):
    service, rec = _gemini('{"reply": "ok"}')
    asyncio.run(service.generate_comment_reply("x", "T", "youtube", sentiment))
    assert "Never ask them to follow or subscribe" in rec.prompts[0]


def test_unparseable_model_output_gives_an_empty_reply_so_the_caller_can_fall_back():
    service, _ = _gemini("not json at all")
    assert asyncio.run(service.generate_comment_reply("x", "T")) == ""
