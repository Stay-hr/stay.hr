from __future__ import annotations

import logging
from decimal import Decimal
from uuid import uuid4

import httpx
from cryptography.hazmat.primitives.serialization import pkcs12
from django.utils import timezone
from lxml import etree
from signxml import XMLSigner, methods, namespaces

from apps.billing.exceptions import FiscalizationError
from apps.billing.models import FiscalizationAttempt, Invoice, InvoiceLine, TenantFiscalSettings
from apps.billing.services.fisk1 import FiscalResult, FiscalizationConnector
from apps.billing.services.fisk1.cis_tls import cis_verify_path
from apps.billing.services.fisk1.timing import (
    format_f73_datetime,
    issued_at_for_f1,
    message_at_for_f1,
)
from apps.billing.services.fisk1.xml_builder import (
    build_racun_xml,
    format_cis_http_error,
    parse_jir_from_response,
    recipient_oib_for_f1,
)
from apps.billing.services.payment import fisk1_payment_code
from apps.billing.services.pdf import render_invoice_pdf

logger = logging.getLogger(__name__)

FISK1_TEST_URL = "https://cistest.apis-it.hr:8449/FiskalizacijaServiceTest"
FISK1_PROD_URL = "https://cis.porezna-uprava.hr:8449/FiskalizacijaService"
# Official WSDL-PROD v1.10 soap:operation for racuni. SOAP 1.1 quotes the header.
SOAP_ACTION = (
    "http://e-porezna.porezna-uprava.hr/fiskalizacija/2012/services/"
    "FiskalizacijaService/racuni"
)
_TOURIST_TAX_KINDS = frozenset(
    {
        InvoiceLine.LineKind.TOURIST_TAX_ADULT,
        InvoiceLine.LineKind.TOURIST_TAX_CHILD,
    }
)
_SNAPSHOT_LIMIT = 20000


def _nontaxable_amount(invoice: Invoice) -> Decimal:
    total = Decimal("0.00")
    for line in invoice.lines.all():
        if line.line_kind in _TOURIST_TAX_KINDS:
            total += line.line_total
    return total


class CisF1XMLSigner(XMLSigner):
    """CIS F1 still mandates RSA-SHA1. signxml 4 rejects SHA1 in the constructor."""

    def check_deprecated_methods(self) -> None:
        return None


def _operator_oib(settings: TenantFiscalSettings) -> str:
    code = (settings.operator_code or "").strip()
    digits = "".join(ch for ch in code if ch.isdigit())
    if len(digits) >= 11:
        return digits[:11]
    return settings.issuer_oib


def _load_private_key_and_cert(settings: TenantFiscalSettings):
    password = settings.get_certificate_password().encode("utf-8")
    p12_bytes = settings.certificate_file.read()
    settings.certificate_file.seek(0)
    private_key, certificate, _additional = pkcs12.load_key_and_certificates(
        p12_bytes,
        password,
    )
    if private_key is None or certificate is None:
        raise FiscalizationError("Certificate file does not contain key/certificate pair.")
    return private_key, certificate


def _sign_xml(root: etree._Element, settings: TenantFiscalSettings) -> bytes:
    private_key, certificate = _load_private_key_and_cert(settings)
    signer = CisF1XMLSigner(
        method=methods.enveloped,
        signature_algorithm="rsa-sha1",
        digest_algorithm="sha1",
        c14n_algorithm="http://www.w3.org/2001/10/xml-exc-c14n#",
    )
    # Porezna F73 sample uses a default xmldsig namespace, not ds:.
    signer.namespaces = {None: namespaces.ds}
    signed = signer.sign(
        root,
        key=private_key,
        cert=[certificate],
        reference_uri="#RacunZahtjev",
    )
    return etree.tostring(signed, encoding="UTF-8")


def _wrap_soap(body_xml: bytes) -> str:
    body = body_xml.decode("utf-8")
    if body.startswith("<?xml"):
        body = body.split("?>", 1)[1].lstrip()
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soapenv:Body>"
        f"{body}"
        "</soapenv:Body>"
        "</soapenv:Envelope>"
    )


