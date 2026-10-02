# hookrecon — Implementation Plan (Python)

**Status:** DRAFT for review · **Date:** 2026-10-01 · **Source:** SRS v1.0 + verified investigation (Stripe docs, psycopg docs, packaging docs, PyPI/npm landscape). All facts below carry sources in §10.

---

## 0. SRS amendments — review these first

The SRS said TypeScript. The stack pivots to Python; everything else in the SRS (3 commands, Postgres-only, read-only wedge, config spec, exit codes) stands. Investigation also surfaced four places where the SRS itself should change:

| # | SRS says | Plan says | Why (verified) |
|---|---|---|---|
| A1 | TypeScript, Node ≥ 20, ESM; deps `stripe`, `pg`, `commander` | **Python ≥ 3.10**; deps `stripe>=16,<17`, `psycopg[binary]>=3.3,<4`, argparse (stdlib) | Owner decision. psycopg 3 is the recommended Postgres driver; argparse is stdlib (no commander equivalent needed). `uvx hookrecon` = npx parity: uv auto-downloads Python if missing. |
| A2 | `doctor` pings Stripe via `GET /v1/balance` | Ping via **`GET /v1/events?limit=1`** | `/v1/balance` returns **403** on exactly the Events:Read-only restricted key the SRS's own recipe (§9.1) tells users to create. Events ping validates auth *and* the one permission the tool needs. |
| A3 | Write-privilege audit via `information_schema.role_table_grants WHERE grantee = current_user` | Audit via **`has_table_privilege(current_user, tbl, priv)`** per table | `role_table_grants` misses grants to `PUBLIC` (the default on many managed Postgres setups) and inherited roles — i.e. it misses the most common real misconfiguration. `has_table_privilege` catches PUBLIC, inheritance, and superuser. |
| A4 | Check SQL uses `$1` placeholder | Keep `$1` in **config** (SRS contract + Postgres culture), translate `$n → %s` at bind time | psycopg 3 only accepts `%s`/`%(name)s`; its extended protocol rewrites to server-side `$n` anyway. Translation is one regex, marked with a `ponytail:` ceiling comment. |
| A5 | `node:test` + one test file | `unittest` (stdlib), same one-file spirit + one small SQL-lint file | Python mirror of §11; lint is a trust boundary so it gets its own asserts. |

---

## 1. Stack (final)

| Decision | Choice | Notes |
|---|---|---|
| Language | Python ≥ 3.10, sync only (no asyncio) | Floor bound by psycopg 3 (`>=3.10`); stripe-python needs only 3.9. uvx users don't care about the floor; pipx users do. |
| Distribution | **PyPI** + `uvx hookrecon` (primary), `pipx install hookrecon` (fallback) | uv's `python-downloads` is `automatic` — `uvx` works with **no Python installed**. Windows gets `.exe` shims (PATH note goes in README). |
| Runtime deps | `stripe>=16,<17` · `psycopg[binary]>=3.3,<4` | `psycopg[binary]` wheels bundle libpq — no system Postgres client. psycopg is LGPL-3.0-only: fine for an MIT CLI (unmodified pip dependency); one README line. |
| CLI parsing | `argparse` (stdlib) | 3 subcommands + flags fully covered. argparse exits **2** on usage errors — same class as SRS exit 2; document it. |
| Config | `hookrecon.config.json` (unchanged) | No parser dep needed; stdlib `json`. |
| Tests | `unittest` + `unittest.mock`-free fakes | SRS §11 mirrored: fake "run SQL" callable, no DB, no network, no framework. |
| Build backend | hatchling | 2026 packaging recommendation for a single pure-Python package. |
| Publish | PyPI **Trusted Publishing** (OIDC, `pypa/gh-action-pypi-publish`) via GitHub Actions on tag | No API tokens. |
| Name | `hookrecon` | **Free on PyPI** (registry 404s today). Also free of conflicts on npm. Register at M6 publish time. |

---

## 2. Repository layout

```
hookrecon/
  pyproject.toml            # hatchling, [project.scripts] hookrecon = "hookrecon.cli:main"
  README.md                 # GIF at top (record at M4), restricted-key recipe, uvx line
  LICENSE                   # MIT (+ one line noting psycopg dependency is LGPL)
  .github/workflows/
    ci.yml                  # unittest matrix: 3.10 / 3.12 / 3.13 × ubuntu + windows
    release.yml             # tag → build → PyPI trusted publish
  src/hookrecon/
    __init__.py             # __version__
    cli.py                  # argparse wiring + init/check/doctor orchestration
    config.py               # load + validate hookrecon.config.json
    stripe_client.py        # fetch events: pagination, dedupe, --limit, mode detect
    db.py                   # connect, SELECT lint, $n→%s, run check SQL, dry-run, privilege audit
    drift.py                # pure diff logic — the only nontrivial module (no I/O)
    report.py               # ANSI table + JSON rendering, money formatting
  tests/
    drift_test.py           # SRS §11's four cases (fake sql fn) → shipped as test_drift.py
    sql_test.py             # SELECT-lint + $n→%s translation (trust boundary) → test_sql.py
```

