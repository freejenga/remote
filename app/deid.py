"""De-identification boundary for the AI chat.

Everything sent to the LLM provider passes through here first: direct
identifiers (names, DOB, addresses, contact info, government IDs) are replaced
with ``[REDACTED]`` while the codes and clinical facts the AI actually needs to
be useful (subject IDs, status, visit, amount, miles, dates) pass through. The
result: the model can reason fully about a subject's record without that
subject's identity ever leaving the machine.

Toggle with the ``CHAT_DEIDENTIFY`` env var (default on; set to ``0`` to send raw
data, e.g. when you have a provider BAA + zero-retention in place).
"""
import os
import re

REDACTED = "[REDACTED]"


def _norm(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


# Field names (normalized) treated as direct identifiers and fully redacted.
_IDENTIFIER_KEYS = {
    "name", "firstname", "lastname", "fullname", "patientname", "givenname",
    "surname", "middlename", "initials",
    "dob", "birthdate", "dateofbirth",
    "ssn", "ssa", "mrn", "medicalrecordnumber", "nationalid", "passport",
    "email", "emailaddress", "phone", "telephone", "mobile", "fax",
    "address", "street", "streetaddress", "addressline1", "addressline2",
    "city", "zip", "zipcode", "postalcode", "postcode",
    "pickup", "dropoff", "pickupaddress", "dropoffaddress", "origin",
    "destination", "homeaddress",
}

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


def enabled() -> bool:
    return os.environ.get("CHAT_DEIDENTIFY", "1") != "0"


def scrub_text(s):
    """Redact identifier-like patterns (SSN, email, phone) from free text."""
    if not isinstance(s, str) or not enabled():
        return s
    s = _SSN.sub("[REDACTED:ssn]", s)
    s = _EMAIL.sub("[REDACTED:email]", s)
    s = _PHONE.sub("[REDACTED:phone]", s)
    return s


def redact_obj(value):
    """Recursively redact identifier fields in dicts/lists and scrub strings."""
    if not enabled():
        return value
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and _norm(k) in _IDENTIFIER_KEYS:
                out[k] = REDACTED
            else:
                out[k] = redact_obj(v)
        return out
    if isinstance(value, list):
        return [redact_obj(v) for v in value]
    if isinstance(value, str):
        return scrub_text(value)
    return value
