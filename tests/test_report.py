"""report.py — pure rendering: format_amount + render_report on a §8.4-shaped dict.

No I/O, no network, no DB.
"""

import unittest

from hookrecon.report import format_amount, render_report


def _report() -> dict:
    """Hand-built §8.4 report: one drift check, one clean, one errored."""
    return {
        "run": {
            "startedAt": "2026-10-01T12:00:00+00:00",
            "lookbackDays": 7,
            "eventsScanned": 1842,
            "mode": "live",
        },
        "checks": [
            {
                "name": "completed checkout without order",
                "event": "checkout.session.completed",
                "status": "drift",
                "driftCount": 2,
                "amountAffected": {"usd": 20900},
                "error": None,
                "skipped": 0,
                "drifts": [
                    {
                        "eventId": "evt_a",
                        "objectId": "cs_a",
                        "amount": 8900,
                        "currency": "usd",
                        "created": "2026-09-28T10:00:00+00:00",
                        "url": "https://dashboard.stripe.com/events/evt_a",
                    },
                    {
                        "eventId": "evt_b",
                        "objectId": "cs_b",
                        "amount": 12000,
                        "currency": "usd",
                        "created": "2026-09-30T10:00:00+00:00",
                        "url": "https://dashboard.stripe.com/events/evt_b",
                    },
                ],
            },
            {
                "name": "paid invoice not in billing table",
                "event": "invoice.payment_succeeded",
                "status": "clean",
                "driftCount": 0,
                "amountAffected": {},
                "error": None,
                "skipped": 0,
                "drifts": [],
            },
            {
                "name": "captured payment without payment record",
                "event": "payment_intent.succeeded",
                "status": "error",
                "driftCount": 0,
                "amountAffected": {},
                "error": "connection refused",
                "skipped": 0,
                "drifts": [],
            },
        ],
    }


class FormatAmountTest(unittest.TestCase):
    def test_usd_gets_symbol_and_two_decimals(self):
        self.assertEqual(format_amount(8900, "usd"), "$89.00")

    def test_jpy_zero_decimal_never_divided(self):
        self.assertEqual(format_amount(1234, "jpy"), "¥1234")

    def test_other_currency_uses_suffix_code(self):
        self.assertEqual(format_amount(1000, "chf"), "10.00 CHF")

    def test_none_currency_is_bare_number(self):
        self.assertEqual(format_amount(8900, None), "89.00")

    def test_currency_case_insensitive(self):
        self.assertEqual(format_amount(8900, "USD"), "$89.00")

    def test_negative_amount_uses_correct_sign_math(self):
        # -150 // 100 = -2 with Python floor division — the sign/abs branch
        # must render -1.50, not -2.50
        self.assertEqual(format_amount(-150, "usd"), "$-1.50")
        self.assertEqual(format_amount(-1234, "jpy"), "¥-1234")


class RenderReportTest(unittest.TestCase):
    def test_full_report_matches_srs_shape(self):
        out = render_report(_report())
        self.assertIn("hookrecon — Stripe ↔ database reconciliation", out)
        self.assertIn("Lookback: 7 days (", out)
        self.assertIn("· 1,842 events scanned · live mode", out)
        self.assertIn("✗ completed checkout without order — 2 drifts, ≈ $209.00", out)
        self.assertIn("✓ paid invoice not in billing table — clean", out)
        self.assertIn("✗ captured payment without payment record — errored: connection refused", out)
        self.assertIn("  run `hookrecon doctor` for preflight", out)

    def test_drift_row_carries_object_id_amount_and_url_detail_line(self):
        out = render_report(_report())
        rows = [line for line in out.splitlines() if "cs_a" in line]
        self.assertTrue(any("$89.00" in row for row in rows))
        detail = next(
            line
            for line in out.splitlines()
            if "https://dashboard.stripe.com/events/evt_a" in line
        )
        self.assertIn("evt_a", detail)

    def test_long_event_id_shortened_in_detail_line(self):
        report = _report()
        long_id = "evt_3ULnzKDtbOMvzOcU1LSvAY46"
        report["checks"][0]["drifts"][0]["eventId"] = long_id
        report["checks"][0]["drifts"][0]["url"] = f"https://dashboard.stripe.com/events/{long_id}"
        out = render_report(report)
        detail = next(line for line in out.splitlines() if "↳" in line and "evt_3ULnzKDtbO" in line)
        self.assertIn("…", detail)

    def test_fix_line_present_for_drift_check(self):
        self.assertIn(
            '→ Fix: open the URL above → "Resend" after fixing the handler,'
            " or insert the row manually.",
            render_report(_report()),
        )

    def test_legend_and_rule_in_full_mode_not_in_quiet(self):
        out = render_report(_report())
        self.assertIn("drift = payment succeeded on Stripe", out)
        self.assertIn("─", out)
        self.assertNotIn("drift = payment succeeded on Stripe", render_report(_report(), quiet=True))

    def test_summary_totals_and_doctor_hint_on_error(self):
        out = render_report(_report())
        self.assertIn("Summary: 2 drifts, ≈ $209.00 affected · run `hookrecon doctor`", out)

    def test_multiple_currencies_comma_joined_never_merged(self):
        report = _report()
        report["checks"][0]["amountAffected"] = {"usd": 8900, "eur": 500}
        self.assertIn("≈ $89.00, €5.00 affected", render_report(report))

    def test_skipped_line_rendered(self):
        report = _report()
        report["checks"][0]["skipped"] = 2
        self.assertIn("2 events skipped (missing field)", render_report(report, quiet=True))

    def test_singular_drift_wording(self):
        report = _report()
        report["checks"][0]["driftCount"] = 1
        report["checks"][0]["amountAffected"] = {"usd": 8900}
        report["checks"][0]["drifts"] = report["checks"][0]["drifts"][:1]
        out = render_report(report, quiet=True)
        self.assertIn("— 1 drift, ≈ $89.00", out)
        self.assertIn("Summary: 1 drift, ≈ $89.00 affected", out)

    def test_singular_event_wording(self):
        report = _report()
        report["run"]["eventsScanned"] = 1
        self.assertIn("· 1 event scanned · live mode", render_report(report))

    def test_all_landed_message_when_no_drifts(self):
        report = _report()
        for check in report["checks"]:
            check["status"] = "clean"
            check["driftCount"] = 0
            check["drifts"] = []
        out = render_report(report)
        self.assertIn("Nothing missing — every completed payment landed in your database.", out)

    def test_quiet_drops_banner_and_clean_checks_keeps_summary(self):
        out = render_report(_report(), quiet=True)
        self.assertNotIn("hookrecon — Stripe", out)
        self.assertNotIn("Lookback:", out)
        self.assertNotIn("paid invoice not in billing table", out)
        self.assertIn("evt_a", out)
        self.assertIn("Summary:", out)

    def test_color_false_has_no_escape_codes(self):
        self.assertNotIn("\x1b", render_report(_report(), color=False))

    def test_color_true_has_escape_codes(self):
        self.assertIn("\x1b[", render_report(_report(), color=True))


if __name__ == "__main__":
    unittest.main()
