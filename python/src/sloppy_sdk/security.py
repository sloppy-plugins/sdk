"""Persistent per-application identities and explicit certificate pinning."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from ._errors import SloppyError

_MISSING = object()


@dataclass(frozen=True)
class PresentedService:
    endpoint: str
    fingerprint: str
    certificate_pem: str
    name: str


class SloppyTrustRequiredError(SloppyError):
    def __init__(
        self, presented: PresentedService, known_fingerprint: str | None = None
    ):
        self.presented = presented
        self.known_fingerprint = known_fingerprint
        super().__init__(
            "server_identity_changed" if known_fingerprint else "server_trust_required",
            (
                f"Sloppy certificate changed at {presented.endpoint}: expected "
                f"{known_fingerprint}, received {presented.fingerprint}. Compare the new fingerprint in Sloppy Settings before explicitly replacing the saved certificate."
                if known_fingerprint
                else f"Confirm Sloppy certificate at {presented.endpoint}: {presented.fingerprint}. Compare this fingerprint in Sloppy Settings, then explicitly approve this service."
            ),
        )


def certificate_fingerprint(der: bytes) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(der).digest()).decode().rstrip(
        "="
    )


def canonical_endpoint(endpoint: str) -> str:
    try:
        url = urlsplit(endpoint)
        port = url.port
        host = url.hostname
        if (
            url.scheme != "https"
            or not host
            or url.username is not None
            or url.password is not None
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
            or port == 0
            or any(ord(char) <= 32 for char in endpoint)
        ):
            raise ValueError("expected an HTTPS authority")
        host = host.encode("idna").decode().lower()
        if ":" in host:
            host = f"[{host}]"
        return f"https://{host}" + (f":{port}" if port not in {None, 443} else "")
    except (ValueError, UnicodeError) as error:
        raise SloppyError(
            "invalid_input",
            "Service endpoint must be an HTTPS authority without credentials or a path",
        ) from error


def _directory(application: str, plugin_id: str, directory, profile) -> Path:
    if directory is None:
        directory = os.environ.get("SLOPPY_IDENTITY_DIRECTORY")
    if directory is not None:
        path = Path(directory)
        if not path.is_absolute():
            raise SloppyError("invalid_input", "Identity directory must be absolute")
        return path
    profile = profile or os.environ.get("SLOPPY_IDENTITY_PROFILE")
    if profile is None:
        profile = hashlib.sha256((application + "\0" + plugin_id).encode()).hexdigest()
    if (
        not isinstance(profile, str)
        or not 1 <= len(profile) <= 128
        or not all(
            char.isascii() and (char.isalnum() or char in "_-") for char in profile
        )
    ):
        raise SloppyError("invalid_input", "Invalid identity profile")
    if sys.platform == "darwin":
        root = Path.home() / "Library/Application Support/Sloppy"
    elif os.name == "nt":
        if not os.environ.get("APPDATA"):
            raise SloppyError("identity_error", "APPDATA is not set")
        root = Path(os.environ["APPDATA"]) / "Sloppy"
    else:
        root = (
            Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
            / "sloppy"
        )
    return root / "identities" / profile


@contextmanager
def _locked(directory: Path):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = directory / ".security-lock"
    deadline = time.monotonic() + 5
    while True:
        try:
            lock.mkdir(mode=0o700)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise SloppyError(
                    "identity_error",
                    f"Security storage is locked: {lock}; if its owner exited, remove this empty lock directory",
                ) from None
            time.sleep(0.025)
    try:
        yield
    finally:
        lock.rmdir()


def _read_optional(file: Path):
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _MISSING
    except (OSError, ValueError) as error:
        raise SloppyError(
            "identity_error", f"Cannot read security storage: {error}"
        ) from error


def _private_json(directory: Path, filename: str, value):
    destination = directory / filename
    if destination.is_symlink():
        raise SloppyError("identity_error", "Refusing to replace a symlink")
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=directory,
        prefix=".sloppy-",
        suffix=".tmp",
        delete=False,
    ) as file:
        temporary = Path(file.name)
        try:
            json.dump(value, file, ensure_ascii=True, indent=2, allow_nan=False)
            file.flush()
            os.fsync(file.fileno())
        except BaseException:
            temporary.unlink()
            raise
    try:
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _generate(name: str) -> dict:
    if (
        not name.strip()
        or len(name.encode()) > 64
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
    ):
        raise SloppyError("invalid_input", "Invalid certificate name")
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage(
                [ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return {
        "version": 1,
        "name": name,
        "certificate_pem": certificate.public_bytes(
            serialization.Encoding.PEM
        ).decode(),
        "private_key_pem": key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(),
    }


def _validate_identity(value):
    try:
        if (
            not isinstance(value, dict)
            or value["version"] != 1
            or not isinstance(value["name"], str)
        ):
            raise ValueError("invalid identity version or name")
        certificate = x509.load_pem_x509_certificate(value["certificate_pem"].encode())
        key = serialization.load_pem_private_key(
            value["private_key_pem"].encode(), password=None
        )
        encoding, form = (
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        if certificate.public_key().public_bytes(
            encoding, form
        ) != key.public_key().public_bytes(encoding, form):
            raise ValueError("certificate and key do not match")
        return value
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise SloppyError(
            "identity_error", f"Invalid identity file: {error}"
        ) from error


def _certificate(pem: str):
    return x509.load_pem_x509_certificate(pem.encode())


def inspect_service(endpoint: str) -> PresentedService:
    endpoint = canonical_endpoint(endpoint)
    url = urlsplit(endpoint)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
    with socket.create_connection((url.hostname, url.port or 443), timeout=5) as raw:
        with context.wrap_socket(raw, server_hostname=url.hostname) as connection:
            der = connection.getpeercert(binary_form=True)
            certificate = x509.load_der_x509_certificate(der)
            return PresentedService(
                endpoint,
                certificate_fingerprint(der),
                certificate.public_bytes(serialization.Encoding.PEM).decode(),
                certificate.subject.rfc4514_string(),
            )


class _PinnedContext(ssl.SSLContext):
    def __new__(cls, fingerprint):
        return super().__new__(cls, ssl.PROTOCOL_TLS_CLIENT)

    def __init__(self, fingerprint):
        self.fingerprint = fingerprint
        self.check_hostname = False
        self.verify_mode = ssl.CERT_NONE
        self.minimum_version = self.maximum_version = ssl.TLSVersion.TLSv1_3

    def wrap_socket(self, *args, **kwargs):
        connection = super().wrap_socket(*args, **kwargs)
        # OpenSSL proves possession during TLS. Pin before the caller can send
        # WebSocket headers, bearer tokens, or application data.
        if (
            certificate_fingerprint(connection.getpeercert(binary_form=True))
            != self.fingerprint
        ):
            connection.close()
            raise SloppyError(
                "server_identity_changed",
                "Sloppy certificate changed during connection; explicit replacement is required",
            )
        return connection


class SloppyIdentity:
    def __init__(
        self, *, application: str, plugin_id: str, directory=None, profile=None
    ):
        self.directory = _directory(application, plugin_id, directory, profile)
        self.name = application

    def _load(self):
        with _locked(self.directory):
            value = _read_optional(self.directory / "identity.json")
            if value is not _MISSING:
                return _validate_identity(value)
            value = _generate(self.name)
            _private_json(self.directory, "identity.json", value)
            return value

    def fingerprint(self) -> str:
        return certificate_fingerprint(
            _certificate(self._load()["certificate_pem"]).public_bytes(
                serialization.Encoding.DER
            )
        )

    def regenerate(self) -> str:
        with _locked(self.directory):
            value = _generate(self.name)
            _private_json(self.directory, "identity.json", value)
            return certificate_fingerprint(
                _certificate(value["certificate_pem"]).public_bytes(
                    serialization.Encoding.DER
                )
            )

    def _known(self):
        known = _read_optional(self.directory / "known_sloppy")
        if known is _MISSING:
            return {"version": 1, "services": {}}
        try:
            if known["version"] != 1 or not isinstance(known["services"], dict):
                raise ValueError("invalid known_sloppy version or services")
            for endpoint, entry in known["services"].items():
                if (
                    endpoint != canonical_endpoint(endpoint)
                    or entry["endpoint"] != endpoint
                    or not isinstance(entry["name"], str)
                    or certificate_fingerprint(
                        _certificate(entry["certificate_pem"]).public_bytes(
                            serialization.Encoding.DER
                        )
                    )
                    != entry["fingerprint"]
                ):
                    raise ValueError("inconsistent known_sloppy entry")
            return known
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise SloppyError(
                "identity_error", f"Invalid known_sloppy: {error}"
            ) from error

    def list_known_services(self) -> list[dict]:
        with _locked(self.directory):
            return list(self._known()["services"].values())

    def remember_service(
        self, service: PresentedService, expected_old_fingerprint: str | None = None
    ):
        endpoint = canonical_endpoint(service.endpoint)
        if (
            certificate_fingerprint(
                _certificate(service.certificate_pem).public_bytes(
                    serialization.Encoding.DER
                )
            )
            != service.fingerprint
        ):
            raise SloppyError(
                "invalid_input", "Presented certificate fingerprint does not match"
            )
        with _locked(self.directory):
            known = self._known()
            old = known["services"].get(endpoint)
            if (old["fingerprint"] if old else None) != expected_old_fingerprint:
                raise SloppyError(
                    "server_identity_changed",
                    "Known service changed; explicit approval of its current fingerprint is required",
                )
            known["services"][endpoint] = {
                **asdict(service),
                "endpoint": endpoint,
                "created_at_ms": old["created_at_ms"]
                if old
                else int(time.time() * 1000),
                "last_connected_at_ms": None,
            }
            _private_json(self.directory, "known_sloppy", known)

    def remove_known_service(self, endpoint: str, expected_fingerprint: str):
        endpoint = canonical_endpoint(endpoint)
        with _locked(self.directory):
            known = self._known()
            if (
                known["services"].get(endpoint, {}).get("fingerprint")
                != expected_fingerprint
            ):
                raise SloppyError(
                    "server_identity_changed",
                    "Known service changed; fingerprint does not match",
                )
            del known["services"][endpoint]
            _private_json(self.directory, "known_sloppy", known)

    def verify_service(self, endpoint: str) -> PresentedService:
        presented = inspect_service(endpoint)
        known = next(
            (
                entry
                for entry in self.list_known_services()
                if entry["endpoint"] == presented.endpoint
            ),
            None,
        )
        if known is None or known["fingerprint"] != presented.fingerprint:
            raise SloppyTrustRequiredError(
                presented, known["fingerprint"] if known else None
            )
        return presented

    def _client_context(self, fingerprint: str) -> ssl.SSLContext:
        identity = self._load()
        context = _PinnedContext(fingerprint)
        with tempfile.TemporaryDirectory(prefix="sloppy-tls-") as directory:
            certificate, key = (
                Path(directory) / "certificate.pem",
                Path(directory) / "key.pem",
            )
            for file, contents in (
                (certificate, identity["certificate_pem"]),
                (key, identity["private_key_pem"]),
            ):
                with open(
                    file,
                    "x",
                    encoding="utf-8",
                    opener=lambda path, flags: os.open(path, flags, 0o600),
                ) as output:
                    output.write(contents)
            context.load_cert_chain(certificate, key)
        return context
