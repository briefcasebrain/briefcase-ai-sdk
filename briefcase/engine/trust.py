"""The engine's trust root: pinned release keys, blob signatures and the signed manifest.

Engine releases are signed with one asymmetric KMS key (ECDSA P-256) through
``cosign sign-blob --key awskms:///... --tlog-upload=false``. Nothing goes to a public
transparency log, so what makes a release trustworthy is exactly one thing: the signature
verifies against a public key that ships inside this SDK, in ``briefcase/engine/keys/``.

* A **key** is a PEM ``SubjectPublicKeyInfo`` file named ``release-*.pub`` in that directory.
  Its id is ``"sha256:" + hex(sha256(DER SubjectPublicKeyInfo))``, computed here from the
  file's contents (the file name is only a label).
* A **signature** is what ``cosign sign-blob --output-signature`` writes: base64 of an
  ASN.1 DER ECDSA signature over the SHA-256 digest of the file. It is checked in pure
  Python with ``cryptography`` (the ``engine`` extra); only when ``cryptography`` cannot be
  imported does this module fall back to ``cosign verify-blob --key``.
* The **manifest** (``engine-manifest.json``, schema 2) is signed as a whole and names the
  key that signed it (``key_id``), the release's files with their sha256, size and signature
  file, and every image by digest. A file is trusted when its sha256 and size match the
  verified manifest; its own ``.sig`` is checked too whenever it is available.

There is deliberately no switch, flag or environment variable that skips verification or
adds a key at run time: a release key is added by shipping it in an SDK release. Tests
point :data:`KEYS_DIR` at a temporary directory from Python (see ``tests/cli/engine_fakes.py``).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

SCHEMA_VERSION = 2
KEYS_DIR = Path(__file__).resolve().parent / "keys"
KEY_GLOB = "release-*.pub"
MAX_MANIFEST_BYTES = 1 << 20
MAX_SIGNATURE_BYTES = 16 << 10

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_KEY_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_REPOSITORY = re.compile(r"^[a-z0-9]+(?:[._/-][a-z0-9]+)*$")
_IMAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

NO_KEY_MESSAGE = ("no trusted release key is bundled in this SDK build; the engine cannot be "
                  "installed until a release key is published")
NO_KEY_FIX = "pip install -U 'briefcase-ai[engine]'   # once a release with the engine key is out"


class TrustError(RuntimeError):
    """A verification failure: what was wrong plus the one thing to do about it."""

    def __init__(self, message: str, fix: Optional[str] = None) -> None:
        super().__init__(message)
        self.fix = fix


# --------------------------------------------------------------------------- keys


@dataclass(frozen=True)
class ReleaseKey:
    key_id: str
    path: Path
    pem: bytes
    der: bytes

    @property
    def label(self) -> str:
        return self.path.name


def pem_to_der(pem: bytes) -> bytes:
    """The DER body of a ``-----BEGIN PUBLIC KEY-----`` PEM (stdlib only)."""
    text = pem.decode("ascii", "replace")
    match = re.search(r"-----BEGIN PUBLIC KEY-----(.+?)-----END PUBLIC KEY-----", text, re.S)
    if not match:
        raise TrustError("not a PEM public key (expected -----BEGIN PUBLIC KEY-----)")
    try:
        return base64.b64decode("".join(match.group(1).split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise TrustError(f"the PEM public key is not valid base64 ({exc})") from exc


def key_id_of(pem: bytes) -> str:
    """``sha256:<hex>`` of the DER SubjectPublicKeyInfo, the id the manifest's ``key_id`` names."""
    return "sha256:" + hashlib.sha256(pem_to_der(pem)).hexdigest()


def keys_dir() -> Path:
    return KEYS_DIR


def load_keys(directory: Optional[Path] = None) -> List[ReleaseKey]:
    """Every ``release-*.pub`` in the key directory, sorted by file name. Unreadable or
    malformed files are skipped (a broken file must not lock out the others)."""
    directory = keys_dir() if directory is None else directory
    out: List[ReleaseKey] = []
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob(KEY_GLOB)):
        try:
            pem = path.read_bytes()
            der = pem_to_der(pem)
        except (OSError, TrustError):
            continue
        out.append(ReleaseKey("sha256:" + hashlib.sha256(der).hexdigest(), path, pem, der))
    return out


