"""Pure diff between Stripe events and database rows.

No I/O of any kind — the DB call is injected as a ``run_sql(sql, param_value)``
callable, which is what makes this testable without a DB or network.
"""

from collections.abc import Callable
from datetime import datetime, timezone


def get_path(obj, dotted_path: str):
    """Walk a dotted path through dicts/objects. None if any step is missing."""
    current = obj
    for part in dotted_path.split("."):
        if isinstance(current, dict):
            if part not in current:
                return None
            current = current[part]
        else:
            current = getattr(current, part, None)
    return current


def reconcile(
    events: list[dict],
    checks: list[dict],
    run_sql: Callable[[str, object], list],
    started_at: str,
    lookback_days: int,
    mode: str,
) -> dict:
    """Diff events against checks via run_sql. Returns the SRS §8.4 report dict
    (plus one additive ``skipped`` key per check).

    0 rows from run_sql = drift (the record the webhook should have created is
    missing), >=1 row = processed fine, raise = that check is ``error`` while
    the others continue. Events are deduped by id (first wins).
    """
    deduped = _dedupe(events)

    check_reports = []
    for check in checks:
        money_path = check.get("money")
        drifts: list[dict] = []
        skipped = 0
        error = None
        try:
            for event in deduped:
                if event.get("type") != check["event"]:
                    continue
                param = get_path(event, check["param"])
                if param is None:
                    skipped += 1  # soft skip — old events render at creation-time API version
                    continue
                # 0 rows = the record is missing = the webhook never landed
                if not run_sql(check["sql"], param):
                    drifts.append(_drift_record(event, param, money_path))
        except Exception as exc:  # never swallow the message — it goes in the report
            error = str(exc)
        check_reports.append(
            {
                "name": check["name"],
                "event": check["event"],
                "status": "error" if error else ("drift" if drifts else "clean"),
                "driftCount": len(drifts),
                "amountAffected": _money_total(drifts),
                "error": error,
                "drifts": drifts,
                "skipped": skipped,
            }
        )

    return {
        "run": {
            "startedAt": started_at,
            "lookbackDays": lookback_days,
            "eventsScanned": len(deduped),
            "mode": mode,
        },
        "checks": check_reports,
    }


def _dedupe(events: list[dict]) -> list[dict]:
    seen = set()
    unique = []
    for event in events:
        event_id = event.get("id")
        if event_id in seen:
            continue
        seen.add(event_id)
        unique.append(event)
    return unique


def _drift_record(event: dict, param, money_path: str | None) -> dict:
    # `if money_path is not None` rather than `or None` so a legitimate $0
    # amount (free-trial invoices) survives instead of becoming null.
    amount = get_path(event, money_path) if money_path is not None else None
    return {
        "eventId": event.get("id"),
        "objectId": param,
        "amount": amount,
        "currency": get_path(event, "data.object.currency"),
        "created": _iso_utc(event.get("created")),
        "url": f"https://dashboard.stripe.com/events/{event.get('id')}",
    }


def _iso_utc(unix_seconds):
    if unix_seconds is None:
        return None
    return datetime.fromtimestamp(unix_seconds, tz=timezone.utc).isoformat()


def _money_total(drifts: list[dict]) -> dict:
    totals: dict = {}
    for drift in drifts:
        amount = drift["amount"]
        # a misconfigured `money` path (e.g. pointing at a string id) must not
        # crash the run — it just contributes no money figure
        if not isinstance(amount, int) or isinstance(amount, bool):
            continue
        totals[drift["currency"]] = totals.get(drift["currency"], 0) + amount
    return totals
