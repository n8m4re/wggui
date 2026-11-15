"""Synthetic keys: Base64 of 32 repeated bytes; clearly fake but valid in form."""

import base64


def fake_key(n: int) -> str:
    return base64.b64encode(bytes([n]) * 32).decode()


# Used in tests/fixtures
PRIVATE = fake_key(0xAA)  # server private key; must never appear in results
PSK = fake_key(0xBB)  # preshared key; must never appear in results
PUB0 = fake_key(0x10)  # interface public key
K1, K2, K3, K4, K9 = (fake_key(n) for n in (1, 2, 3, 4, 9))
SECRETS = (PRIVATE, PSK)
