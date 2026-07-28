"""Build ObrazacPDV XML from fiscal settings + PDVAmounts.

Mapping rules live in ``amount_mapper`` — this class only serializes XML.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from lxml import etree

from apps.billing.models import ForeignServiceInvoice, TenantFiscalSettings
from apps.billing.services.eporezna.errors import PdvBuildError
from apps.billing.services.eporezna.filename import build_filename
from apps.billing.services.eporezna.metadata import EporeznaMetadataBuilder
from apps.billing.services.eporezna.pdv.amount_mapper import PDVAmountMapper, PDVAmounts
from apps.billing.services.eporezna.period import FiscalPeriod
from apps.billing.services.eporezna.readiness import fiscal_eporezna_readiness
from apps.billing.services.eporezna.source_data import has_source_fiscal_data
from apps.billing.services.eporezna.time_providers import (
    Clock,
    SystemClock,
    SystemUuidProvider,
    UuidProvider,
)
from apps.billing.services.eporezna.xml_helpers import (
    append_bool,
    append_decimal,
    append_text,
)
from apps.tenants.models import Tenant

NS = "http://e-porezna.porezna-uprava.hr/sheme/zahtjevi/ObrazacPDV/v11-0"
PDV_TITLE = "Obrazac PDV"
PDV_CONFORMS_TO = "ObrazacPDV-v11-0"
ZERO = Decimal("0.00")

_SCALAR_000_111 = (
    "Podatak000",
    *(f"Podatak{i}" for i in range(100, 112)),
)
_PAIR_200_215 = tuple(f"Podatak{i}" for i in range(200, 216))
_PAIR_300_314 = tuple(f"Podatak{i}" for i in range(300, 315))
_SCALAR_TAIL = (
    "Podatak315",
    "Podatak400",
    "Podatak500",
    "Podatak610",
    "Podatak611",
    "Podatak612",
    "Podatak613",
    "Podatak614",
    "Podatak615",
    "Podatak620",
    "Podatak630",
    "Podatak640",
    "Podatak650",
)
_PAIR_701_704 = tuple(f"Podatak{i}" for i in range(701, 705))


@dataclass(frozen=True)
class PDVExport:
    xml_bytes: bytes
    filename: str
    period_from: date
    period_to: date


def schema_path() -> Path:
    return Path(__file__).resolve().parent.parent / "schemas" / "ObrazacPDV-v11-0.xsd"


class PDVBuilder:
    """Serialize ObrazacPDV Zaglavlje + Tijelo from ``PDVAmounts``."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        uuids: UuidProvider | None = None,
    ) -> None:
        self._clock = clock or SystemClock()
        self._uuids = uuids or SystemUuidProvider()

    def build(self, *, tenant: Tenant, period: str) -> PDVExport:
        readiness = fiscal_eporezna_readiness(tenant)
        if not readiness.configured:
            raise PdvBuildError(
                "PDV fiscal settings incomplete: " + ", ".join(readiness.missing)
            )

        try:
            fiscal_period = FiscalPeriod.from_year_month(period)
        except ValueError as exc:
            raise PdvBuildError(str(exc)) from exc

        settings = (
            TenantFiscalSettings.objects.filter(tenant=tenant)
            .select_related("default_preparer")
            .get()
        )
        preparer = settings.default_preparer
        assert preparer is not None

        if not has_source_fiscal_data(tenant=tenant, period=fiscal_period.period):
            raise PdvBuildError("No source fiscal data for tax period.")

        invoices = list(
            ForeignServiceInvoice.objects.filter(
                tenant=tenant,
                tax_period=fiscal_period.period,
            ).order_by("invoice_number", "id")
        )
        amounts = PDVAmountMapper().map(invoices)
        if amounts.eu_services_base <= ZERO:
            raise PdvBuildError("No source fiscal data for tax period.")

        now = self._clock.now()
        ident = self._uuids.new()
        root = self._build_root(
            settings=settings,
            preparer=preparer,
            fiscal_period=fiscal_period,
            now=now,
            ident=ident,
            amounts=amounts,
        )
        xml_bytes = etree.tostring(
            root,
            xml_declaration=True,
            encoding="UTF-8",
            pretty_print=False,
        )
        return PDVExport(
            xml_bytes=xml_bytes,
            filename=build_filename(
                form="PDV",
                oib=settings.issuer_oib,
                period=fiscal_period,
            ),
            period_from=fiscal_period.date_from,
            period_to=fiscal_period.date_to,
        )

    def _build_root(
        self,
        *,
        settings: TenantFiscalSettings,
        preparer,
        fiscal_period: FiscalPeriod,
        now,
        ident,
        amounts: PDVAmounts,
    ) -> etree._Element:
        root = etree.Element(
            f"{{{NS}}}ObrazacPDV",
            nsmap={None: NS},
            verzijaSheme="11.0",
        )

        autor = f"{preparer.first_name} {preparer.last_name}".strip()
        EporeznaMetadataBuilder().append(
            root,
            title=PDV_TITLE,
            conforms_to=PDV_CONFORMS_TO,
            autor=autor,
            now=now,
            ident=ident,
        )

        zaglavlje = etree.SubElement(root, f"{{{NS}}}Zaglavlje")
        razdoblje = etree.SubElement(zaglavlje, f"{{{NS}}}Razdoblje")
        append_text(
            razdoblje, "DatumOd", fiscal_period.date_from.isoformat(), ns=NS
        )
        append_text(
            razdoblje, "DatumDo", fiscal_period.date_to.isoformat(), ns=NS
        )

        obveznik = etree.SubElement(zaglavlje, f"{{{NS}}}Obveznik")
        append_text(obveznik, "Ime", settings.issuer_first_name, ns=NS)
        append_text(obveznik, "Prezime", settings.issuer_last_name, ns=NS)
        append_text(obveznik, "OIB", settings.issuer_oib, ns=NS)
        adresa = etree.SubElement(obveznik, f"{{{NS}}}Adresa")
        append_text(adresa, "Mjesto", settings.issuer_place, ns=NS)
        append_text(adresa, "Ulica", settings.issuer_street, ns=NS)
        append_text(adresa, "Broj", settings.issuer_street_number or "", ns=NS)

        sastavio = etree.SubElement(zaglavlje, f"{{{NS}}}ObracunSastavio")
        append_text(sastavio, "Ime", preparer.first_name, ns=NS)
        append_text(sastavio, "Prezime", preparer.last_name, ns=NS)

        append_text(zaglavlje, "Ispostava", settings.tax_office_code, ns=NS)

        tijelo = etree.SubElement(root, f"{{{NS}}}Tijelo")
        self._append_tijelo(tijelo, amounts)
        return root

    def _append_tijelo(self, tijelo: etree._Element, amounts: PDVAmounts) -> None:
        """Emit v11-0 Tijelo; only II.10 / II UKUPNO / IV carry reverse-charge amounts."""
        for tag in _SCALAR_000_111:
            append_decimal(tijelo, tag, ZERO, ns=NS)

        for tag in _PAIR_200_215:
            pair = etree.SubElement(tijelo, f"{{{NS}}}{tag}")
            if tag == "Podatak200":
                # II UKUPNO — only II.10 is non-zero for current foreign-service invoices.
                append_decimal(pair, "Vrijednost", amounts.eu_services_base, ns=NS)
                append_decimal(pair, "Porez", amounts.eu_services_vat, ns=NS)
            elif tag == "Podatak210":
                # II.10 Primljene usluge iz EU po stopi 25%.
                append_decimal(pair, "Vrijednost", amounts.eu_services_base, ns=NS)
                append_decimal(pair, "Porez", amounts.eu_services_vat, ns=NS)
            else:
                append_decimal(pair, "Vrijednost", ZERO, ns=NS)
                append_decimal(pair, "Porez", ZERO, ns=NS)

        for tag in _PAIR_300_314:
            # III pretporez stays zero for paušalist (no deduction).
            pair = etree.SubElement(tijelo, f"{{{NS}}}{tag}")
            append_decimal(pair, "Vrijednost", ZERO, ns=NS)
            append_decimal(pair, "Porez", ZERO, ns=NS)

        for tag in _SCALAR_TAIL:
            if tag == "Podatak400":
                append_decimal(tijelo, tag, amounts.payable, ns=NS)
            else:
                append_decimal(tijelo, tag, ZERO, ns=NS)

        append_bool(tijelo, "Podatak660", False, ns=NS)

        for tag in _PAIR_701_704:
            pair = etree.SubElement(tijelo, f"{{{NS}}}{tag}")
            append_decimal(pair, "NabavnaVrijednost", ZERO, ns=NS)
            append_decimal(pair, "ProdajnaVrijednost", ZERO, ns=NS)

        append_decimal(tijelo, "Povrat", ZERO, ns=NS)
        etree.SubElement(tijelo, f"{{{NS}}}PodaciZaUstup")
        append_decimal(tijelo, "Predujam", ZERO, ns=NS)
        append_decimal(tijelo, "UstupPovrata", ZERO, ns=NS)
