from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import pkcs12

from apps.billing.exceptions import FiscalConfigError
from apps.billing.models import TenantFiscalSettings


def format_amount_for_zki(amount: Decimal) -> str:
    """Pravilnik: decimal separator for ZKI input is a dot."""
    quantized = amount.quantize(Decimal("0.01"))
    return f"{quantized:.2f}"


def format_datetime_for_zki(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y %H:%M:%S")


def build_zki_input_string(
    *,
    oib: str,
    issued_at: datetime,
    invoice_number: str,
    business_premise_code: str,
    payment_device_code: str,
    total: Decimal,
) -> str:
    """Prescribed ZKI input: OIB + datetime + BrOznRac + OznPosPr + OznNapUr + amount."""
    return (
        f"{oib.strip()}"
        f"{format_datetime_for_zki(issued_at)}"
        f"{invoice_number.strip()}"
        f"{business_premise_code.strip()}"
        f"{payment_device_code.strip()}"
        f"{format_amount_for_zki(total)}"
    )


def load_fiscal_private_key(settings: TenantFiscalSettings) -> RSAPrivateKey:
    if not settings.certificate_file:
        raise FiscalConfigError("Tenant fiscal certificate file is missing.")
    password = settings.get_certificate_password()
    if not password:
        raise FiscalConfigError("Tenant fiscal certificate password is missing.")

    p12_bytes = settings.certificate_file.read()
    settings.certificate_file.seek(0)
    private_key, certificate, _additional = pkcs12.load_key_and_certificates(
        p12_bytes,
        password.encode("utf-8"),
    )
    if private_key is None or certificate is None:
        raise FiscalConfigError("Certificate file does not contain key/certificate pair.")
    if not isinstance(private_key, RSAPrivateKey):
        raise FiscalConfigError("Fiscal certificate private key must be RSA.")
    return private_key


def calculate_zki(
    *,
    oib: str,
    issued_at: datetime,
    invoice_number: str,
    business_premise_code: str,
    payment_device_code: str,
    total: Decimal,
    private_key: RSAPrivateKey,
) -> str:
    """Zaštitni kod izdavatelja: RSA-SHA1 of the prescribed string, then MD5 of the signature."""
    payload = build_zki_input_string(
        oib=oib,
        issued_at=issued_at,
        invoice_number=invoice_number,
        business_premise_code=business_premise_code,
        payment_device_code=payment_device_code,
        total=total,
    )
    signature = private_key.sign(
        payload.encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA1(),
    )
    return hashlib.md5(signature).hexdigest()
