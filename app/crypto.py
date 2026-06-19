"""Field-level encryption at rest for the platform's sensitive data.

Sensitive JSON blobs (subjects, visit logs, trips, chat memory) are encrypted
with Fernet (AES-128-CBC + HMAC) before they touch the SQLite file, so a copied
``.db`` is unreadable without the key. The key is derived from the
``PLATFORM_ENCRYPTION_KEY`` env var (any passphrase -> SHA-256 -> Fernet key).

Disabled by default: with no key set, ``encrypt`` is a pass-through and the data
is stored as plain JSON (so local dev and the test suite work unchanged).
Encrypted values are tagged with an ``enc:`` prefix so ``decrypt`` can handle a
database that mixes plain and encrypted rows during migration.
"""
import base64
import hashlib
import os

_PREFIX = "enc:"


def enabled() -> bool:
    return bool(os.environ.get("PLATFORM_ENCRYPTION_KEY"))


def _fernet():
    key = os.environ.get("PLATFORM_ENCRYPTION_KEY")
    if not key:
        return None
    from cryptography.fernet import Fernet
    digest = hashlib.sha256(key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(text: str) -> str:
    f = _fernet()
    if f is None:
        return text
    return _PREFIX + f.encrypt(text.encode()).decode()


def decrypt(text: str) -> str:
    if not isinstance(text, str) or not text.startswith(_PREFIX):
        return text  # plain (unencrypted) value
    f = _fernet()
    if f is None:
        raise RuntimeError(
            "Encrypted data found but PLATFORM_ENCRYPTION_KEY is not set.")
    return f.decrypt(text[len(_PREFIX):].encode()).decode()
