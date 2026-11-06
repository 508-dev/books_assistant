"""Reading bank descriptions: vendor keywords, invoice numbers, and the names parties appear under."""

import re

# Card and ACH prefixes that come before the merchant in bank descriptions.
_PREFIXES = re.compile(
    r"^(?:(?:POS\s+)?(?:DEBIT|CREDIT)(?:_CARD)?|MISC_DEBIT|MISC_CREDIT|ACH_DEBIT|ACH_CREDIT|QUICKPAY_CREDIT|"
    r"QUICKPAY_DEBIT|PARTNERFI_TO_CHASE|FEE_TRANSACTION|WIRE_OUTGOING|WIRE_INCOMING|WIRE IN|WIRE OUT|POS|"
    r"SQ \*|TST\*)\s+|^RCH-"
)
# First words that say nothing about who was paid; the keyword then uses the first two words.
_GENERIC = {"MONTHLY", "ONLINE", "STANDARD", "CHASE", "FOREIGN", "SERVICE", "PAYMENT", "TRANSFER", "FEE",
            "INTEREST", "THE", "DOMESTIC", "INCOMING", "OUTGOING", "ACH", "WIRE", "CARD"}
_DOMAINS = {"COM", "IO", "AI", "NET", "ORG", "CO", "APP", "DEV"}
_ORIG_CO = re.compile(r"ORIG CO NAME:\s*(.+?)\s+(?:ORIG ID|DESC DATE|CO ENTRY)", re.I)
_PAID_TO = re.compile(r"^ONLINE (?:ACH PAYMENT|TRANSFER)\b.*?\sTO\s+(.+)$")
_INVOICE = {
    "Sales Invoice": re.compile(r"(?:ACC[-\s]?)?SINV[-\s]?(\d{4})[-\s]?(\d{5})(?:-(\d+))?", re.I),
    "Purchase Invoice": re.compile(r"(?:ACC[-\s]?)?PINV[-\s]?(\d{4})[-\s]?(\d{5})(?:-(\d+))?", re.I),
}
# Words that describe how money moved, not who moved it.
_NOT_NAMES = {"BASIC", "ONLINE", "PAYROLL", "PAYMENT", "PAYMENTS", "CAPITAL", "ONE", "BILLS", "BILL", "INVOICE",
              "INVOICES", "WIRE", "IN", "US", "INC", "INC.", "WISE", "RAMP", "REIMBURSE", "INTUIT", "STRIPE", "PAYPAL",
              "VENMO", "MERCURY", "DEPOSIT", "TRANSFER", "LLC", "REVENUE", "ACCT_XFER", "XFER", "REAL", "TIME", "RECD",
              "FEDWIRE", "CREDIT", "DEBIT", "MISC_CREDIT", "MISC_DEBIT", "WIRE_INCOMING", "WIRE_OUTGOING",
              "INTERNATIONAL", "DOMESTIC", "ACH", "MONTHLY", "CHASE", "FROM", "TO", "AND"}


def searchable(description):
    """The part of a description that identifies the merchant, upper-cased with punctuation spaced out.

    'MISC_DEBIT ORIG CO NAME:Wise Inc ORIG ID:9453...' -> 'WISE INC'; 'Interest (Received)' -> 'INTEREST RECEIVED'.
    Rules are matched against this, so prefixes like MISC_DEBIT can't trigger a rule by accident.
    """
    text = (description or "").upper().strip()
    orig = _ORIG_CO.search(text)
    if orig:
        text = orig.group(1)
    text = _PREFIXES.sub("", text)
    paid_to = _PAID_TO.search(text)
    if paid_to:
        text = paid_to.group(1)
    text = re.sub(r"^WWW\.", "", text)
    return " ".join(re.sub(r"[^\w.&'-]+|_", " ", text).split())


