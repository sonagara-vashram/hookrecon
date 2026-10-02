"""Pure tests for db.py — fake connections only, no real Postgres."""

import io
import json
import unittest
from contextlib import redirect_stderr

from hookrecon import db


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, rows=None, error=None):
        self.calls = []
        self._rows = rows or []
        self._error = error

    def execute(self, sql, params=None):
        if self._error is not None:
            raise self._error
        self.calls.append((sql, params))
        return FakeCursor(self._rows)


# Hand-built EXPLAIN (FORMAT JSON) payload: nested Subquery Scans over an
# Index Scan + Seq Scan, with a duplicate "orders" deeper in the tree.
_EXPLAIN_PLAN = [
    {
        "Plan": {
            "Node Type": "Subquery Scan",
            "Plans": [
                {
                    "Node Type": "Nested Loop",
                    "Plans": [
                        {
                            "Node Type": "Index Scan",
                            "Relation Name": "orders",
                            "Index Name": "orders_pkey",
                            "Plans": [],
                        },
                        {
                            "Node Type": "Seq Scan",
                            "Relation Name": 'public."Order Items"',  # schema-qualified + double-quoted
                            "Plans": [],
                        },
                    ],
                },
                {
                    "Node Type": "Subquery Scan",
                    "Plans": [
                        {
                            "Node Type": "Index Scan",
                            "Relation Name": "orders",  # duplicate — must dedupe
                            "Plans": [],
                        }
                    ],
                },
            ],
        }
    }
]


class TablesFromPlanTest(unittest.TestCase):
    def test_walks_nested_nodes_in_order_and_dedupes(self):
        self.assertEqual(
            db.tables_from_plan(_EXPLAIN_PLAN),
            ["orders", 'public."Order Items"'],
        )

    def test_accepts_bare_plan_dict(self):
        plan = {"Plan": {"Node Type": "Seq Scan", "Relation Name": "payments", "Plans": []}}
        self.assertEqual(db.tables_from_plan(plan), ["payments"])

    def test_no_relations(self):
        self.assertEqual(db.tables_from_plan([{"Plan": {"Node Type": "Result"}}]), [])


class RegexFallbackTest(unittest.TestCase):
    def test_simple_from(self):
        self.assertEqual(
            db._tables_from_regex("SELECT 1 FROM orders WHERE stripe_session_id = $1"),
            ["orders"],
        )

    def test_join_schema_qualified_and_quoted(self):
        sql = 'SELECT * FROM "public"."orders" o LEFT JOIN "payments" p ON p.order_id = o.id'
        self.assertEqual(db._tables_from_regex(sql), ["public.orders", "payments"])

    def test_derived_table_outer_from_ignored(self):
        sql = "SELECT * FROM (SELECT * FROM orders) s JOIN public.billing b ON b.id = s.id"
        self.assertEqual(db._tables_from_regex(sql), ["orders", "public.billing"])


class RunCheckTest(unittest.TestCase):
    def test_translates_placeholders_and_binds_param(self):
        conn = FakeConn(rows=[(1,)])
        rows = db.run_check(conn, "SELECT 1 FROM orders WHERE id = $1", "cs_123")
        self.assertEqual(rows, [(1,)])
        self.assertEqual(conn.calls, [("SELECT 1 FROM orders WHERE id = %s", ["cs_123"])])


class MakeRunSqlTest(unittest.TestCase):
    def test_quiet_by_default_loud_with_flag(self):
        conn = FakeConn(rows=[])
        err = io.StringIO()
        with redirect_stderr(err):
            db.make_run_sql(conn)("SELECT 1 FROM t WHERE id = $1", "x")
        self.assertEqual(err.getvalue(), "")
        with redirect_stderr(err):
            db.make_run_sql(conn, show_sql=True)("SELECT 1 FROM t WHERE id = $1", "x")
        self.assertIn("hookrecon: sql> ", err.getvalue())
        self.assertIn("SELECT 1 FROM t WHERE id = $1", err.getvalue())
        self.assertIn("'x'", err.getvalue())


class ExtractTablesTest(unittest.TestCase):
    def test_explain_path_translates_and_binds(self):
        payload = json.dumps(
            [{"Plan": {"Node Type": "Seq Scan", "Relation Name": "orders", "Plans": []}}]
        )
        conn = FakeConn(rows=[(payload,)])
        self.assertEqual(db.extract_tables(conn, "SELECT 1 FROM orders WHERE id = $1"), ["orders"])
        sql, params = conn.calls[0]
        self.assertTrue(sql.startswith("EXPLAIN (FORMAT JSON) "))
        self.assertNotIn("$1", sql)
        self.assertEqual(params, ["hookrecon-doctor-probe"])

    def test_never_raises_falls_back_to_regex(self):
        conn = FakeConn(error=RuntimeError("no db here"))
        sql = "SELECT 1 FROM orders WHERE id = $1"
        self.assertEqual(db.extract_tables(conn, sql), ["orders"])


class AuditPrivilegesTest(unittest.TestCase):
    def test_returns_only_writable_tables_with_their_privs(self):
        conn = FakeConn(rows=[(True, False, True, False)])
        self.assertEqual(
            db.audit_privileges(conn, ["orders"]), [("orders", ["INSERT", "DELETE"])]
        )
        self.assertEqual(conn.calls[0][1], ["orders"] * 4)

    def test_read_only_table_excluded(self):
        conn = FakeConn(rows=[(False, False, False, False)])
        self.assertEqual(db.audit_privileges(conn, ["orders"]), [])


if __name__ == "__main__":
    unittest.main()
