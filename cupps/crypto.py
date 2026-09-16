"""Magnetic-stripe track encryption (TS 01.04.0004 section 30.3).

Track data read from an MS device is encrypted by the platform before it
crosses the interface and Base64-encoded for transport, because it may carry
cardholder data that the site needs to keep out of PCI DSS scope.  Section
30.9.2 is explicit that "the encryption algorithm uses the current session's
device token as the encryption key".

The specification defers the cipher construction to the CUPPS SDK sample
package, but Listing 30.30 pins it down exactly: it publishes a device token
of ``K39FM6AK2P10MG4K`` alongside the plaintext of four tracks and their
ciphertexts.  Those vectors identify the construction as:

* ``aes-strong`` -- AES-128 in ECB mode, PKCS#7 padding, key = the 16-byte
  device token.
* ``des-weak``   -- single DES in ECB mode, PKCS#7 padding, key = the first
  8 bytes of the device token (the truncation is called out in the note to
  section 30.3).

The Listing 30.30 vectors are asserted in ``tests/test_crypto.py``, so a
platform that differs will be caught by the conformance run rather than in
production.

ECB is a weak construction and is not this implementation's choice: it is what
the published vectors require for interoperability.  Applications should treat
decrypted track data as cardholder data and keep it out of logs (section
6.3.12 warning).
"""

from __future__ import annotations

import base64
from typing import Callable, Optional

from .results import CryptAlgorithm

_AES_BLOCK = 16
_DES_BLOCK = 8


class CryptoUnavailable(RuntimeError):
    """No supported cryptography backend is installed."""


def _load_backend() -> tuple[Callable[..., object], str]:
    """Return a ``(factory, flavour)`` pair for whichever backend is present.

    Airport images vary in which crypto wheel is available, so both the
    ``cryptography`` and ``pycryptodome`` packages are accepted.
    """
    try:
        from Crypto.Cipher import AES, DES  # type: ignore

        def factory(algorithm: str, key: bytes):
            if algorithm == "aes":
                return AES.new(key, AES.MODE_ECB)
            return DES.new(key, DES.MODE_ECB)

        return factory, "pycryptodome"
    except ImportError:
        pass

    try:
        from cryptography.hazmat.primitives.ciphers import (  # type: ignore
            Cipher,
            algorithms,
            modes,
        )

        def factory(algorithm: str, key: bytes):
            if algorithm == "aes":
                return Cipher(algorithms.AES(key), modes.ECB())
            return Cipher(algorithms.TripleDES(key), modes.ECB())

        return factory, "cryptography"
    except ImportError as exc:
        raise CryptoUnavailable(
            "MS track decryption needs either 'pycryptodome' or "
            "'cryptography'; install one of them"
        ) from exc


def _pkcs7_pad(data: bytes, block: int) -> bytes:
    padding = block - (len(data) % block)
    return data + bytes([padding]) * padding


def _pkcs7_unpad(data: bytes, block: int) -> bytes:
    if not data or len(data) % block:
        raise ValueError(f"ciphertext is not a multiple of {block} bytes")
    padding = data[-1]
    if padding < 1 or padding > block or data[-padding:] != bytes([padding]) * padding:
        # A wrong device token produces garbage that rarely carries valid
        # padding, so this is the usual symptom of a stale session key.
        raise ValueError("invalid PKCS#7 padding; wrong device token?")
    return data[:-padding]


def _key_for(algorithm: str, device_token: str) -> tuple[str, bytes, int]:
    token = device_token.encode("ascii")
    if algorithm == CryptAlgorithm.AES_STRONG.value:
        if len(token) not in (16, 24, 32):
            raise ValueError(
                f"aes-strong needs a 16, 24 or 32 byte device token, "
                f"got {len(token)}"
            )
        return "aes", token, _AES_BLOCK
    if algorithm == CryptAlgorithm.DES_WEAK.value:
        # Section 30.3 note: "the DES encryption only uses the first 8 bytes
        # of the device token".
        if len(token) < 8:
            raise ValueError("des-weak needs at least an 8 byte device token")
        return "des", token[:8], _DES_BLOCK
    raise ValueError(f"unsupported crypt algorithm {algorithm!r}")


def _des_key_for_backend(key: bytes, flavour: str) -> bytes:
    """Widen a single-DES key when the backend only exposes Triple DES.

    ``cryptography`` removed the single-DES primitive, but 3DES with all three
    sub-keys equal is arithmetically identical to single DES, so K1=K2=K3
    reproduces the same ciphertext.
    """
    if flavour == "cryptography":
        return key * 3
    return key


def decrypt_track(
    ciphertext_b64: str,
    device_token: str,
    algorithm: str = CryptAlgorithm.AES_STRONG.value,
) -> bytes:
    """Decrypt one ``<blockData>`` payload into raw track bytes.

    Section 26.10 allows whitespace and newlines inside Base64 data, so the
    input is stripped before decoding.
    """
    factory, flavour = _load_backend()
    name, key, block = _key_for(algorithm, device_token)
    if name == "des":
        key = _des_key_for_backend(key, flavour)

    raw = base64.b64decode("".join(ciphertext_b64.split()))
    cipher = factory(name, key)
    if flavour == "pycryptodome":
        plaintext = cipher.decrypt(raw)  # type: ignore[attr-defined]
    else:
        decryptor = cipher.decryptor()  # type: ignore[attr-defined]
        plaintext = decryptor.update(raw) + decryptor.finalize()
    return _pkcs7_unpad(plaintext, block)


def encrypt_track(
    plaintext: bytes,
    device_token: str,
    algorithm: str = CryptAlgorithm.AES_STRONG.value,
) -> str:
    """Encrypt raw track bytes and Base64-encode them.

    Applications do not normally need this -- the platform is the encrypting
    side -- but the platform simulator and the conformance tests do.
    """
    factory, flavour = _load_backend()
    name, key, block = _key_for(algorithm, device_token)
    if name == "des":
        key = _des_key_for_backend(key, flavour)

    padded = _pkcs7_pad(plaintext, block)
    cipher = factory(name, key)
    if flavour == "pycryptodome":
        ciphertext = cipher.encrypt(padded)  # type: ignore[attr-defined]
    else:
        encryptor = cipher.encryptor()  # type: ignore[attr-defined]
        ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(ciphertext).decode("ascii")


def backend_name() -> Optional[str]:
    """Which crypto backend is in use, or ``None`` if none is installed."""
    try:
        return _load_backend()[1]
    except CryptoUnavailable:
        return None
