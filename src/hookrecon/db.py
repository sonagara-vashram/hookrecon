"""Database layer — connect, run check SQL, EXPLAIN table extraction, privilege audit.

psycopg exceptions from run_check/dry_run/audit_privileges propagate verbatim:
drift.reconcile turns them into per-check errors, cli.py handles connect errors.
"""

import json
import os
import re
import sys
import urllib.parse

import psycopg

from hookrecon.config import translate_placeholders

_PRIVS = ("INSERT", "UPDATE", "DELETE", "TRUNCATE")
# to_regclass(%s) is NULL for a table that doesn't exist, which makes
# has_table_privilege return NULL (no privilege) instead of erroring — a
# check SQL naming a missing table must not break the audit.
_AUDIT_SQL = "SELECT " + ", ".join(
    f"has_table_privilege(current_user, to_regclass(%s), '{priv}')" for priv in _PRIVS
)
_TABLE_IDENT = r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)'
_TABLE_RE = re.compile(
    rf"\b(?:FROM|JOIN)\s+({_TABLE_IDENT}(?:\.{_TABLE_IDENT})?)", re.IGNORECASE
)
# EXPLAIN plans the statement but never executes it (ANALYZE would), so the
# bound value is irrelevant — it only has to type-infer cleanly.
_PROBE_PARAM = "hookrecon-doctor-probe"


class DbError(Exception):
    """Connection problem. The message names the exact env var; ``hint`` suggests a fix."""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.hint = hint


def connect(url_env: str) -> psycopg.Connection:
    """Connect to the Postgres URL stored in ``os.environ[url_env]``.

    A full postgresql:// URI (including ?sslmode=require) works as-is. On
    failure the psycopg error text is preserved verbatim, with the DSN scrubbed
    so the password never reaches output.
    """
    dsn = os.environ.get(url_env)
    if not dsn:
        raise DbError(f"environment variable {url_env} is not set")
    try:
        conn = psycopg.connect(dsn)
    except Exception as exc:
        msg = str(exc)
        # Scrub the password first (psycopg may cite individual DSN
        # components rather than the full URL), then the whole DSN.
        try:
            parsed = urllib.parse.urlsplit(dsn)
            if parsed.password:
                msg = msg.replace(parsed.password, "******")
        except Exception:
            pass
        msg = msg.replace(dsn, "<redacted>")
        raise DbError(
            msg,
            hint=f"if the server requires SSL, append ?sslmode=require to the {url_env} URL",
        ) from None
    # Read-only tool: one transaction per statement, so one check's SQL error
    # can't abort the connection for every later query ("current transaction
    # is aborted...").
    conn.autocommit = True
    return conn


def run_check(conn: psycopg.Connection, sql: str, param_value) -> list:
    """Run one already-linted check SELECT; returns all rows (0 rows = drift)."""
    translated = translate_placeholders(sql)
    return conn.execute(translated, {"param": param_value}).fetchall()


def make_run_sql(conn: psycopg.Connection, show_sql: bool = False):
    """The run_sql callable for drift.reconcile; --show-sql prints before executing."""

    def run_sql(sql: str, param_value) -> list:
        if show_sql:
            print(f"hookrecon: sql> {sql} ; param = {param_value!r}", file=sys.stderr)
        return run_check(conn, sql, param_value)

    return run_sql


def tables_from_plan(explain_json) -> list[str]:
    """Collect every ``Relation Name`` from an EXPLAIN (FORMAT JSON) payload,
    in plan order (subqueries/nested nodes included), deduped."""
    tables: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            name = node.get("Relation Name")
            if isinstance(name, str) and name not in tables:
                tables.append(name)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(explain_json)
    return tables


def extract_tables(conn: psycopg.Connection, sql: str) -> list[str]:
    """Tables touched by an already-linted SELECT, via EXPLAIN (FORMAT JSON).

    Planner only — never ANALYZE, which would execute the statement. Any error
    falls back to a regex over FROM|JOIN; this function never raises.
    """
    try:
        translated = translate_placeholders(sql)
        rows = conn.execute(f"EXPLAIN (FORMAT JSON) {translated}", {"param": _PROBE_PARAM}).fetchall()
        payload = rows[0][0]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return tables_from_plan(payload)
    except Exception:
        return _tables_from_regex(sql)


def _tables_from_regex(sql: str) -> list[str]:
    """Fallback table extraction over FROM|JOIN; handles schema-qualified and
    double-quoted identifiers. ponytail: naive heuristic — a string literal
    containing FROM/JOIN yields a false positive; the EXPLAIN path is accurate."""
    tables: list[str] = []
    for match in _TABLE_RE.finditer(sql):
        table = ".".join(part.strip('"') for part in match.group(1).split("."))
        if table not in tables:
            tables.append(table)
    return tables


def audit_privileges(conn: psycopg.Connection, tables: list[str]) -> list[tuple[str, list[str]]]:
    """Per table: which of INSERT/UPDATE/DELETE/TRUNCATE current_user can do.

    has_table_privilege catches PUBLIC grants and inherited roles, which
    information_schema misses. Returns only tables with >=1 writable priv.
    """
    writable: list[tuple[str, list[str]]] = []
    for table in tables:
        flags = conn.execute(_AUDIT_SQL, [table] * len(_PRIVS)).fetchone()
        privs = [priv for priv, ok in zip(_PRIVS, flags) if ok]
        if privs:
            writable.append((table, privs))
    return writable


def dry_run(conn: psycopg.Connection, sql: str, param_value=_PROBE_PARAM) -> None:
    """Execute the check SQL and ignore the rows — doctor's validity probe.

    Default param is the internal probe value; the rows are meaningless, only
    "did it execute" matters (psycopg errors propagate verbatim).
    """
    run_check(conn, sql, param_value)