---

## 3. Module design

### `config.py` — load + validate
- Resolve env-var **names** from config (defaults: `STRIPE_SECRET_KEY`, `DATABASE_URL`); values come only from the environment (SRS §9.5 — config file stays commit-safe).
- Validate: `lookbackDays` positive int; `checks` non-empty; per check: `name`, `event`, `sql`, optional `param` (default `data.object.id`), optional `money`.
- `event`: validated against a small known-list (the 3 canonical types); unknown types allowed with a warning.
- Distinct event types across checks **> 20 → validation error** (Stripe `types` filter accepts max 20 per call; 3 canonical checks make this theoretical).
- **SQL lint (trust boundary):** strip `--` and `/* */` comments → first token must be `SELECT` → reject `;` (except one optional trailing) → reject word-boundary write/DDL keywords (`INSERT UPDATE DELETE TRUNCATE DROP ALTER CREATE GRANT REVOKE COPY CALL DO VACUUM ANALYZE WITH`). `WITH`/CTEs rejected in v1 — users suppress false positives with plain `WHERE`, per SRS §7.
- Any validation error → `exit 2` naming the offending check (SRS §7).

### `stripe_client.py` — fetch events
- Key from env (name from config). Mode detect by prefix (`rk_live_` / `sk_live_` / `rk_test_` / `rk_test_`) for the banner; **loud warning on test keys** (SRS §9.1).
- `stripe.api_key = key`, then `stripe.Event.list(created={"gte": now - lookback}, types=[...], limit=100).auto_paging_iter()`.
- Break at `--limit` (default 10000) → set `capped=True` → warn "results incomplete" in report.
- **Dedupe by `event.id`** (pages can overlap while objects update mid-scan).
- **Lookback > 30 days → warn**: "Stripe retains events for 30 days; older events are silently absent" — warn and continue (do not silently undercount).
- Events converted with `.to_dict()`; a tiny hand-rolled path getter `get_path(obj, "data.object.amount_total")` (split `.`, walk dict) serves both `param` and `money`.

### `db.py` — connection, binding, audit
- `psycopg.connect(dsn)` — full `postgresql://…?sslmode=require` URL straight from env (psycopg accepts libpq conninfo/URI). On connect failure: show `pg` error verbatim + SSL hint (SRS §10).
- **Binding:** lint (§ above) → `re.sub(r"\$\d+", "%s", sql)` → `conn.execute(sql, [param_value])`. `ponytail:` naive regex could match `$1` inside a string literal — acceptable ceiling; the read-only DB user is the real boundary.
- **Free safety net:** psycopg 3's extended protocol refuses multi-statement strings at the driver level, so even a lint miss can't append `; DELETE …`.
- **Table extraction for the audit:** `EXPLAIN (FORMAT JSON) <user SQL>` with the param bound — planner only, **never `ANALYZE`** (that executes) — walk the plan tree collecting `Relation Name`. Fallback: regex over `FROM|JOIN` if EXPLAIN errors. (Regex alone misses subqueries/quoted/schema-qualified names.)
- **Privilege audit:** per extracted table —
  `SELECT has_table_privilege(current_user, %s, 'INSERT'), … 'UPDATE', … 'DELETE', … 'TRUNCATE'`
  Any true → print the SRS §9.2 warning verbatim-style: "⚠ connected user can WRITE to <table> — use a read-only user; hookrecon never writes, but don't take our word for it."
- **Doctor dry-run:** run each check's SQL with dummy param `"hookrecon-doctor-probe"`; **no exception = pass** (row count is meaningless, ignore it). Exception = doctor fail with verbatim error, exit 2.

