"""Generate WireGuard keys for new clients locally. Keep them only in memory (QR/export), never in DB or logs.

Per wg(8), private, public, and preshared keys are 32 bytes each, Base64 encoded. The public key
is the Curve25519 (X25519, RFC 7748) form of the private key; `wg genpsk` yields 32 random bytes.
Use X25519 from `cryptography` (docs: hazmat/primitives/asymmetric/x25519) and randomness from `secrets`.
"""

from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def public_key(private_b64: str) -> str:
    raw = base64.b64decode(private_b64, validate=True)
    if len(raw) != 32:
        raise ValueError("Private Key muss 32 Byte lang sein")
    return _b64(X25519PrivateKey.from_private_bytes(raw).public_key().public_bytes_raw())


def generate_psk() -> str:
    return _b64(secrets.token_bytes(32))


@dataclass(frozen=True)
class KeyPair:
    private: str = field(repr=False)  # Never expose in repr or logs
    public: str


def generate_keypair() -> KeyPair:
    key = X25519PrivateKey.generate()
    return KeyPair(_b64(key.private_bytes_raw()), _b64(key.public_key().public_bytes_raw()))
