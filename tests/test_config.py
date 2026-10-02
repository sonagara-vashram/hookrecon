"""Config validation extras — duplicate names, unknown keys."""

import json
import os
import tempfile
import unittest

from hookrecon import config

_BASE_CHECK = {
    "name": "captured payment without payment record",
    "event": "payment_intent.succeeded",
    "sql": "SELECT 1 FROM payments WHERE stripe_payment_intent_id = $1",
}


def _load_with(raw):
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(raw, f)
        path = f.name
    try:
        return config.load(path)
    finally:
        os.unlink(path)


class ConfigExtrasTest(unittest.TestCase):
    def test_duplicate_check_names_are_rejected(self):
        raw = {"checks": [_BASE_CHECK, dict(_BASE_CHECK)]}
        with self.assertRaises(config.ConfigError) as ctx:
            _load_with(raw)
        self.assertIn("duplicate check name", str(ctx.exception))

    def test_unknown_keys_are_reported_as_warnings(self):
        raw = {
            "urlenv": "MY_DB",  # typo at top level
            "stripe": {"lookbackDays": 3},
            "database": {"urlEnv": "MY_DB"},
            "checks": [dict(_BASE_CHECK, monee="data.object.amount_received")],  # typo in check
        }
        cfg, warnings = _load_with(raw)
        joined = "\n".join(warnings)
        self.assertIn("'urlenv'", joined)
        self.assertIn("'monee'", joined)
        self.assertEqual(cfg["database"]["urlEnv"], "MY_DB")


if __name__ == "__main__":
    unittest.main()