def require_keys(directory: Optional[Path] = None) -> List[ReleaseKey]:
    keys = load_keys(directory)
    if not keys:
        raise TrustError(NO_KEY_MESSAGE, fix=NO_KEY_FIX)
    return keys


def pinned_key(key_id: str, keys: Sequence[ReleaseKey]) -> ReleaseKey:
    for key in keys:
        if key.key_id == key_id:
            return key
    pinned = ", ".join(k.key_id for k in keys) or "none"
    raise TrustError(
        f"signed by key {key_id}, which this SDK does not trust (pinned: {pinned})",
        fix="pip install -U 'briefcase-ai[engine]'   # a newer SDK may carry the new release key; "
            "if it is already current, do not install this file")


# --------------------------------------------------------------------------- signatures


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def decode_signature(raw: bytes) -> bytes:
    """cosign's ``--output-signature`` file (base64 DER, maybe with a trailing newline). A raw
    DER signature is accepted too."""
    if len(raw) > MAX_SIGNATURE_BYTES:
        raise TrustError("the signature file is too large to be a signature")
    text = raw.strip()
    if text[:1] == b"\x30" and len(text) < 128:  # raw DER SEQUENCE
        return bytes(text)
    try:
        der = base64.b64decode(b"".join(text.split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise TrustError("the signature file is not base64 (expected `cosign sign-blob "
                         "--output-signature` output)") from exc
    if not der or der[:1] != b"\x30":
        raise TrustError("the signature is not an ECDSA DER signature")
    return der


def crypto_available() -> bool:
    try:
        import cryptography.hazmat.primitives.asymmetric.ec  # noqa: F401
    except ImportError:
        return False
    return True


Data = Union[bytes, Path]


def _digest(data: Data) -> bytes:
    if isinstance(data, bytes):
        return hashlib.sha256(data).digest()
    return bytes.fromhex(sha256_file(data))


def _verify_crypto(data: Data, signature: bytes, key: ReleaseKey) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, utils

    public = serialization.load_der_public_key(key.der)
    if not isinstance(public, ec.EllipticCurvePublicKey) or public.curve.name != "secp256r1":
        raise TrustError(f"pinned key {key.label} is not an ECDSA P-256 key")
    try:
        public.verify(signature, _digest(data), ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        return True
    except InvalidSignature:
        return False


def _verify_cosign(data: Data, der: bytes, key: ReleaseKey,
                   runner: Callable = subprocess.run, which: Callable = shutil.which) -> bool:
    cosign = which("cosign")
    if cosign is None:
        raise TrustError("cannot check signatures: neither the `cryptography` package nor `cosign` "
                         "is installed", fix="pip install 'briefcase-ai[engine]'")
    with tempfile.TemporaryDirectory(prefix="briefcase-verify-") as tmp:
        work = Path(tmp)
        (work / "key.pub").write_bytes(key.pem)
        (work / "blob.sig").write_bytes(base64.b64encode(der))
        if isinstance(data, bytes):
            blob = work / "blob"
            blob.write_bytes(data)
        else:
            blob = data
        result = runner([cosign, "verify-blob", "--key", str(work / "key.pub"), "--signature",
                         str(work / "blob.sig"), "--insecure-ignore-tlog=true", str(blob)],
                        capture_output=True, text=True)
    return getattr(result, "returncode", 1) == 0


def verify_blob(data: Data, signature: bytes, key: ReleaseKey, *, runner: Callable = subprocess.run,
                which: Callable = shutil.which) -> bool:
    """Whether ``signature`` (the raw ``.sig`` file contents) is ``key``'s signature over ``data``
    (bytes, or a file streamed from disk)."""
    der = decode_signature(signature)
    if crypto_available():
        return _verify_crypto(data, der, key)
    return _verify_cosign(data, der, key, runner, which)


def verify_with_any(data: Data, signature: bytes, keys: Sequence[ReleaseKey], **kw) -> Optional[ReleaseKey]:
    """The pinned key that signed ``data``, or ``None``."""
    for key in keys:
        if verify_blob(data, signature, key, **kw):
            return key
    return None


# --------------------------------------------------------------------------- versions


def parse_version(text: str) -> Tuple[int, ...]:
    """``"4.5.0"`` -> ``(4, 5, 0)``; pre-release and build suffixes are ignored."""
    core = re.split(r"[-+]", str(text).strip().lstrip("v"), maxsplit=1)[0]
    parts = []
    for piece in core.split("."):
        match = re.match(r"\d+", piece)
        if not match:
            raise ValueError(f"not a version: {text!r}")
        parts.append(int(match.group(0)))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def sdk_version() -> str:
    from briefcase import __version__

    return __version__


# --------------------------------------------------------------------------- manifest


@dataclass
class Manifest:
    data: Dict[str, Any]
    raw: bytes
    key: ReleaseKey

    @property
    def version(self) -> str:
        return self.data["version"]

    @property
    def key_id(self) -> str:
        return self.data["key_id"]

    @property
    def images(self) -> Dict[str, Dict[str, str]]:
        return self.data["images"]

    def binary_for(self, os_name: str, arch: str) -> Dict[str, Any]:
        for entry in self.data["binaries"]:
            if entry["os"] == os_name and entry["arch"] == arch:
                return entry
        have = sorted({f"{b['os']}/{b['arch']}" for b in self.data["binaries"]})
        raise TrustError(f"engine {self.version} has no oci-bai build for {os_name}/{arch} "
                         f"(available: {', '.join(have) or 'none'})",
                         fix="run the engine on a supported OS/architecture")

    def bundle_for(self, arch: str) -> Optional[Dict[str, Any]]:
        for entry in self.data["bundles"]:
            if entry["arch"] == arch:
                return entry
        return None

    def files(self) -> List[Dict[str, Any]]:
        return list(self.data["binaries"]) + list(self.data["bundles"])


def _fail_schema(what: str) -> TrustError:
    return TrustError(f"the engine manifest is malformed: {what}",
                      fix="download the manifest again from the portal; do not install this one")


def _file_entry(entry: Any, where: str, need_os: bool) -> None:
    if not isinstance(entry, dict):
        raise _fail_schema(f"{where} is not an object")
    keys = ("os", "arch", "file", "sha256", "size", "signature") if need_os else \
        ("arch", "file", "sha256", "size", "signature")
    for key in keys:
        if key not in entry:
            raise _fail_schema(f"{where} has no {key!r}")
    for key in ("file", "signature"):
        if not isinstance(entry[key], str) or not _SAFE_FILE.match(entry[key]):
            raise _fail_schema(f"{where}.{key} is not a plain file name: {entry[key]!r}")
    if not isinstance(entry["sha256"], str) or not _HEX64.match(entry["sha256"]):
        raise _fail_schema(f"{where}.sha256 is not 64 lowercase hex digits")
    if not isinstance(entry["size"], int) or isinstance(entry["size"], bool) or entry["size"] < 0:
        raise _fail_schema(f"{where}.size is not a non-negative integer")


def validate_manifest(data: Any) -> Dict[str, Any]:
    """Schema checks for manifest v2 (call after the signature verified)."""
    if not isinstance(data, dict):
        raise _fail_schema("not a JSON object")
    schema = data.get("schema_version")
    if schema != SCHEMA_VERSION:
        raise TrustError(
            f"the engine manifest has schema_version {schema!r}; this SDK understands only "
            f"{SCHEMA_VERSION}", fix="pip install -U 'briefcase-ai[engine]'")
    for key in ("version", "released_at", "key_id", "min_sdk_version"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise _fail_schema(f"{key!r} is missing")
    if not _KEY_ID.match(data["key_id"]):
        raise _fail_schema(f"key_id {data['key_id']!r} is not sha256:<64 hex>")
    if not re.match(r"^[0-9A-Za-z][0-9A-Za-z.+-]*$", data["version"]):
        raise _fail_schema(f"version {data['version']!r}")
    try:
        parse_version(data["min_sdk_version"])
    except ValueError:
        raise _fail_schema(f"min_sdk_version {data['min_sdk_version']!r}") from None
    for key in ("binaries", "bundles"):
        if not isinstance(data.get(key, []), list):
            raise _fail_schema(f"{key!r} is not a list")
        data.setdefault(key, [])
    for i, entry in enumerate(data["binaries"]):
        _file_entry(entry, f"binaries[{i}]", need_os=True)
    for i, entry in enumerate(data["bundles"]):
        _file_entry(entry, f"bundles[{i}]", need_os=False)
    images = data.get("images")
    if not isinstance(images, dict) or not images:
        raise _fail_schema("'images' is missing or empty")
    for name, image in images.items():
        if not _IMAGE_NAME.match(str(name)):
            raise _fail_schema(f"image name {name!r}")
        if not isinstance(image, dict):
            raise _fail_schema(f"images.{name} is not an object")
        if not isinstance(image.get("repository"), str) or not _REPOSITORY.match(image["repository"]):
            raise _fail_schema(f"images.{name}.repository {image.get('repository')!r}")
        if not isinstance(image.get("digest"), str) or not _DIGEST.match(image["digest"]):
            raise _fail_schema(f"images.{name}.digest is not sha256:<64 hex> (images must be pinned by digest)")
    if "notes" in data and not isinstance(data["notes"], str):
        raise _fail_schema("'notes' is not a string")
    return data


def check_sdk_version(data: Mapping[str, Any], running: Optional[str] = None) -> None:
    running = running or sdk_version()
    needed = data["min_sdk_version"]
    if parse_version(needed) > parse_version(running):
        raise TrustError(f"engine {data['version']} needs briefcase-ai {needed} or newer (this is {running})",
                         fix="pip install -U 'briefcase-ai[engine]'")


def load_manifest(raw: bytes, signature: bytes, keys: Optional[Sequence[ReleaseKey]] = None,
                  running_sdk: Optional[str] = None, **kw) -> Manifest:
    """Verify and parse a manifest: the key it names must be pinned, the signature must verify
    with that key, the schema must be v2, and this SDK must be new enough."""
    keys = require_keys() if keys is None else keys
    if not keys:
        raise TrustError(NO_KEY_MESSAGE, fix=NO_KEY_FIX)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise TrustError("the engine manifest is too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _fail_schema(f"not JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise _fail_schema("not a JSON object")
    key_id = data.get("key_id")
    if not isinstance(key_id, str) or not _KEY_ID.match(key_id):
        raise _fail_schema("it names no key_id (sha256:<64 hex>)")
    key = pinned_key(key_id, keys)
    if not verify_blob(raw, signature, key, **kw):
        raise TrustError(f"the engine manifest's signature does not verify with pinned key {key_id}: "
                         "the file was changed after it was signed, or the signature belongs to another file",
                         fix="download the manifest and its .sig again from the portal; do not install this one")
    validate_manifest(data)
    check_sdk_version(data, running_sdk)
    return Manifest(data=data, raw=raw, key=key)


def check_file(path: Path, entry: Mapping[str, Any], manifest: Manifest, signature: Optional[bytes] = None,
               **kw) -> str:
    """Check a downloaded file against its manifest entry (size, sha256) and, when given, its
    own signature (with the manifest's key). Returns ``"signature"`` or ``"sha256"``: what
    vouched for it beyond the signed manifest."""
    size = path.stat().st_size
    if size != entry["size"]:
        raise TrustError(f"{path.name}: size {size} does not match the signed manifest ({entry['size']})",
                         fix="download it again; do not install this file")
    actual = sha256_file(path)
    if actual != entry["sha256"]:
        raise TrustError(f"{path.name}: sha256 {actual} does not match the signed manifest ({entry['sha256']})",
                         fix="download it again; do not install this file")
    if signature is None:
        return "sha256"
    if not verify_blob(path, signature, manifest.key, **kw):
        raise TrustError(f"{path.name}: its signature does not verify with pinned key {manifest.key_id}",
                         fix="download the file and its .sig again; do not install this file")
    return "signature"


def manifest_entry_for(manifest: Manifest, path: Path, sha256: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The binaries/bundles entry for a file: by name, else by content."""
    for entry in manifest.files():
        if entry["file"] == path.name:
            return entry
    sha256 = sha256 or sha256_file(path)
    for entry in manifest.files():
        if entry["sha256"] == sha256:
            return entry
    return None
