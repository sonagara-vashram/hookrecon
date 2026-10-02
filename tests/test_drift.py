"""SRS §11 — the four pure-logic cases for drift.reconcile.

Fake run_sql closure; no DB, no network, no mock library.
"""

import unittest
from datetime import datetime, timezone

from hookrecon import drift


def _event(eid, etype="checkout.session.completed", amount=8900, currency="usd", created=1727500000):
    """Stripe event dict, shape after stripe.Event.to_dict()."""
    return {
        "id": eid,
        "type": etype,
        "created": created,
        "data": {"object": {"id": "obj-" + eid, "currency": currency, "amount_total": amount}},
    }


CHECKOUT_CHECK = {
    "name": "completed checkout without order",
    "event": "checkout.session.completed",
    "sql": "SELECT 1 FROM orders WHERE stripe_session_id = $1",
    "param": "data.object.id",
    "money": "data.object.amount_total",
}

INVOICE_CHECK = {
    "name": "paid invoice not in billing table",
    "event": "invoice.payment_succeeded",
    "sql": "SELECT 1 FROM subscriptions WHERE stripe_invoice_id = $1",
    "param": "data.object.id",
    "money": "data.object.amount_paid",
}


def _reconcile(events, checks, run_sql):
    return drift.reconcile(events, checks, run_sql, "2026-10-01T00:00:00+00:00", 7, "test")


class ReconcileTest(unittest.TestCase):
    def test_missing_row_is_drift_and_processed_event_is_clean(self):
        events = [_event("evt_a"), _event("evt_b", amount=12000), _event("evt_c")]
        processed = {"obj-evt_b"}

        def run_sql(sql, param):
            return [(1,)] if param in processed else []

        report = _reconcile(events, [CHECKOUT_CHECK], run_sql)

        self.assertEqual(report["run"]["eventsScanned"], 3)
        self.assertEqual(report["run"]["lookbackDays"], 7)
        self.assertEqual(report["run"]["mode"], "test")
        check = report["checks"][0]
        self.assertEqual(check["status"], "drift")
        self.assertEqual(check["driftCount"], 2)
        self.assertEqual(check["amountAffected"], {"usd": 17800})
        self.assertIsNone(check["error"])
        self.assertEqual(check["skipped"], 0)
        self.assertEqual([d["eventId"] for d in check["drifts"]], ["evt_a", "evt_c"])
        rec = check["drifts"][0]
        self.assertEqual(rec["objectId"], "obj-evt_a")
        self.assertEqual(rec["amount"], 8900)
        self.assertEqual(rec["currency"], "usd")
        self.assertEqual(rec["url"], "https://dashboard.stripe.com/events/evt_a")
        self.assertEqual(
            rec["created"], datetime.fromtimestamp(1727500000, tz=timezone.utc).isoformat()
        )

    def test_duplicate_event_id_deduped_and_counted_once(self):
        events = [_event("evt_a"), _event("evt_a"), _event("evt_b")]
        processed = {"obj-evt_a"}

        def run_sql(sql, param):
            return [(1,)] if param in processed else []

        report = _reconcile(events, [CHECKOUT_CHECK], run_sql)

        self.assertEqual(report["run"]["eventsScanned"], 2)
        self.assertEqual(report["checks"][0]["driftCount"], 1)

    def test_sql_error_marks_check_errored_and_other_check_continues(self):
        events = [_event("evt_a"), _event("evt_i", etype="invoice.payment_succeeded")]

        def run_sql(sql, param):
            if "orders" in sql:
                raise RuntimeError("connection refused")
            return [(1,)]

        report = _reconcile(events, [CHECKOUT_CHECK, INVOICE_CHECK], run_sql)

        errored, healthy = report["checks"]
        self.assertEqual(errored["status"], "error")
        self.assertIn("connection refused", errored["error"])
        # the fake returns a row (processed) for the invoice check → clean
        self.assertEqual(healthy["status"], "clean")
        self.assertEqual(healthy["driftCount"], 0)
        self.assertIsNone(healthy["error"])

    def test_check_without_money_path_has_null_amount_and_no_money_figure(self):
        events = [_event("evt_a")]
        check = {k: v for k, v in CHECKOUT_CHECK.items() if k != "money"}

        def run_sql(sql, param):
            return []  # 0 rows = drift

        report = _reconcile(events, [check], run_sql)

        check_report = report["checks"][0]
        self.assertIsNone(check_report["drifts"][0]["amount"])
        self.assertEqual(check_report["amountAffected"], {})

    def test_non_integer_amount_is_excluded_from_money_totals(self):
        # money path pointing at a string field must not crash the run
        events = [_event("evt_a", amount="8900")]

        def run_sql(sql, param):
            return []

        report = _reconcile(events, [CHECKOUT_CHECK], run_sql)

        check = report["checks"][0]
        self.assertEqual(check["status"], "drift")
        self.assertEqual(check["amountAffected"], {})
        self.assertEqual(check["drifts"][0]["amount"], "8900")


if __name__ == "__main__":
    unittest.main()
