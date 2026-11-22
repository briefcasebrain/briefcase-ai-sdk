"""An offline stand-in for the AWS KMS client, for tests only.

Implements the two calls the SDK makes (``sign`` and ``verify`` with
``MessageType="RAW"``) for ``RSASSA_PSS_SHA_256`` and
``RSASSA_PKCS1_V1_5_SHA_256`` with local RSA keys, and raises
``exceptions.KMSInvalidSignatureException`` on a bad signature the way the
real client does. It never talks to AWS and needs no credentials.

``tests/fixtures/compliance/fake_kms_test_key.pub.pem`` is the PUBLIC half of
a throwaway RSA key that signed the format-1 fixtures once; the private half
was discarded, so the fixture key can only verify. Tests that sign create a
fresh key with :meth:`FakeKms.create_key`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

FIXTURE_PUBLIC_KEY = Path(__file__).resolve().parents[1] / "fixtures" / "compliance" / "fake_kms_test_key.pub.pem"
FIXTURE_KEY_ID = "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-4000-8000-000000000001"
SIGNING_KEY_ID = "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-4000-8000-000000000002"


class _Exceptions:
    class KMSInvalidSignatureException(Exception):
        pass

    class NotFoundException(Exception):
        pass


class FakeKms:
    exceptions = _Exceptions

    def __init__(self) -> None:
        self.keys: Dict[str, rsa.RSAPrivateKey] = {}
        self.public_keys: Dict[str, rsa.RSAPublicKey] = {}
        self.calls: list = []

    @classmethod
    def with_fixture_key(cls) -> "FakeKms":
        """Verify-only fixture key, plus a fresh signing key under SIGNING_KEY_ID."""
        kms = cls()
        public = serialization.load_pem_public_key(FIXTURE_PUBLIC_KEY.read_bytes())
        assert isinstance(public, rsa.RSAPublicKey)
        kms.public_keys[FIXTURE_KEY_ID] = public
        kms.create_key(SIGNING_KEY_ID)
        return kms

    def create_key(self, key_id: str) -> str:
        self.keys[key_id] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_keys[key_id] = self.keys[key_id].public_key()
        return key_id

    @staticmethod
    def _padding(algorithm: str):
        if algorithm == "RSASSA_PSS_SHA_256":
            return padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32)
        if algorithm == "RSASSA_PKCS1_V1_5_SHA_256":
            return padding.PKCS1v15()
        raise ValueError("fake KMS does not support %s" % algorithm)

    def _key(self, key_id: str) -> rsa.RSAPrivateKey:
        if key_id not in self.keys:
            raise self.exceptions.NotFoundException(key_id)
        return self.keys[key_id]

    def sign(self, *, KeyId, Message, MessageType, SigningAlgorithm):  # noqa: N803 (boto3 names)
        assert MessageType == "RAW"
        self.calls.append(("sign", KeyId))
        sig = self._key(KeyId).sign(Message, self._padding(SigningAlgorithm), hashes.SHA256())
        return {"KeyId": KeyId, "Signature": sig, "SigningAlgorithm": SigningAlgorithm}

    def verify(self, *, KeyId, Message, MessageType, Signature, SigningAlgorithm):  # noqa: N803
        assert MessageType == "RAW"
        self.calls.append(("verify", KeyId))
        if KeyId not in self.public_keys:
            raise self.exceptions.NotFoundException(KeyId)
        try:
            self.public_keys[KeyId].verify(
                Signature, Message, self._padding(SigningAlgorithm), hashes.SHA256()
            )
        except InvalidSignature:
            raise self.exceptions.KMSInvalidSignatureException("invalid signature") from None
        return {"KeyId": KeyId, "SignatureValid": True, "SigningAlgorithm": SigningAlgorithm}


class FakeSession:
    """``boto3.Session`` stand-in whose ``client("kms")`` is the fake."""

    def __init__(self, kms: FakeKms) -> None:
        self._kms = kms

    def client(self, name: str) -> FakeKms:
        assert name == "kms"
        return self._kms
