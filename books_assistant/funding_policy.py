"""Deterministic cash checks. No ledger balance is treated as a bank balance."""

from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json

CENT = Decimal("0.01")


def money(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError("Amount must be finite")
        return result.quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Invalid amount") from exc


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def cash_check(snapshot, amount, reserved, now, max_age_hours=4):
    amount, reserved = money(amount), money(reserved)
    if amount < 0 or reserved < 0 or max_age_hours <= 0:
        raise ValueError("Invalid funding inputs")
    result = {"amount": float(amount), "reserved": float(reserved), "status": "unverified", "reason": "missing_balance"}
    if not snapshot:
        return result
    balance = money(snapshot["available_balance"])
    pending, buffer = money(snapshot["pending_outflows"]), money(snapshot["minimum_buffer"])
    if pending < 0 or buffer < 0:
        raise ValueError("Commitments and buffer cannot be negative")
    available = balance - pending - reserved - buffer
    result.update(
        available_balance=float(balance), pending_outflows=float(pending), minimum_buffer=float(buffer),
        available_for_payout=float(available), after_payout=float(available - amount),
        shortfall=float(max(amount - available, Decimal(0))), balance_as_of=str(snapshot["balance_as_of"]),
        snapshot=snapshot["name"],
    )
    age = now - snapshot["balance_as_of"]
    if age < timedelta(0) or age > timedelta(hours=max_age_hours):
        result["reason"] = "stale_balance" if age >= timedelta(0) else "future_balance"
    elif amount > available:
        result.update(status="blocked", reason="cash_shortfall")
    else:
        result.update(status="sufficient", reason=None)
    return result
