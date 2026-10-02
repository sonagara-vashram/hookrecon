"""argparse wiring + the three commands: init, check, doctor.

All printing lives here; config/db/stripe_client/drift stay silent.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

import stripe

from hookrecon import config, db, drift, stripe_client
from hookrecon.report import BOLD, DIM, GREEN, RED, RESET, color_enabled, render_report

_CONFIG_FILE = "hookrecon.config.json"
_AUTH_HINT = "is this a restricted key with Events: Read?"
_TEST_KEY_WARNING = "using a TEST key — results cover test-mode events only"
_WRITE_WARNING = (
    "connected user can WRITE to {table} — use a read-only user;"
    " hookrecon never writes, but don't take our word for it."
)
_KEY_RECIPE = """\
Next: create a Stripe RESTRICTED key (Dashboard → Developers → API keys) with only:
  - Events: Read
  - Checkout Sessions: Read, Invoices: Read, Payment Intents: Read (for the default checks)
hookrecon only ever reads — it never calls a Stripe write endpoint.
Store the key in the {api_key_env} environment variable, never in the config file."""
_SINCE_RE = re.compile(r"(\d+)d")


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return number


def main(argv: list[str] | None = None) -> int:
    # Windows pipes can default to a legacy codepage (cp1252) on Python < 3.15
    # — the report's ✓/✗/⚠ glyphs must degrade, never crash the run.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass
    parser = argparse.ArgumentParser(
        prog="hookrecon",
        description="Find Stripe payments that never reached your database.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Write a starter hookrecon.config.json")

    check = subparsers.add_parser("check", help="Reconcile Stripe events against your database")
    check.add_argument("--since", metavar="Nd", help="Override the configured lookback, e.g. 30d")
    check.add_argument("--json", action="store_true", help="Print the machine-readable JSON report")
    check.add_argument(
        "--show-sql", action="store_true", help="Print every SQL statement before running it"
    )
    check.add_argument(
        "--limit",
        type=_positive_int,
        default=10000,
        help="Safety cap on Stripe events fetched (default 10000)",
    )
    check.add_argument("--quiet", action="store_true", help="Only drift lines + summary, no banner")

    subparsers.add_parser(
        "doctor", help="Preflight: env vars, Stripe ping, DB connection, privileges"
    )

    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            return _check(args, parser)
        if args.command == "init":
            return _init()
        return run_doctor(_CONFIG_FILE)
    except KeyboardInterrupt:
        print("\nhookrecon: interrupted", file=sys.stderr)
        return 2


def run_doctor(cfg_path: str) -> int:
    """Ordered preflight: config → env → Stripe ping → DB → dry runs → privilege audit.

    Fail-soft: a failed step marks the run (exit 2) but independent later steps
    still run; steps depending on a failed one print a skip line. The privilege
    audit is advisory — ⚠ lines never fail the run. Never prints key/DSN values.
    """
    color = color_enabled()
    failed = False
    print(_c(BOLD, "hookrecon doctor — preflight", color))

    try:
        cfg, warnings = config.load(cfg_path)
    except config.ConfigError as exc:
        print(_step(False, f"config: {exc}", color))
        if "not found" in str(exc):
            print(f"  hint: run `hookrecon init` in your project directory to create {cfg_path}")
        return 2
    print(_step(True, f"config: {cfg_path}", color))
    for warning in warnings:
        print(f"  ⚠ {warning}")

    key_env, url_env = cfg["stripe"]["apiKeyEnv"], cfg["database"]["urlEnv"]
    key = os.environ.get(key_env)
    dsn = os.environ.get(url_env)
    missing = [name for name, value in ((key_env, key), (url_env, dsn)) if not value]
    if missing:
        failed = True
        for name in missing:
            print(_step(False, f"env var {name} is not set", color))
        first = missing[0]
        print(
            f"  hint: set it in this shell first, e.g. export {first}=\"...\""
            f" (Windows: setx {first} \"...\") — hookrecon reads keys from the environment, not from files"
        )
    else:
        print(_step(True, f"env vars {key_env}, {url_env} set", color))

    if not key:
        print(_skip(f"Stripe ping — {key_env} is not set", color))
    elif not _doctor_stripe(key_env, color):
        failed = True

    conn = None
    if not dsn:
        print(_skip(f"database checks — {url_env} is not set", color))
    else:
        try:
            conn = db.connect(url_env)
            conn.execute("SELECT 1").fetchall()
        except db.DbError as exc:
            failed = True
            print(_step(False, f"database: {exc}", color))
            if exc.hint:
                print(f"  hint: {exc.hint}")
        except Exception as exc:  # connected, but SELECT 1 itself failed
            failed = True
            print(_step(False, f"database: SELECT 1: {exc}", color))
        else:
            print(_step(True, "database: connected (SELECT 1 ok)", color))
            for check in cfg["checks"]:
                if not _doctor_check(conn, check, color):
                    failed = True
            _doctor_privileges(conn, cfg["checks"], color)
        finally:
            if conn is not None:
                conn.close()

    return 2 if failed else 0


def _doctor_stripe(key_env: str, color: bool) -> bool:
    """Ping via the events list — /v1/balance 403s on Events:Read-only restricted keys."""
    mode = "unknown"
    try:
        _, mode = stripe_client.resolve_key(key_env)
        stripe.api_key = os.environ[key_env]
        stripe.Event.list(limit=1)
    except Exception as exc:
        print(_step(False, f"Stripe ping ({mode} mode): {exc}", color))
        if mode == "test":
            print(f"  ⚠ {_TEST_KEY_WARNING}")
        print(f"  hint: {_AUTH_HINT}")
        return False
    print(_step(True, f"Stripe: events API reachable ({mode} mode)", color))
    if mode == "test":
        print(f"  ⚠ {_TEST_KEY_WARNING}")
    return True


def _doctor_check(conn, check: dict, color: bool) -> bool:
    try:
        db.dry_run(conn, check["sql"])
    except Exception as exc:
        print(_step(False, f"check {check['name']!r}: {exc}", color))
        return False
    print(_step(True, f"check {check['name']!r}: SQL dry run ok", color))
    return True


def _doctor_privileges(conn, checks: list[dict], color: bool) -> None:
    """Advisory audit — ⚠ lines inform but never fail the run."""
    print(_c(BOLD, "privilege audit", color))
    tables = list(
        dict.fromkeys(table for check in checks for table in db.extract_tables(conn, check["sql"]))
    )
    try:
        writable = db.audit_privileges(conn, tables)
    except Exception as exc:
        print(f"  ⚠ privilege audit could not run: {exc}")
        return
    for table, _privs in writable:
        print(f"  ⚠ {_WRITE_WARNING.format(table=table)}")
    if not writable:
        print(_step(True, "connected user cannot write to any audited table", color))


def _init() -> int:
    """Wizard → config file → restricted-key recipe → doctor preflight.

    Exit 0 once the config is written; preflight failures stay informational.
    """
    if os.path.exists(_CONFIG_FILE):
        print(
            f"hookrecon: {_CONFIG_FILE} already exists — not overwriting; delete it first",
            file=sys.stderr,
        )
        return 2

    print("hookrecon init — press Enter to accept the [default]")
    api_key_env = _ask("Stripe secret key env var name", "STRIPE_SECRET_KEY")
    url_env = _ask("Database URL env var name", "DATABASE_URL")
    lookback_days = _ask_days()
    write_config(_CONFIG_FILE, api_key_env, url_env, lookback_days)
    print(f"hookrecon: wrote {_CONFIG_FILE} — edit each check's table/columns to match your schema")
    print()
    print(_KEY_RECIPE.format(api_key_env=api_key_env))
    print()
    if run_doctor(_CONFIG_FILE) != 0:
        print()
        print("hookrecon: preflight found issues — fix them, then run `hookrecon doctor`")
    return 0


def write_config(path: str, api_key_env: str, url_env: str, lookback_days: int) -> None:
    """Serialize build_default_config — answers in, file out; init's testable half."""
    cfg = config.build_default_config(api_key_env, url_env, lookback_days)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