class Fisk1Connector(FiscalizationConnector):
    def __init__(self, *, http_client: httpx.Client | None = None):
        self._http_client = http_client

    def fiscalize(self, invoice: Invoice, settings: TenantFiscalSettings) -> FiscalResult:
        accommodation = invoice.lines.filter(
            line_kind=InvoiceLine.LineKind.ACCOMMODATION
        ).first()
        if accommodation is None:
            raise FiscalizationError("Invoice has no accommodation line.")

        issued_at_iso = format_f73_datetime(issued_at_for_f1(invoice))
        root = build_racun_xml(
            oib=settings.issuer_oib,
            issued_at_iso=issued_at_iso,
            sequence_number=invoice.sequence_number,
            vat_rate=accommodation.vat_rate,
            vat_base=accommodation.unit_price * accommodation.quantity,
            vat_amount=accommodation.vat_amount,
            total=invoice.total,
            payment_code=fisk1_payment_code(invoice.payment_method),
            operator_oib=_operator_oib(settings),
            zki=invoice.zki,
            business_premise_code=settings.business_premise_code,
            payment_device_code=settings.payment_device_code,
            message_id=str(uuid4()),
            message_at_iso=format_f73_datetime(message_at_for_f1(invoice)),
            in_vat_system=settings.is_vat_registered,
            recipient_oib=recipient_oib_for_f1(invoice.buyer_document_number),
            nontaxable_amount=_nontaxable_amount(invoice),
        )
        signed_xml = _sign_xml(root, settings)
        soap_payload = _wrap_soap(signed_xml)
        endpoint = FISK1_TEST_URL if settings.use_test_endpoint else FISK1_PROD_URL

        client = self._http_client or httpx.Client(
            timeout=30.0,
            verify=cis_verify_path(),
        )
        close_client = self._http_client is None
        try:
            response = client.post(
                endpoint,
                content=soap_payload.encode("utf-8"),
                headers={
                    "Content-Type": "text/xml; charset=utf-8",
                    "SOAPAction": f'"{SOAP_ACTION}"',
                },
            )
        finally:
            if close_client:
                client.close()

        response_text = response.text
        if response.status_code >= 400:
            raise FiscalizationError(
                format_cis_http_error(response.status_code, response_text),
                request_snapshot=soap_payload[:_SNAPSHOT_LIMIT],
                response_snapshot=response_text[:_SNAPSHOT_LIMIT],
            )
        try:
            jir = parse_jir_from_response(response_text)
        except ValueError as exc:
            raise FiscalizationError(
                format_cis_http_error(response.status_code, response_text),
                request_snapshot=soap_payload[:_SNAPSHOT_LIMIT],
                response_snapshot=response_text[:_SNAPSHOT_LIMIT],
            ) from exc

        return FiscalResult(
            jir=jir,
            request_snapshot=soap_payload[:_SNAPSHOT_LIMIT],
            response_snapshot=response_text[:_SNAPSHOT_LIMIT],
        )


def apply_fiscalization_result(
    invoice: Invoice,
    settings: TenantFiscalSettings,
    result: FiscalResult,
    *,
    attempt_no: int,
    fiskal_request_id=None,
) -> None:
    invoice.jir = result.jir
    invoice.fiscal_status = Invoice.FiscalStatus.FISCALIZED
    invoice.fiscal_error = ""
    invoice.fiscalized_at = timezone.now()
    invoice.save(
        update_fields=[
            "jir",
            "fiscal_status",
            "fiscal_error",
            "fiscalized_at",
            "updated_at",
        ]
    )
    FiscalizationAttempt.objects.create(
        invoice=invoice,
        attempt_no=attempt_no,
        success=True,
        request_snapshot=result.request_snapshot[:_SNAPSHOT_LIMIT],
        response_snapshot=result.response_snapshot[:_SNAPSHOT_LIMIT],
        fiskal_request_id=fiskal_request_id or result.fiskal_request_id,
    )
    render_invoice_pdf(invoice, settings)


def record_fiscalization_failure(
    invoice: Invoice,
    *,
    attempt_no: int,
    error_message: str,
    request_snapshot: str = "",
    response_snapshot: str = "",
    fiskal_request_id=None,
) -> None:
    invoice.fiscal_status = Invoice.FiscalStatus.FAILED
    invoice.fiscal_error = error_message[:2000]
    invoice.save(update_fields=["fiscal_status", "fiscal_error", "updated_at"])
    FiscalizationAttempt.objects.create(
        invoice=invoice,
        attempt_no=attempt_no,
        success=False,
        error_message=error_message[:2000],
        request_snapshot=request_snapshot[:_SNAPSHOT_LIMIT],
        response_snapshot=response_snapshot[:_SNAPSHOT_LIMIT],
        fiskal_request_id=fiskal_request_id,
    )
