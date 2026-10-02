"""SQL lint + $n → %(param)s translation — the trust boundary (PLAN §3 config.py)."""

import unittest

from hookrecon.config import ConfigError, lint_select, translate_placeholders


class LintSelectTest(unittest.TestCase):
    def test_valid_select_passes(self):
        sql = "SELECT 1 FROM orders WHERE stripe_session_id = $1"
        self.assertEqual(lint_select(sql), sql)

    def test_leading_comment_passes(self):
        # comments are ignored for validation; the SQL itself is returned unchanged
        self.assertEqual(
            lint_select("-- lookup the order\nSELECT 1 FROM orders"),
            "-- lookup the order\nSELECT 1 FROM orders",
        )

    def test_trailing_semicolon_passes(self):
        self.assertEqual(lint_select("SELECT 1 FROM orders;"), "SELECT 1 FROM orders;")

    def test_lowercase_select_passes(self):
        self.assertEqual(lint_select("  select 1 from orders  "), "select 1 from orders")

    def test_update_rejected(self):
        with self.assertRaises(ConfigError):
            lint_select("UPDATE x SET y = 1")

    def test_multi_statement_rejected(self):
        with self.assertRaises(ConfigError):
            lint_select("SELECT 1; DROP TABLE x")

    def test_cte_rejected(self):
        with self.assertRaises(ConfigError):
            lint_select("WITH cte AS (SELECT 1) SELECT * FROM cte")


class TranslatePlaceholdersTest(unittest.TestCase):
    def test_single_placeholder(self):
        self.assertEqual(
            translate_placeholders("SELECT 1 FROM t WHERE id = $1"),
            "SELECT 1 FROM t WHERE id = %(param)s",
        )

    def test_two_placeholders(self):
        self.assertEqual(translate_placeholders("SELECT $1 + $2"), "SELECT %(param)s + %(param)s")

    def test_repeated_dollar_one_passes_and_binds_one_value(self):
        sql = "SELECT 1 FROM t WHERE a = $1 OR b = $1"
        self.assertEqual(
            translate_placeholders(lint_select(sql)),
            "SELECT 1 FROM t WHERE a = %(param)s OR b = %(param)s",
        )

    def test_higher_placeholders_rejected_loudly(self):
        # binding one value into $2 would be silently wrong — reject at lint time
        with self.assertRaises(ConfigError) as ctx:
            lint_select("SELECT 1 FROM t WHERE a = $1 AND b = $2")
        self.assertIn("only $1 is supported", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
