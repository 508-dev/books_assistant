"""Rule-first, CPU-local and optional hosted category suggestions; never accounting writes."""

from uuid import uuid4

import frappe
from frappe.utils import flt, today

from books_assistant import bank_learning as learning
from books_assistant.integration import fingerprint, resolve_scope
from books_assistant.bank_classifier import merchant_key
from books_assistant.matching import vendor_key

APPROVERS = ("Accounts Manager", "System Manager")
HISTORY_DAYS = 730
MAX_HISTORY = 2000
# Redis atomically reserves failed attempts too. This is an operational limiter,
# not a billing guarantee across cache eviction or resets.
RESERVE = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], 172800) end
return n
"""


def _settings():
    return frappe.get_single("Automation Settings")


def _history(scope, bank_account=None, direction=None):
    return learning.load_observations(scope, history_days=HISTORY_DAYS, for_update=False, max_rows=MAX_HISTORY + 1,
                                      bank_account=bank_account, direction=direction)


def _reserve_daily(scope, limit):
    try:
        cache = frappe.cache()
        key = cache.make_key(f"bank-classifier-calls:{scope['company']}:{today()}")
        return int(cache.eval(RESERVE, 1, key)) <= limit
    except Exception:
        return False  # Meter failure must never turn into unlimited provider calls.


def _categories(scope, observations):
    observed = {r["account"] for r in observations if not r.get("ineligible") and not r.get("generated") and r.get("account")}
    rows = frappe.get_list("Account", filters={"company": scope["company"], "account_currency": scope["currency"],
                           "root_type": ["in", ["Income", "Expense"]], "is_group": 0, "disabled": 0},
                           fields=["name", "account_name", "root_type", "account_type"], limit_page_length=0)
    return [{"account": r.name, "name": r.account_name, "description": r.root_type}
            for r in rows if r.name in observed and learning.eligible_ledger(r)]


def _finish(result, transaction, scope, run_id, source, dataset_hash=None):
    result = {**result, "contract_version": 1, "action_id": "bank.classify", "run_id": run_id,
              "runtime_owner": "erpnext", "source": source, "company": scope["company"],
              "currency": scope["currency"], "bank_transaction": transaction["name"],
              "input_fingerprint": fingerprint(transaction), "execution": "review_required",
              "description": transaction["description"], "keyword": vendor_key(transaction["description"]),
              "bank_account": transaction["bank_account"]}
    if dataset_hash:
        result["dataset_hash"] = dataset_hash
    # Only bounded machine metadata reaches logs, never bank text or provider bodies.
    frappe.logger("books_assistant.bank_classifier").info({key: result.get(key) for key in (
        "action_id", "contract_version", "run_id", "runtime_owner", "source", "reason_code", "model_version", "model_id", "dataset_hash")})
    return result


def transaction_snapshot(bt, scope):
    amount = flt(bt.deposit) or flt(bt.withdrawal)
    return {"name": bt.name, "description": bt.description or "", "bank_account": bt.bank_account,
                   "company": scope["company"], "currency": scope["currency"], "amount": amount,
                   "direction": "Money in" if flt(bt.deposit) > 0 else "Money out", "date": str(bt.date),
                   "modified": str(bt.modified), "docstatus": bt.docstatus,
                   "unallocated_amount": flt(bt.unallocated_amount)}


@frappe.whitelist(methods=["POST"])
def suggest(bank_transaction, company=None):
    """Suggest a category for one visible bank line. Hosted calls require explicit opt-in."""
    from books_assistant import bank_review as br
    from books_assistant.bank_api import _check_line
    from books_assistant.bank_automation import _existing_candidate
    from books_assistant.bank_classifier import Classifier, provider_context

    frappe.only_for(APPROVERS)
    scope = resolve_scope(company)
    bt = _check_line(bank_transaction, scope)
    amount = flt(bt.deposit) or flt(bt.withdrawal)
    transaction = transaction_snapshot(bt, scope)
    run_id = str(uuid4())
    def finish(result, source, data=None):
        return _finish(result, transaction, scope, run_id, source, data)
    if (bt.docstatus != 1 or amount <= 0 or bool(flt(bt.deposit)) == bool(flt(bt.withdrawal))
            or abs(flt(bt.unallocated_amount) - amount) >= 0.005 or str(bt.date) > today()):
        return finish({"status": "abstain", "reason_code": "INELIGIBLE_BANK_LINE"}, "validation")
    context = br.Context(scope["company"])
    deterministic = context.suggest(frappe._dict(bt.as_dict()))
    if deterministic["kind"] != "none":
        return finish({"status": "existing_action", "reason_code": "DETERMINISTIC_ACTION",
                       "action_kind": deterministic["kind"], "account": deterministic.get("account")}, "rules")
    settings = _settings()
    if not (settings.get("bank_local_classifier_enabled") or settings.get("bank_cloudflare_classifier_enabled")):
        return finish({"status": "abstain", "reason_code": "DISABLED"}, "configuration")
    blocker = _existing_candidate(bt, context.bank_gl.get(bt.bank_account), scope, for_update=False)
    if blocker:
        return finish({"status": "abstain", "reason_code": blocker}, "validation")
    observations = _history(scope, bt.bank_account, transaction["direction"])
    if len(observations) > MAX_HISTORY:
        return finish({"status": "abstain", "reason_code": "HISTORY_CAPACITY_EXCEEDED"}, "history")
    relevant = [r for r in observations if all(r.get(k) == transaction[k] for k in ("company", "currency", "bank_account", "direction"))]
    merchant = merchant_key(transaction["description"])
    same = [r for r in relevant if merchant_key(r["description"]) == merchant]
    if (merchant in learning.EXCLUDED_KEYWORDS or any(r.get("ineligible") for r in same)
            or len({r.get("account") for r in same if not r.get("generated")}) > 1):
        return finish({"status": "abstain", "reason_code": "AMBIGUOUS_HISTORY"}, "history")
    categories = _categories(scope, relevant)
    allowed = {c["account"] for c in categories}
    model = Classifier(observations)
    local = model.predict(transaction)
    if settings.get("bank_local_classifier_enabled") and local["status"] == "suggested":
        if local.get("account") in allowed:
            return finish(local, "local", model.dataset_hash)
        local = {"status": "abstain", "reason_code": "INVALID_CATEGORY"}
    if not settings.get("bank_cloudflare_classifier_enabled"):
        return finish(local if settings.get("bank_local_classifier_enabled") else
                      {"status": "abstain", "reason_code": "LOCAL_DISABLED"}, "local", model.dataset_hash)
    if not 1 <= len(categories) <= 32:
        return finish({"status": "abstain", "reason_code": "INSUFFICIENT_OR_EXCESS_CATEGORIES"}, "history", model.dataset_hash)
    from books_assistant.cloudflare_decision import CloudflareDecisionClient

    limit = int(settings.get("bank_classifier_daily_calls") or 100)
    if not 1 <= limit <= 1000 or not _reserve_daily(scope, limit):
        return finish({"status": "abstain", "reason_code": "PROVIDER_BUDGET_UNAVAILABLE"}, "cloudflare", model.dataset_hash)
    categories, examples = provider_context(transaction, relevant, categories)
    hosted = CloudflareDecisionClient().classify(transaction, categories, examples)
    if hosted.get("status") == "suggested" and hosted.get("account") not in allowed:
        hosted = {"status": "abstain", "reason_code": "INVALID_CATEGORY"}
    return finish(hosted, "cloudflare", model.dataset_hash)


@frappe.whitelist(methods=["POST"])
def benchmark(company=None):
    """Replay visible historical classifications locally; this never calls a provider."""
    from books_assistant.bank_classifier_eval import benchmark as evaluate

    frappe.only_for(APPROVERS)
    scope = resolve_scope(company)
    observations = _history(scope)
    if len(observations) > MAX_HISTORY:
        return {"status": "insufficient_data", "reason_code": "HISTORY_CAPACITY_EXCEEDED", "contract_version": 1}
    return {"contract_version": 1, "source": "visible_erp_history", "company": scope["company"],
            "history_days": HISTORY_DAYS, **evaluate(observations, source_label="visible_erp_history")}
