import unittest
from datetime import datetime, timedelta

from books_assistant.funding_policy import cash_check, fingerprint, money


class TestFundingPolicy(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 13, 12)
        self.snapshot = dict(name="bank-check", available_balance=8000, pending_outflows=1000,
                             minimum_buffer=1000, balance_as_of=self.now)

    def check(self, amount=6000, reserved=2000):
        return cash_check(self.snapshot, amount, reserved, self.now)

    def test_shortfall_includes_pending_reservations_and_buffer(self):
        result = self.check()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["available_for_payout"], 4000)
        self.assertEqual(result["shortfall"], 2000)
        self.assertEqual(result["after_payout"], -2000)

    def test_bank_pending_not_subtracted_twice(self):
        # The bank already deducted a 2000 transfer from its available balance.
        # Do not also enter it as a pending outflow or keep its reservation active.
        self.snapshot.update(available_balance=6000, pending_outflows=0, minimum_buffer=0)
        self.assertEqual(self.check(amount=6000, reserved=0)["status"], "sufficient")

    def test_batch_amount_and_other_approvals_share_available_cash(self):
        self.assertEqual(self.check(amount=6000, reserved=0)["status"], "sufficient")
        self.assertEqual(self.check(amount=6000, reserved=1000)["shortfall"], 1000)

    def test_exact_boundary_and_one_cent_shortfall(self):
        self.assertEqual(self.check(amount=4000)["status"], "sufficient")
        self.assertEqual(self.check(amount="4000.01")["shortfall"], 0.01)

    def test_missing_balance_is_unverified(self):
        self.assertEqual(cash_check(None, 1, 0, self.now)["reason"], "missing_balance")

    def test_stale_and_future_balances_never_authorize(self):
        for age, reason in ((timedelta(hours=5), "stale_balance"), (timedelta(seconds=-1), "future_balance")):
            with self.subTest(reason=reason):
                self.snapshot["balance_as_of"] = self.now - age
                self.assertEqual(self.check(amount=1)["reason"], reason)
                self.assertEqual(self.check(amount=1)["status"], "unverified")

    def test_negative_balance_is_a_shortfall(self):
        self.snapshot.update(available_balance=-1, pending_outflows=0, minimum_buffer=0)
        self.assertEqual(self.check(amount=1, reserved=0)["shortfall"], 2)

    def test_nonfinite_invalid_and_negative_inputs_fail(self):
        for value in ("NaN", "Infinity", "-Infinity", "invalid"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                money(value)
        for kwargs in ({"amount": -1}, {"reserved": -1}, {"max_age_hours": 0}):
            args = dict(amount=1, reserved=0, max_age_hours=4)
            args.update(kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                cash_check(self.snapshot, now=self.now, **args)

    def test_fingerprint_covers_reviewed_values_and_is_order_independent(self):
        self.assertEqual(fingerprint(dict(a=1, b=2)), fingerprint(dict(b=2, a=1)))
        self.assertNotEqual(fingerprint(dict(a=1)), fingerprint(dict(a=2)))
