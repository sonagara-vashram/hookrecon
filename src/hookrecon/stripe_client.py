"""Stripe event fetching — the only module that talks to Stripe.

No prints (cli.py owns output); Stripe API errors propagate as exceptions.
"""

import os
import time
from collections.abc import Callable

import stripe

_PAGE_SIZE = 100  # Stripe's max page size


class StripeError(Exception):
    """Key problem. The message names the exact env var; ``hint`` suggests a fix."""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


def resolve_key(api_key_env: str) -> tuple[str, str]:
    """Read the key from the environment; returns (key, mode).

    Mode comes from the key prefix. Never echoes the key value.
    """
    key = os.environ.get(api_key_env)
    if not key:
        raise StripeError(f"environment variable {api_key_env} is not set")
    if key.startswith(("sk_live_", "rk_live_")):
        mode = "live"
    elif key.startswith(("sk_test_", "rk_test_")):
        mode = "test"
    else:
        mode = "unknown"
    return key, mode


def since_ts(days: int) -> int:
    """Unix timestamp for ``days`` ago."""
    return int(time.time()) - days * 86400


def fetch_events(
    list_fn: Callable[..., object],
    event_types: list[str],
    since_ts: int,
    limit: int,
) -> tuple[list[dict], bool]:
    """Page ``stripe.Event.list`` through ``list_fn``; returns (events, capped).

    Events come back as dicts (``to_dict()``), deduped by id AS COLLECTED so
    duplicates never consume the cap. Stops at ``limit`` unique events;
    capped=True only if more events were actually available — a fetch that
    ends exactly at ``limit`` is complete, not truncated.
    """
    if limit <= 0:
        return [], True
    events: list[dict] = []
    seen: set[str] = set()
    # Stripe caps `types` at 20 array entries — duplicate types across checks
    # would waste entries (and 400 past the cap)
    distinct_types = list(dict.fromkeys(event_types))
    page = list_fn(created={"gte": since_ts}, types=distinct_types, limit=_PAGE_SIZE)
    stream = page.auto_paging_iter()
    for event in stream:
        data = event.to_dict()
        event_id = data.get("id")
        if event_id in seen:
            continue
        seen.add(event_id)
        events.append(data)
        if len(events) >= limit:
            try:
                next(stream)  # peek: is there really more?
            except StopIteration:
                return events, False
            return events, True
    return events, False
