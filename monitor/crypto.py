"""Optional password protection for the published dashboard data.

When DASHBOARD_PASSWORD is set, the dashboard JSON is encrypted with AES-256-GCM using a
key derived by PBKDF2-SHA256. The browser decrypts it with the Web Crypto API once the
viewer enters the password, so the data is unreadable without it, even on a public host.
This complements, and does not replace, access control at the hosting layer (see README).
"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ITERATIONS = 600_000


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def derive_key(password: str, salt: bytes, iterations: int = ITERATIONS) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return kdf.derive(password.encode("utf-8"))


def encrypt(plaintext: bytes, password: str) -> dict:
    salt, iv = os.urandom(16), os.urandom(12)
    ct = AESGCM(derive_key(password, salt)).encrypt(iv, plaintext, None)
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iterations": ITERATIONS, "salt": _b64(salt), "iv": _b64(iv), "ciphertext": _b64(ct)}


def decrypt(envelope: dict, password: str) -> bytes:
    salt = base64.b64decode(envelope["salt"])
    key = derive_key(password, salt, envelope["iterations"])
    return AESGCM(key).decrypt(base64.b64decode(envelope["iv"]), base64.b64decode(envelope["ciphertext"]), None)