def vendor_key(description):
    """A short keyword identifying the merchant, e.g. 'GITHUB, INC. GITHUB.COM CA' and 'GITHUB' -> 'GITHUB'."""
    tokens = [t.strip(".-'") for t in re.findall(r"[A-Z0-9][A-Z0-9.&'-]*", searchable(description))]
    tokens = [t for t in tokens if t and not t.isdigit()]
    if not tokens:
        return None
    first = tokens[0]
    parts = first.split(".")
    if len(parts) > 1 and parts[-1] in _DOMAINS:
        first = ".".join(parts[:-1])
    if first in _GENERIC and len(tokens) > 1:
        first = f"{first} {tokens[1]}"
    return first if len(first) >= 3 else None


def keyword_in(keyword, description):
    """Whether a rule keyword appears as whole words in the searchable part of a description."""
    words = " ".join(re.sub(r"[^\w.&'-]+|_", " ", (keyword or "").upper()).split())
    return bool(words) and re.search(rf"(?<![\w.]){re.escape(words)}(?![\w])", searchable(description)) is not None


def invoice_refs(description, doctype="Sales Invoice"):
    """Invoice numbers mentioned in a description, normalised to ERPNext names, in order and de-duplicated."""
    names = []
    for year, number, amendment in _INVOICE[doctype].findall(description or ""):
        prefix = "ACC-SINV" if doctype == "Sales Invoice" else "ACC-PINV"
        name = f"{prefix}-{year}-{number}" + (f"-{amendment}" if amendment else "")
        if name not in names:
            names.append(name)
    return names


def bank_name(description):
    """The payer or payee name in a description, e.g. 'Zelle payment from JANE Q DOE 2695...' -> 'JANE Q DOE'.

    Returns None for card purchases and for descriptions that only name a payment service.
    """
    text = description or ""
    if re.match(r"\s*(?:POS\s+)?(?:DEBIT|CREDIT)[_ ]CARD|\s*POS\s", text, re.I):
        return None
    orig = _ORIG_CO.search(text)
    if orig:
        text = orig.group(1)
    text = re.sub(r"\((?:Sent|Received)\)|\(\)", " ", text, flags=re.I)
    text = re.sub(r"^\s*(?:QUICKPAY_CREDIT|QUICKPAY_DEBIT|PARTNERFI_TO_CHASE|WIRE IN|WIRE OUT)\s*", "", text, flags=re.I)
    text = re.sub(r"^\s*Zelle payment (?:from|to)\s*", "", text, flags=re.I)
    # Stop at invoice numbers, references and memo words that follow the name.
    if re.search(r"\b\d{2}/\d{2}\b", text):
        return None  # card-statement style (merchant, state, date), not a person or company paying us
    text = re.split(
        r"\s-\s|ACC-?[SP]INV|(?:^|\s)(?:508(?:\.dev)?|TRANSFER-|INV-|Invoice|Contract|via|Repayment|Referral|for|"
        r"from|MercuryACH|BILL\.COM|Revenue|ID NBR|ACCTVERIFY)\b|\s(?:USA|WFCT|TDP)[A-Z0-9]{6,}|\s\d+\b",
        text, flags=re.I,
    )[0]
    words = [w for w in text.split() if not re.fullmatch(r"(?=[\w-]*\d)(?=[\w-]*[A-Za-z])[\w-]{6,}", w)][:4]
    text = " ".join(words).strip(" -.,")
    if not (3 <= len(text) <= 60) or not re.search(r"[^\W\d_]{3}", text):
        return None
    if all(w.upper().strip(".,") in _NOT_NAMES for w in text.split()):
        return None
    return text


def _words(text):
    return set(re.findall(r"[^\W_]+", (text or "").casefold()))


def mentions(description, name):
    """Whether a party name appears in a description.

    Multi-word names match when every word appears; single words must be at least six letters, so short
    common words don't match by accident. Names in other scripts are matched as a substring.
    """
    name = (name or "").strip()
    if len(name) < 3:
        return False
    if not re.search(r"[A-Za-z]", name):
        return name in (description or "")
    words = _words(name)
    if len(words) == 1 and len(next(iter(words))) < 6:
        return False
    return words <= _words(description)
