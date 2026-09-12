"""Shared fixture helpers for billing tests (tourist tax requires billable guests)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.reservations.models import Guest


def make_guest(
    *,
    tenant,
    reservation,
    first_name: str,
    last_name: str,
    date_of_birth: date | None = date(1990, 1, 1),
    nationality: str = "HR",
    sex: str = "male",
    **kwargs,
) -> Guest:
    """Create a billable guest for tourist-tax / invoice builders.

    Secondary guests need DOB + nationality + sex (or document_number), otherwise
    ``guests_for_checkout`` treats them as unfilled and drops them from tax calc.
    """
    return Guest.objects.create(
        tenant=tenant,
        reservation=reservation,
        first_name=first_name,
        last_name=last_name,
        date_of_birth=date_of_birth,
        nationality=nationality,
        sex=sex,
        **kwargs,
    )


def make_test_p12(*, password: str = "secret", oib: str = "12345678901") -> SimpleUploadedFile:
    """Generate an in-memory PKCS#12 for ZKI / fiscal tests. Not a production cert."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "HR"),
            x509.NameAttribute(NameOID.COMMON_NAME, "Test FISKAL"),
            x509.NameAttribute(x509.ObjectIdentifier("2.5.4.97"), f"HR{oib}"),
        ]
    )
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    p12_bytes = serialization.pkcs12.serialize_key_and_certificates(
        name=b"test",
        key=key,
        cert=cert,
        cas=None,
        encryption_algorithm=serialization.BestAvailableEncryption(password.encode("utf-8")),
    )
    return SimpleUploadedFile("test.p12", p12_bytes)