### `drift.py` — pure diff (no I/O)
- Inputs: deduped events (dicts), validated checks, a `run_sql(sql, param) -> rows` callable (injected — that's what makes it testable without DB/network).
- Per check: events where `event["type"] == check.event` → per event: `param = get_path(event, check.param)`.
  - Param path missing (possible: old events render at their creation-time API version) → **soft skip**: count per check as `skipped`, don't crash, shown in report/JSON.
  - 0 rows from `run_sql` → drift record `{event_id, object_id, amount, currency, created, url}` where `url = https://dashboard.stripe.com/events/<event_id>` (0 rows = the record the webhook should have created is missing — corrected from this plan's original inverted wording after live acceptance testing); amount from `money` path or `null` if path absent (SRS §7: that check then contributes no money figure).
  - `run_sql` raises → that check `status: "error"` with the message, other checks continue (SRS §8 step 3).
- Money summary: sum per check per currency — currencies never merged (SRS §8 step 4).
- Output: report dict matching the §8.4 JSON schema exactly.

### `report.py` — rendering
- ANSI by hand (SRS forbids color libs): `\x1b[31m` red, `\x1b[32m` green, `\x1b[1m` bold, `\x1b[2m` dim. Respect `NO_COLOR` and non-tty → plain text.
- Table per SRS §8.3: banner (lookback, events scanned, mode) → per-check ✓/✗ blocks with drift rows (event id, object id, amount, date, dashboard URL) and the `→ Fix:` line → summary.
- **Money formatting:** minor units → human string with a **zero-decimal currency guard** (`JPY KRW VND CLP XOF XAF BIF DJF GNF KMF MGA PYG RWF UGX VUV` — never ÷100). `ponytail:` fixed set; extend on complaint.
- **$0 drifts are real:** free-trial invoices fire `invoice.payment_succeeded` with `amount_paid: 0` — render at $0.00; README tells users to suppress via WHERE filter.
- Dates: human table in local time (event `created` is Unix seconds UTC); `--json` always ISO-8601 UTC.
- `--json` → `json.dumps(report, indent=2)` per §8.4. `--quiet` → no banner, drift lines + summary only.

### `cli.py` — wiring
- argparse: subparsers `init` / `check` / `doctor`; `check` flags `--since <Nd> --json --show-sql --limit <n> --quiet` (flag `--since` overrides config `lookbackDays` — now defined).
- `init`: `input()` wizard (Stripe key env name → DB URL env name → lookback days) → writes `hookrecon.config.json` pre-filled with the 3 canonical checks → prints the restricted-key recipe → runs doctor preflight.
- `doctor`: env vars → Stripe events ping → DB `SELECT 1` → per-check dry-run → privilege audit. Exit 0 if all pass.
- Exit codes: **0** clean (including the explicit "0 events — widen --since" case), **1** drift, **2** fatal/config/any-check-errored (+ argparse usage errors). Single `sys.exit(main())` at the end — always finish rendering before exiting (SRS §10).

---

## 4. Command flows (refined, logic unchanged from SRS §5/§8)

- **init** = wizard → config file → key recipe → preflight.
- **check** = load/validate config → resolve env → fetch events (§stripe_client) → pure diff (§drift) → money summary → render (§report) → exit code.
- **doctor** = preflight only, ordered fail-soft with named failures, privilege audit last so it always gets shown.

---

## 5. Tests

`tests/test_drift.py` — SRS §11's four, verbatim logic, `unittest.TestCase`, fake `run_sql`:
1. 3 events, 1 has its DB row present → the other 2 are drifts, correct money sum, correct url shape.
2. Duplicate event id → deduped, counted once.
3. Check SQL raises → that check `status: "error"`, other checks still reconcile.
4. `money` path absent → amount `null`, excluded from totals.

`tests/test_sql.py` — the trust boundary:
5. Valid SELECT passes; `UPDATE …`, `SELECT 1; DROP TABLE x`, CTE (`WITH …`), leading comment + SELECT passes.
6. `$1`/`$2` → `%s`/`%s` translation; literal-heavy SQL untouched.

Run: `python -m unittest discover tests` (or `python -m unittest` with default discovery). No fixtures, no mocks library, no pytest.

---

## 6. CI / release

- `ci.yml`: matrix 3.10/3.12/3.13 × ubuntu/windows → `pip install -e .` → unittest → assert exit codes via a tiny scripted run of `--help`/config-error paths (no Stripe/DB in CI — SRS: "no e2e in CI").
- `release.yml`: on tag → hatchling build → PyPI trusted publishing (OIDC). Trigger: manually, at M6.

---

## 7. Milestones (evenings-sized, Python-adjusted SRS §12)

| Block | Deliverable |
|---|---|
| **M1** | Repo scaffold: pyproject (hatchling, entry point, deps), src-layout, `pip install -e .` runs; `config.py` + SQL lint; `drift.py` + both test files — all verifiable offline |
| **M2** | `stripe_client.py` (pagination, dedupe, limit, mode detect, 30-day warning) wired into `check --json` against a test-mode key |
| **M3** | `db.py`: connect, lint→translate→bind, per-check error isolation, `EXPLAIN` table extraction, privilege audit |
| **M4** | `report.py`: ANSI table, money summary (zero-decimal guard), JSON schema, exit codes, `--show-sql`, `--quiet`; **record the README GIF here** |
| **M5** | `doctor` (events ping, dry-run, audit) + `init` wizard |
| **M6** | README (GIF first, uvx line, restricted-key recipe, trust section, LGPL note), MIT, CI + release workflows, **PyPI publish**, GitHub topics (§9) |

---

## 8. Acceptance checklist (definition of done)

- [ ] `uvx hookrecon init` (or local `pip install -e .`) produces a working config against Stripe test mode + a Postgres with an `orders` table.
- [ ] E2E demo: create a checkout session in test mode, "forget" the insert → `check` reports the drift with the correct amount.
- [ ] `doctor` flags a write-privileged DB user (tested with both an owner user and a true read-only user).
- [ ] Doctor passes against the SRS-prescribed restricted key (Events: Read only) — this is what A2 fixed.
- [ ] All tests pass; exit codes 0/1/2 verified in CI.
- [ ] `hookrecon` registered on PyPI; `uvx hookrecon --help` works on a machine without Python (uv downloads it).

---

## 9. SEO / distribution (per your note — ranking comes from repo description + README H1 + topics)

- **Repo description:** `Find Stripe payments that never reached your database — read-only CLI, zero install: uvx hookrecon check`
- **README H1 (exact):** `# hookrecon — find Stripe payments that never reached your database`
- **First paragraph carries the search phrases:** "missing Stripe payments", "failed webhooks", "Stripe database reconciliation", "silent webhook failures".
- **GitHub topics:** `stripe`, `webhook`, `reconciliation`, `postgres`, `cli`, `payments`, `drift-detection`, `python`, `stripe-webhooks`, `database`
- **PyPI description** mirrors the README (PyPI ranks in Google for the same phrases).
- GIF at top before launch (SRS §6 note).

---

## 10. Verified-fact sources

Stripe [events/list](https://docs.stripe.com/api/events/list) (30-day retention, `types` ≤ 20, `delivery_success`) · [rate limits](https://docs.stripe.com/rate-limits) · [restricted keys](https://docs.stripe.com/keys/restricted-api-keys) + [403 changelog](https://docs.stripe.com/changelog/2016-10-19/insufficient-permissions-throw-403-error) · [Checkout Session](https://docs.stripe.com/api/checkout/sessions/object) / [Invoice](https://docs.stripe.com/api/invoices/object) / [PaymentIntent](https://docs.stripe.com/api/payment_intents/object) (money paths, nullability, $0 invoices) · [auto-pagination](https://docs.stripe.com/api/pagination/auto?lang=python) · [stripe-python](https://github.com/stripe/stripe-python) / [PyPI stripe 16](https://pypi.org/pypi/stripe/json) · psycopg [3 docs](https://www.psycopg.org/psycopg3/docs/basic/from_pg2.html) (%s only, multi-statement refusal) / [conninfo](https://www.psycopg.org/psycopg3/docs/api/conninfo.html) / [PyPI psycopg 3.3](https://pypi.org/pypi/psycopg/json) · Postgres [role_table_grants](https://www.postgresql.org/docs/current/infoschema-role-table-grants.html) (PUBLIC gap → `has_table_privilege`) · [read-only user recipe](https://www.crunchydata.com/blog/creating-a-read-only-postgres-user) · packaging [recommendations](https://packaging.python.org/guides/tool-recommendations) · [uv python downloads](https://docs.astral.sh/uv/concepts/python-versions/) (uvx needs no Python) · [PyPI trusted publishers](https://docs.pypi.org/trusted-publishers/) · landscape: `stripe-reconcile`/`stripe-reconciler`/`stripe-drift` = zero results on npm+PyPI; `hookrecon` free on PyPI; closest existing thing = [Stripe's own hand-rolled recipe](https://docs.stripe.com/webhooks/process-undelivered-events).

---

## 11. Deliberately excluded (one line each, revisit after dogfooding)

- **`delivery_success=false` filter** (discovered in the events API): catches *never-acknowledged* webhooks — a different failure mode than hookrecon's target (handler acked 200 then failed, or WAF answered for it). The full-scan diff is the product; this could become a bonus section in the report later.
- MySQL/SQLite/Mongo, other gateways, daemon/scheduler, replay — SRS non-goals, unchanged.
- Concurrency/asyncio — 10k events × ~1 ms sequential queries is fine; add none.
