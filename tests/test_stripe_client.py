"""stripe_client tests — fakes only, no network."""

import os
import unittest

from hookrecon import stripe_client


class _FakeEvent:
    """Mimics stripe.Event: to_dict() is all fetch_events uses."""

    def __init__(self, event_id):
        self._id = event_id

    def to_dict(self):
        return {"id": self._id, "type": "checkout.session.completed", "created": 1727500000}


class _FakePage:
    def __init__(self, events):
        self._events = events

    def auto_paging_iter(self):
        return iter(self._events)


class FetchEventsTest(unittest.TestCase):
    def test_stops_at_limit_and_reports_capped(self):
        page = _FakePage([_FakeEvent(f"evt_{i}") for i in range(150)])
        events, capped = stripe_client.fetch_events(
            lambda **params: page, ["checkout.session.completed"], 0, 100
        )
        self.assertTrue(capped)
        self.assertEqual(len(events), 100)
        self.assertEqual(events[-1]["id"], "evt_99")

    def test_exactly_limit_is_complete_not_capped(self):
        page = _FakePage([_FakeEvent(f"evt_{i}") for i in range(100)])
        events, capped = stripe_client.fetch_events(
            lambda **params: page, ["checkout.session.completed"], 0, 100
        )
        self.assertFalse(capped)
        self.assertEqual(len(events), 100)

    def test_duplicates_do_not_consume_the_cap(self):
        # 140 yielded, only 81 unique — dedupe happens during collection.
        ids = ["evt_dup"] * 60 + [f"evt_{i}" for i in range(80)]
        page = _FakePage([_FakeEvent(i) for i in ids])
        events, capped = stripe_client.fetch_events(
            lambda **params: page, ["checkout.session.completed"], 0, 100
        )
        self.assertFalse(capped)
        self.assertEqual(len(events), 81)
        self.assertEqual({e["id"] for e in events}, {f"evt_{i}" for i in range(80)} | {"evt_dup"})

    def test_list_fn_receives_the_stripe_query_shape(self):
        seen = {}

        def list_fn(**params):
            seen.update(params)
            return _FakePage([])

        events, capped = stripe_client.fetch_events(
            list_fn, ["invoice.payment_succeeded", "invoice.payment_succeeded"], 12345, 100
        )
        self.assertEqual(
            seen, {"created": {"gte": 12345}, "types": ["invoice.payment_succeeded"], "limit": 100}
        )
        self.assertEqual((events, capped), ([], False))


class ResolveKeyTest(unittest.TestCase):
    _ENV = "HOOKRECON_TEST_KEY"

    def _set_key(self, value):
        os.environ[self._ENV] = value
        self.addCleanup(os.environ.pop, self._ENV, None)

    def test_prefix_modes(self):
        cases = {
            "sk_live_abc": "live",
            "rk_live_abc": "live",
            "sk_test_abc": "test",
            "rk_test_abc": "test",
            "not-a-stripe-key": "unknown",
        }
        for value, expected_mode in cases.items():
            with self.subTest(value=value):
                self._set_key(value)
                key, mode = stripe_client.resolve_key(self._ENV)
                self.assertEqual(key, value)
                self.assertEqual(mode, expected_mode)

    def test_missing_env_raises_stripe_error_naming_the_var(self):
        os.environ.pop(self._ENV, None)
        with self.assertRaises(stripe_client.StripeError) as ctx:
            stripe_client.resolve_key(self._ENV)
        self.assertIn(self._ENV, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
