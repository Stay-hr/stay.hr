"""Build ObrazacPDV XML — structure only in PR1 (no tax calculation).

PDVBuilder intentionally contains no tax calculation logic. PR1 serializes
the official v11-0 structure only. Tax computation will be introduced in a
dedicated mapping layer once an official filled reference export is available.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from lxml import etree

from apps.billing.models import TenantFiscalSettings
from apps.billing.services.eporezna.errors import PdvBuildError
from apps.billing.services.eporezna.filename import build_filename
from apps.billing.services.eporezna.metadata import EporeznaMetadataBuilder
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

# Scalar money tags in Tijelo (reference v11-0 zero export order).
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
    """Serialize ObrazacPDV Zaglavlje + zero Tijelo (PR1 structure only)."""

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

        now = self._clock.now()
        ident = self._uuids.new()
        root = self._build_root(
            settings=settings,
            preparer=preparer,
            fiscal_period=fiscal_period,
            now=now,
            ident=ident,
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

        # ePorezna v11 reference: ObracunSastavio has Ime + Prezime only (no Email).
        sastavio = etree.SubElement(zaglavlje, f"{{{NS}}}ObracunSastavio")
        append_text(sastavio, "Ime", preparer.first_name, ns=NS)
        append_text(sastavio, "Prezime", preparer.last_name, ns=NS)

        append_text(zaglavlje, "Ispostava", settings.tax_office_code, ns=NS)

        tijelo = etree.SubElement(root, f"{{{NS}}}Tijelo")
        self._append_zero_tijelo(tijelo)
        return root

    def _append_zero_tijelo(self, tijelo: etree._Element) -> None:
        """Emit reference-shaped zero body — no tax mapping in PR1."""
        for tag in _SCALAR_000_111:
            append_decimal(tijelo, tag, ZERO, ns=NS)

        for tag in _PAIR_200_215:
            pair = etree.SubElement(tijelo, f"{{{NS}}}{tag}")
            append_decimal(pair, "Vrijednost", ZERO, ns=NS)
            append_decimal(pair, "Porez", ZERO, ns=NS)

        for tag in _PAIR_300_314:
            pair = etree.SubElement(tijelo, f"{{{NS}}}{tag}")
            append_decimal(pair, "Vrijednost", ZERO, ns=NS)
            append_decimal(pair, "Porez", ZERO, ns=NS)

        for tag in _SCALAR_TAIL:
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
