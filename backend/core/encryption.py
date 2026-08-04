# encryption.py — Fernet encryption/decryption for API keys at rest

from __future__ import annotations

import base64
import sys
from pathlib import Path

# Allow `python backend/core/encryption.py` from trading-bot/ root
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from cryptography.fernet import Fernet, InvalidToken

from backend.config import APP_SECRET_KEY


class EncryptionError(Exception):
    """Raised when encryption fails."""


class DecryptionError(Exception):
    """Raised when decryption fails (wrong key, corrupted data, etc.)."""


def _derive_fernet_key(secret: str) -> bytes:
    """
    Derive a Fernet-compatible key from APP_SECRET_KEY.

    Fernet requires a 32-byte url-safe base64-encoded key.
    If secret is not exactly 32 bytes, pad with nulls or truncate.
    """
    key_bytes = secret.encode("utf-8")
    key_bytes = key_bytes[:32].ljust(32, b"\0")
    return base64.urlsafe_b64encode(key_bytes)


def get_fernet() -> Fernet:
    """Return a Fernet instance using APP_SECRET_KEY. Raises ValueError if key missing."""
    if not APP_SECRET_KEY:
        raise ValueError("APP_SECRET_KEY is missing — set it in .env")
    return Fernet(_derive_fernet_key(APP_SECRET_KEY))


def encrypt(plain_text: str) -> str:
    """Encrypt plain_text and return a base64 Fernet token string."""
    try:
        fernet = get_fernet()
        token = fernet.encrypt(plain_text.encode("utf-8"))
        return token.decode("utf-8")
    except ValueError:
        raise
    except Exception as exc:
        raise EncryptionError(f"Failed to encrypt data: {exc}") from exc


def decrypt(cipher_text: str) -> str:
    """Decrypt a Fernet token string and return the plain text."""
    try:
        fernet = get_fernet()
        plain = fernet.decrypt(cipher_text.encode("utf-8"))
        return plain.decode("utf-8")
    except ValueError:
        raise
    except InvalidToken as exc:
        raise DecryptionError("Failed to decrypt data: invalid token or wrong key") from exc
    except Exception as exc:
        raise DecryptionError(f"Failed to decrypt data: {exc}") from exc


if __name__ == "__main__":
    test_key = "my-fake-api-key-12345"
    encrypted = encrypt(test_key)
    print(f"Encrypted: {encrypted[:30]}...")
    decrypted = decrypt(encrypted)
    print(f"Decrypted: {decrypted}")
    assert decrypted == test_key, "Decryption mismatch!"

    # Test wrong input raises error
    try:
        decrypt("invalid-cipher-text")
        assert False, "Should have raised error"
    except DecryptionError:
        pass

    print("✅ ENCRYPTION TEST PASSED")
