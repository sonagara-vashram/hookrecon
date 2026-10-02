"""Load + validate hookrecon.config.json.

Pure logic only — no DB, no network, no prints (cli.py and report.py own output).
"""

import json
import re

KNOWN_EVENTS = (
    "checkout.session.completed",
    "invoice.payment_succeeded",
    "payment_intent.succeeded",
)

_MAX_EVENT_TYPES = 20  # Stripe events.list `types` filter accepts at most 20

_DEFAULT_API_KEY_ENV = "STRIPE_SECRET_KEY"
_DEFAULT_LOOKBACK_DAYS = 7
_DEFAULT_URL_ENV = "DATABASE_URL"
_DEFAULT_PARAM = "data.object.id"

_KNOWN_KEYS = {
    None: {"stripe", "database", "checks"},
    "stripe": {"apiKeyEnv", "lookbackDays"},
    "database": {"urlEnv"},
    "check": {"name", "event", "sql", "param", "money"},
}

_FORBIDDEN_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|TRUNCATE|DROP|ALTER|CREATE|GRANT|REVOKE|COPY|CALL"
    r"|DO|VACUUM|ANALYZE|WITH)\b",
    re.IGNORECASE,
)
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")


class ConfigError(Exception):
    """Invalid config or SQL. The message names the offending check."""


