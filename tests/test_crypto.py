"""Magnetic stripe encryption, against the vectors published in Listing 30.30.

Listing 30.30 publishes a device token of ``K39FM6AK2P10MG4K`` together with
the plaintext of four tracks and the Base64 ciphertext for each.  Those
vectors fix the construction the platform uses, so they are asserted here:
a platform that differs will fail this test rather than silently returning
garbage track data in production.
"""

from __future__ import annotations

import pytest

from cupps import decrypt_track, encrypt_track
from cupps.crypto import CryptAlgorithm

DEVICE_TOKEN = "K39FM6AK2P10MG4K"

#: (plaintext, ciphertext) from the comments and body of Listing 30.30.
LISTING_30_30_VECTORS = [
    (b"John Smith\x00\x01", "93pU3Wiop2Eg/PRkeLzknQ=="),
    (b"1234 Elm Street", "2Pv6GOyupfVxNcneZ5gHsA=="),
    (b"Palm Springs, Florida", "S12q8hTpo7Wc48nNdDUHDXIv7S5bR2eziu0A2DHUhb4="),
    (b"USA", "Xxor+RKJmx1sOWIYDgX5XA=="),
]


@pytest.mark.parametrize("plaintext,ciphertext", LISTING_30_30_VECTORS)
def test_decrypts_listing_30_30(plaintext, ciphertext):
    assert decrypt_track(ciphertext, DEVICE_TOKEN) == plaintext


@pytest.mark.parametrize("plaintext,ciphertext", LISTING_30_30_VECTORS)
def test_encrypts_listing_30_30(plaintext, ciphertext):
    assert encrypt_track(plaintext, DEVICE_TOKEN) == ciphertext


def test_multi_block_vector_discriminates_the_cipher_mode():
    """The two-block vector is what rules out CBC with a zero IV.

    Single-block ciphertexts are identical under ECB and zero-IV CBC, so the
    21-byte "Palm Springs, Florida" track is the one that identifies the mode.
    """
    plaintext, ciphertext = LISTING_30_30_VECTORS[2]
    assert len(plaintext) > 16
    assert encrypt_track(plaintext, DEVICE_TOKEN) == ciphertext


def test_base64_may_contain_whitespace():
    """Section 26.10 allows spaces and newlines inside Base64 data."""
    _, ciphertext = LISTING_30_30_VECTORS[0]
    spaced = ciphertext[:8] + "\n  " + ciphertext[8:]
    assert decrypt_track(spaced, DEVICE_TOKEN) == LISTING_30_30_VECTORS[0][0]


def test_des_weak_uses_the_first_eight_bytes_of_the_token():
    """Section 30.3 note: "DES encryption only uses the first 8 bytes"."""
    algorithm = CryptAlgorithm.DES_WEAK.value
    payload = b"track data"
    full = encrypt_track(payload, DEVICE_TOKEN, algorithm)
    truncated_key_token = DEVICE_TOKEN[:8] + "XXXXXXXX"
    assert encrypt_track(payload, truncated_key_token, algorithm) == full
    assert decrypt_track(full, DEVICE_TOKEN, algorithm) == payload


def test_wrong_token_is_reported_rather_than_returning_garbage():
    _, ciphertext = LISTING_30_30_VECTORS[2]
    with pytest.raises(ValueError, match="padding"):
        decrypt_track(ciphertext, "WRONGTOKEN123456")
