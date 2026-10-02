"""init's config side: build_default_config shape + write_config round-trip.

No stdin (write_config takes answers), no network, no Stripe/DB.
"""

import tempfile
import unittest

from hookrecon import cli, config

_CANONICAL = [
    (
        "checkout.session.completed",
        "SELECT 1 FROM orders WHERE stripe_session_id = $1",
        "data.object.amount_total",
    ),
    (
        "invoice.payment_succeeded",
        "SELECT 1 FROM subscriptions WHERE stripe_invoice_id = $1",
        "data.object.amount_paid",
    ),
    (
        "payment_intent.succeeded",
        "SELECT 1 FROM payments WHERE stripe_payment_intent_id = $1",
        "data.object.amount_received",
    ),
]


class BuildDefaultConfigTest(unittest.TestCase):
    def test_has_the_three_canonical_checks(self):
        built = config.build_default_config("STRIPE_SECRET_KEY", "DATABASE_URL", 7)
        self.assertEqual(
            [(c["event"], c["sql"], c["money"]) for c in built["checks"]], _CANONICAL
        )
        self.assertTrue(all(c["param"] == "data.object.id" for c in built["checks"]))

    def test_shape_matches_config_spec(self):
        built = config.build_default_config("STRIPE_SECRET_KEY", "DATABASE_URL", 7)
        self.assertEqual(built["stripe"], {"apiKeyEnv": "STRIPE_SECRET_KEY", "lookbackDays": 7})
        self.assertEqual(built["database"], {"urlEnv": "DATABASE_URL"})
        self.assertEqual([c["name"] for c in built["checks"]][0], "completed checkout without order")


class WriteConfigTest(unittest.TestCase):
    def test_written_config_round_trips_through_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/hookrecon.config.json"
            cli.write_config(path, "MY_KEY_ENV", "MY_URL_ENV", 30)
            cfg, warnings = config.load(path)
            self.assertEqual(warnings, [])
            self.assertEqual(cfg, config.build_default_config("MY_KEY_ENV", "MY_URL_ENV", 30))

    def test_file_is_indented_json_with_trailing_newline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/hookrecon.config.json"
            cli.write_config(path, "STRIPE_SECRET_KEY", "DATABASE_URL", 7)
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertTrue(text.endswith("\n"))
            self.assertIn('"lookbackDays": 7', text)


if __name__ == "__main__":
    unittest.main()