def load(path: str) -> tuple[dict, list[str]]:
    """Read + validate the config file. Returns (config, warnings).

    Fills defaults for optional fields; raises ConfigError (message names the
    offending check) on malformed JSON or wrong types. Unknown event types are
    allowed and come back as warning strings.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config file is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a JSON object")

    stripe_cfg = raw.get("stripe", {})
    if not isinstance(stripe_cfg, dict):
        raise ConfigError("'stripe' must be an object")
    api_key_env = stripe_cfg.get("apiKeyEnv", _DEFAULT_API_KEY_ENV)
    if not isinstance(api_key_env, str):
        raise ConfigError("'stripe'.'apiKeyEnv' must be a string")
    lookback_days = stripe_cfg.get("lookbackDays", _DEFAULT_LOOKBACK_DAYS)
    if isinstance(lookback_days, bool) or not isinstance(lookback_days, int) or lookback_days <= 0:
        raise ConfigError("'stripe'.'lookbackDays' must be a positive integer")

    db_cfg = raw.get("database", {})
    if not isinstance(db_cfg, dict):
        raise ConfigError("'database' must be an object")
    url_env = db_cfg.get("urlEnv", _DEFAULT_URL_ENV)
    if not isinstance(url_env, str):
        raise ConfigError("'database'.'urlEnv' must be a string")

    checks_raw = raw.get("checks")
    if not isinstance(checks_raw, list) or not checks_raw:
        raise ConfigError("'checks' must be a non-empty list")
    checks = [_validate_check(entry, i) for i, entry in enumerate(checks_raw)]

    names = [c["name"] for c in checks]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ConfigError(f"duplicate check name(s): {', '.join(repr(n) for n in dupes)}")

    distinct = {c["event"] for c in checks}
    if len(distinct) > _MAX_EVENT_TYPES:
        raise ConfigError(
            f"{len(distinct)} distinct event types across checks"
            f" — Stripe's `types` filter accepts at most {_MAX_EVENT_TYPES}"
        )

    unknown = sorted(set(raw) - _KNOWN_KEYS[None])
    unknown += sorted(set(stripe_cfg) - _KNOWN_KEYS["stripe"])
    unknown += sorted(set(db_cfg) - _KNOWN_KEYS["database"])
    for entry in checks_raw:
        if isinstance(entry, dict):
            unknown += sorted(set(entry) - _KNOWN_KEYS["check"])
    warnings = [
        f"unknown config key {key!r} — ignored (typo?)" for key in dict.fromkeys(unknown)
    ] + [
        f"check {c['name']!r}: event {c['event']!r} is not a known Stripe event type"
        f" (known: {', '.join(KNOWN_EVENTS)})"
        for c in checks
        if c["event"] not in KNOWN_EVENTS
    ]

    return (
        {
            "stripe": {"apiKeyEnv": api_key_env, "lookbackDays": lookback_days},
            "database": {"urlEnv": url_env},
            "checks": checks,
        },
        warnings,
    )


def _validate_check(entry, index: int) -> dict:
    where = f"checks[{index}]"
    if not isinstance(entry, dict):
        raise ConfigError(f"{where}: each check must be an object")
    name = entry.get("name")
    if not isinstance(name, str):
        raise ConfigError(f"{where}: 'name' must be a string")
    where = f"check {name!r}"
    event = entry.get("event")
    if not isinstance(event, str):
        raise ConfigError(f"{where}: 'event' must be a string")
    sql = entry.get("sql")
    if not isinstance(sql, str):
        raise ConfigError(f"{where}: 'sql' must be a string")
    try:
        lint_select(sql)
    except ConfigError as exc:
        raise ConfigError(f"{where}: {exc}") from None
    param = entry.get("param", _DEFAULT_PARAM)
    if not isinstance(param, str):
        raise ConfigError(f"{where}: 'param' must be a string")
    money = entry.get("money")
    if money is not None and not isinstance(money, str):
        raise ConfigError(f"{where}: 'money' must be a string")
    check = {"name": name, "event": event, "sql": sql, "param": param}
    if money is not None:
        check["money"] = money
    return check


def lint_select(sql: str) -> str:
    """Trust boundary: allow exactly one read-only SELECT, nothing else.

    Strips `--` and `/* */` comments, then requires the first token to be
    SELECT, rejects any `;` except one optional trailing one, and rejects
    word-boundary write/DDL keywords. Returns the SQL whitespace-trimmed.
    Naive about string literals (a literal containing '--' or a keyword is
    misjudged) — the read-only DB user is the real boundary.
    """
    stripped = _LINE_COMMENT_RE.sub(" ", _BLOCK_COMMENT_RE.sub(" ", sql))
    tokens = stripped.split()
    if not tokens or tokens[0].upper() != "SELECT":
        raise ConfigError("SQL must start with SELECT")
    body = stripped.strip()
    if body.endswith(";"):
        body = body[:-1].strip()
    if ";" in body:
        raise ConfigError("SQL must be a single statement (unexpected ';')")
    higher = sorted({int(n) for n in re.findall(r"\$(\d+)", body) if int(n) > 1})
    if higher:
        raise ConfigError(
            "only $1 is supported — hookrecon binds a single value; "
            f"found ${higher[0]}: repeat $1 instead"
        )
    forbidden = _FORBIDDEN_RE.search(body)
    if forbidden:
        raise ConfigError(
            f"SQL must be read-only: forbidden keyword {forbidden.group(0).upper()!r}"
        )
    return sql.strip()


def translate_placeholders(sql: str) -> str:
    """Rewrite Postgres-style $n placeholders to psycopg 3's %(param)s.

    Named placeholders allow the same $1 to appear multiple times in the query
    while binding only a single parameter value. $2+ is rejected at lint time —
    hookrecon binds exactly one value.

    ponytail: naive regex can match $n inside a string literal — acceptable
    ceiling; the read-only DB user is the real boundary.
    """
    return re.sub(r"\$\d+", "%(param)s", sql)


def build_default_config(api_key_env: str, url_env: str, lookback_days: int) -> dict:
    """The SRS §7 starter config: exactly the 3 canonical checks, nothing else.

    Pure — init serializes this and the user edits table/column names.
    """
    return {
        "stripe": {"apiKeyEnv": api_key_env, "lookbackDays": lookback_days},
        "database": {"urlEnv": url_env},
        "checks": [
            {
                "name": "completed checkout without order",
                "event": "checkout.session.completed",
                "sql": "SELECT 1 FROM orders WHERE stripe_session_id = $1",
                "money": "data.object.amount_total",
                "param": "data.object.id",
            },
            {
                "name": "paid invoice not in billing table",
                "event": "invoice.payment_succeeded",
                "sql": "SELECT 1 FROM subscriptions WHERE stripe_invoice_id = $1",
                "money": "data.object.amount_paid",
                "param": "data.object.id",
            },
            {
                "name": "captured payment without payment record",
                "event": "payment_intent.succeeded",
                "sql": "SELECT 1 FROM payments WHERE stripe_payment_intent_id = $1",
                "money": "data.object.amount_received",
                "param": "data.object.id",
            },
        ],
    }
