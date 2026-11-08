"""Which channel an AI call is being made for.

The Gemini call path (``gemini.py``) knows the task and model but not the channel,
and threading a ``channel_id`` argument through every method would touch dozens of
call sites. Instead the channel rides in a ``ContextVar``: the request middleware
sets it for ``/channels/{id}/…`` routes, and background jobs wrap their per-channel
work with :func:`ai_channel_scope` or :func:`with_ai_channel`. ``asyncio.to_thread``
and ``create_task`` copy the context, so the value follows the call into worker
threads and the fire-and-forget log writer.

A call made outside any scope is stored without a channel and shows up as
"unattributed" in the cost breakdown.
"""

from __future__ import annotations

import functools
import inspect
import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TypeVar

_current_channel: ContextVar[str | None] = ContextVar("ai_channel_id", default=None)

_CHANNEL_PATH = re.compile(r"/channels/([^/]+)")

T = TypeVar("T")


def current_ai_channel() -> str | None:
    return _current_channel.get()


def channel_from_path(path: str) -> str | None:
    """The channel id in a ``/…/channels/{id}/…`` URL path, else None."""

    match = _CHANNEL_PATH.search(path)
    return match.group(1) if match else None


def set_ai_channel(channel_id: str | None):
    """Set the channel for the current context; returns the token for ``reset``."""

    return _current_channel.set(channel_id or None)


def reset_ai_channel(token) -> None:
    _current_channel.reset(token)


@contextmanager
def ai_channel_scope(channel_id: str | None) -> Iterator[None]:
    token = _current_channel.set(channel_id or None)
    try:
        yield
    finally:
        _current_channel.reset(token)


def with_ai_channel(
    resolve: str | Callable[..., str | None],
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Decorator: run an async function inside :func:`ai_channel_scope`.

    ``resolve`` is the name of the argument holding the channel id, or a callable
    that receives the function's bound arguments (as keyword arguments) and returns it.
    """

    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        signature = inspect.signature(fn)

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            bound = signature.bind_partial(*args, **kwargs)
            channel = bound.arguments.get(resolve) if isinstance(resolve, str) else resolve(**bound.arguments)
            with ai_channel_scope(channel):
                return await fn(*args, **kwargs)

        return wrapper

    return decorator
