"""Human report rendering — pure string building; the caller (cli.py) prints.

Input is the §8.4 report dict from drift.reconcile. Colors are raw ANSI
(no color libs). No I/O except the stdout TTY check.
"""

import os
import sys
from datetime import datetime, timedelta

RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"

# ponytail: zero-decimal set is a fixed list of Stripe's common ones; extend on complaint.
_ZERO_DECIMAL = frozenset("JPY KRW VND CLP XOF XAF BIF DJF GNF KMF MGA PYG RWF UGX VUV".split())
_SYMBOLS = {"usd": "$", "eur": "€", "gbp": "£", "jpy": "¥"}

_FIX_LINE = (
    '      → Fix: open the URL above → "Resend" after fixing the handler,'
    " or insert the row manually."
)
_RULE = "─" * 66
_LEGEND = "drift = payment succeeded on Stripe, but its row is missing in your database"


def color_enabled() -> bool:
    """Color on a real terminal only; NO_COLOR and --json callers get plain text."""
    return "NO_COLOR" not in os.environ and sys.stdout.isatty()


def format_amount(minor_units: int, currency: str | None) -> str:
    """Minor units → human money: ``$89.00``, ``¥1234`` (zero-decimal), ``10.00 CHF``."""
    code = currency.lower() if currency else None
    if code and code.upper() in _ZERO_DECIMAL:
        text = str(minor_units)  # never divided — zero-decimal currencies
    else:
        # abs() avoids Python's floor-division gotcha on negatives:
        # -150 // 100 = -2 and -150 % 100 = 50, which would render "-2.50"
        # instead of the correct "-1.50".
        sign = "-" if minor_units < 0 else ""
        pos = abs(minor_units)
        text = f"{sign}{pos // 100}.{pos % 100:02d}"
    symbol = _SYMBOLS.get(code)
    if symbol:
        return f"{symbol}{text}"
    return f"{text} {code.upper()}" if code else text


def render_report(report: dict, quiet: bool = False, color: bool = False) -> str:
    """Render the report dict as the human table. Returns a string; caller prints."""
    run = report["run"]
    lines: list[str] = []
    if not quiet:
        end_local = datetime.fromisoformat(run["startedAt"]).astimezone()
        start_local = end_local - timedelta(days=run["lookbackDays"])
        n_events = run["eventsScanned"]
        lines += [
            "hookrecon — Stripe ↔ database reconciliation",
            f"Lookback: {_plural(run['lookbackDays'], 'day')}"
            f" ({_fmt_date(start_local)} – {_fmt_date(end_local)})"
            f" · {n_events:,} {_noun(n_events, 'event')} scanned · {run['mode']} mode",
            _c(DIM, _LEGEND, color),
            "",
        ]
    for check in report["checks"]:
        if quiet and check["status"] == "clean":
            continue
        lines += _check_lines(check, color)
        lines.append("")
    if not quiet:
        lines.append(_c(DIM, _RULE, color))
    lines.append(_summary(report))
    if not quiet and _all_landed(report):
        lines.append(_c(GREEN, "Nothing missing — every completed payment landed in your database.", color))
    return "\n".join(lines)


def _summary(report: dict) -> str:
    totals: dict = {}
    for check in report["checks"]:
        for currency, amount in check["amountAffected"].items():
            totals[currency] = totals.get(currency, 0) + amount
    total = sum(check["driftCount"] for check in report["checks"])
    summary = f"Summary: {_plural(total, 'drift')}"
    money = ", ".join(format_amount(amount, currency) for currency, amount in totals.items())
    if money:
        summary += f", ≈ {money} affected"
    if any(check["status"] == "error" for check in report["checks"]):
        summary += " · run `hookrecon doctor`"
    return summary


def _check_lines(check: dict, color: bool) -> list[str]:
    name = check["name"]
    if check["status"] == "clean":
        return [f"{_c(GREEN, '✓', color)} {_c(BOLD, name, color)} — {_c(GREEN, 'clean', color)}"]
    if check["status"] == "error":
        return [
            f"{_c(RED, '✗', color)} {_c(BOLD, name, color)}"
            f" — {_c(YELLOW, 'errored', color)}: {check['error']}",
            "  run `hookrecon doctor` for preflight",
        ]
    money = ", ".join(
        format_amount(amount, currency) for currency, amount in check["amountAffected"].items()
    )
    head = (
        f"{_c(RED, '✗', color)} {_c(BOLD, name, color)}"
        f" — {_c(RED, _plural(check['driftCount'], 'drift'), color)}"
    )
    if money:
        head += f", ≈ {money}"
    amounts = [
        format_amount(d["amount"], d["currency"]) if d["amount"] is not None else "-"
        for d in check["drifts"]
    ]
    id_width = max(len(str(d["objectId"] or "")) for d in check["drifts"])
    amount_width = max(map(len, amounts))
    lines = [head]
    for drift, amount in zip(check["drifts"], amounts):
        created = (
            datetime.fromisoformat(drift["created"]).astimezone() if drift["created"] else None
        )
        date = _fmt_date(created) if created else "?"
        lines.append(
            f"      {_c(BOLD, str(drift['objectId'] or '').ljust(id_width), color)}"
            f"  {_c(BOLD, amount.rjust(amount_width), color)}"
            f"  {_c(DIM, date, color)}"
        )
        lines.append(
            f"         {_c(DIM, '↳ ' + _short(drift['eventId']) + ' · ' + drift['url'], color)}"
        )
    if check.get("skipped"):
        lines.append(
            f"      {_c(DIM, _plural(check['skipped'], 'event') + ' skipped (missing field)', color)}"
        )
    lines.append(_FIX_LINE)
    return lines


def _short(event_id: str | None) -> str:
    """evt_3ULnzKDtbOMvzOcU1LSvAY46 → evt_3ULnzKDtbO…LSvAY46 — full id lives in the URL/JSON."""
    if not event_id or len(event_id) <= 24:
        return event_id or ""
    return f"{event_id[:14]}…{event_id[-7:]}"


def _all_landed(report: dict) -> bool:
    return (
        sum(check["driftCount"] for check in report["checks"]) == 0
        and not any(check["status"] == "error" for check in report["checks"])
    )


def _c(code: str, text: str, color: bool) -> str:
    return f"{code}{text}{RESET}" if color else text


def _fmt_date(d: datetime) -> str:
    return f"{d.strftime('%b')} {d.day}"  # local time, abbreviated month, no zero pad


def _noun(n: int, word: str) -> str:
    return word + ("s" if n != 1 else "")


def _plural(n: int, word: str) -> str:
    return f"{n} {_noun(n, word)}"
