"""Optional signing-key implementations for provisioning identities."""

from __future__ import annotations

import base64

from .core import KeyMaterial, OpaqueTokenKeyGenerator


def _urlsafe(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class CryptographyEd25519KeyGenerator:
    """Generate Ed25519 keys through the optional ``cryptography`` package."""

    @staticmethod
    def is_available() -> bool:
        try:
            from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: F401
        except ImportError:
            return False
        return True

    def generate(self) -> KeyMaterial:
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey,
            )
        except ImportError as exc:
            raise RuntimeError(
                "CryptographyEd25519KeyGenerator requires the cryptography package"
            ) from exc

        private_key = Ed25519PrivateKey.generate()
        private_raw = private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_raw = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return KeyMaterial(
            algorithm="ed25519",
            public_identity=_urlsafe(public_raw),
            private_identity=_urlsafe(private_raw),
        )


def preferred_key_generator() -> CryptographyEd25519KeyGenerator | OpaqueTokenKeyGenerator:
    """Prefer signing keys, retaining a secure stdlib-only fallback."""

    if CryptographyEd25519KeyGenerator.is_available():
        return CryptographyEd25519KeyGenerator()
    return OpaqueTokenKeyGenerator()