def _ask(prompt: str, default: str) -> str:
    try:
        answer = input(f"{prompt} [{default}]: ").strip()
    except EOFError:  # piped/closed stdin — take the default rather than crash
        return default
    return answer or default


def _ask_days() -> int:
    for _ in range(2):  # one re-prompt on invalid input, then the default
        try:
            raw = input("Lookback days [7]: ").strip()
        except EOFError:
            return 7
        if not raw:
            return 7
        try:
            days = int(raw)
        except ValueError:
            days = 0
        if days > 0:
            return days
        print("  please enter a positive number (or press Enter for 7)")
    return 7


def _check(args, parser: argparse.ArgumentParser) -> int:
    started_at = datetime.now(timezone.utc).isoformat()

    try:
        cfg, warnings = config.load(_CONFIG_FILE)
    except config.ConfigError as exc:
        print(f"hookrecon: {exc}", file=sys.stderr)
        if "not found" in str(exc):
            print(
                f"hookrecon: run `hookrecon init` in your project directory to create {_CONFIG_FILE}",
                file=sys.stderr,
            )
        return 2

    try:
        lookback_days = _parse_since(args.since, cfg["stripe"]["lookbackDays"])
    except ValueError:
        parser.error(f"invalid --since {args.since!r} (expected e.g. 7d)")  # exits 2

    try:
        key, mode = stripe_client.resolve_key(cfg["stripe"]["apiKeyEnv"])
    except stripe_client.StripeError as exc:
        print(f"hookrecon: {exc}", file=sys.stderr)
        if "not set" in str(exc):
            var = cfg["stripe"]["apiKeyEnv"]
            print(
                f"hookrecon: set it in this shell first, e.g. export {var}=\"...\""
                f" (Windows: setx {var} \"...\") — hookrecon reads keys from the environment, not from files",
                file=sys.stderr,
            )
        else:
            print(f"hookrecon: {_AUTH_HINT}", file=sys.stderr)
        return 2

    if mode == "test":
        warnings.append(f"⚠ {_TEST_KEY_WARNING}")
    if lookback_days > 30:
        warnings.append("⚠ Stripe retains events for only 30 days — older events are silently absent")
    for warning in warnings:
        print(f"hookrecon: {warning}", file=sys.stderr)

    try:
        stripe.api_key = key
        events, capped = stripe_client.fetch_events(
            lambda **params: stripe.Event.list(**params),
            [check["event"] for check in cfg["checks"]],
            stripe_client.since_ts(lookback_days),
            args.limit,
        )
    except Exception as exc:  # Stripe auth/network or anything else — never a traceback
        print(f"hookrecon: Stripe fetch failed: {exc}", file=sys.stderr)
        if isinstance(exc, stripe.StripeError):
            print(f"hookrecon: {_AUTH_HINT}", file=sys.stderr)
        return 2

    if capped:
        print(
            f"hookrecon: ⚠ fetch stopped at --limit {args.limit} — results are incomplete",
            file=sys.stderr,
        )
    if not events:
        print(
            "hookrecon: 0 events in lookback — widen --since if this is unexpected",
            file=sys.stderr,
        )

    try:
        conn = db.connect(cfg["database"]["urlEnv"])
    except db.DbError as exc:
        print(f"hookrecon: {exc}", file=sys.stderr)
        if exc.hint:
            print(f"hookrecon: hint: {exc.hint}", file=sys.stderr)
        return 2

    try:
        run_sql = db.make_run_sql(conn, show_sql=args.show_sql)
        report = drift.reconcile(events, cfg["checks"], run_sql, started_at, lookback_days, mode)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_report(report, quiet=args.quiet, color=color_enabled()))

    if any(check["status"] == "error" for check in report["checks"]):
        return 2
    if any(check["driftCount"] > 0 for check in report["checks"]):
        return 1
    return 0


def _parse_since(value: str | None, config_days: int) -> int:
    """--since <Nd> in days; None -> config lookback. Raises ValueError if invalid."""
    if value is None:
        return config_days
    match = _SINCE_RE.fullmatch(value.strip())
    days = int(match.group(1)) if match else 0
    if days <= 0:
        raise ValueError(value)
    return days


def _c(code: str, text: str, color: bool) -> str:
    return f"{code}{text}{RESET}" if color else text


def _step(ok: bool, text: str, color: bool) -> str:
    return f"{_c(GREEN if ok else RED, '✓' if ok else '✗', color)} {text}"


def _skip(text: str, color: bool) -> str:
    return f"  {_c(DIM, 'skip: ' + text, color)}"


if __name__ == "__main__":
    sys.exit(main())
