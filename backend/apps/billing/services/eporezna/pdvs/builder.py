from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from lxml import etree

from apps.billing.models import ForeignServiceInvoice, TenantFiscalSettings
from apps.billing.services.eporezna.errors import PdvsBuildError
from apps.billing.services.eporezna.pdvs.line_mapper import PDVSLine, PDVSLineMapper
from apps.billing.services.eporezna.readiness import fiscal_pdvs_readiness
from apps.billing.services.eporezna.time_providers import (
    Clock,
    SystemClock,
    SystemUuidProvider,
    UuidProvider,
)
from apps.tenants.models import Tenant

NS = "http://e-porezna.porezna-uprava.hr/sheme/zahtjevi/ObrazacPDVS/v1-0"
META_NS = "http://e-porezna.porezna-uprava.hr/sheme/Metapodaci/v2-0"
PDVS_TITLE = (
    "Prijava za stjecanje dobara i primljene usluge "
    "iz drugih država članica Europske unije"
)


@dataclass(frozen=True)
class PDVSExport:
    xml_bytes: bytes
    filename: str
    period_from: date
    period_to: date
    invoice_count: int


def _period_bounds(period: str) -> tuple[date, date]:
    try:
        year_s, month_s = period.split("-", 1)
        year, month = int(year_s), int(month_s)
        if month < 1 or month > 12:
            raise ValueError
    except ValueError as exc:
        raise PdvsBuildError(f"Invalid period {period!r}; expected YYYY-MM") from exc
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    return start, end


def schema_path() -> Path:
    return Path(__file__).resolve().parent.parent / "schemas" / "ObrazacPDVS-v1-0.xsd"


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):.2f}"


class PDVSBuilder:
    """Build ObrazacPDVS XML from fiscal settings + PDVSLine list.

    Deterministic for a given tenant + period + clock/uuid providers.
    Only Metapodaci Datum / Identifikator vary when clock/uuid change.
    Mapping rules live in ``line_mapper`` — this class only serializes XML.
    """

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        uuids: UuidProvider | None = None,
    ) -> None:
        self._clock = clock or SystemClock()
        self._uuids = uuids or SystemUuidProvider()

    def build(self, *, tenant: Tenant, period: str) -> PDVSExport:
        readiness = fiscal_pdvs_readiness(tenant)
        if not readiness.configured:
            raise PdvsBuildError(
                "PDV-S fiscal settings incomplete: "
                + ", ".join(readiness.missing)
            )

        settings = (
            TenantFiscalSettings.objects.filter(tenant=tenant)
            .select_related("default_preparer")
            .get()
        )
        preparer = settings.default_preparer
        assert preparer is not None  # guaranteed by readiness

        period_from, period_to = _period_bounds(period)
        invoices = list(
            ForeignServiceInvoice.objects.filter(tenant=tenant, tax_period=period).order_by(
                "invoice_number", "id"
            )
        )
        if not invoices:
            raise PdvsBuildError("No invoices for tax period.")

        lines = PDVSLineMapper().map(invoices)
        if not lines:
            raise PdvsBuildError("No invoices for tax period.")

        now = self._clock.now()
        ident = self._uuids.new()
        root = self._build_root(
            settings=settings,
            preparer=preparer,
            period_from=period_from,
            period_to=period_to,
            now=now,
            ident=ident,
            lines=lines,
        )
        xml_bytes = etree.tostring(
            root,
            xml_declaration=True,
            encoding="UTF-8",
            pretty_print=False,
        )
        filename = (
            f"PDV-S_{settings.issuer_oib}_"
            f"{period_from.strftime('%Y%m%d')}-{period_to.strftime('%Y%m%d')}.xml"
        )
        return PDVSExport(
            xml_bytes=xml_bytes,
            filename=filename,
            period_from=period_from,
            period_to=period_to,
            invoice_count=len(invoices),
        )

    def _build_root(
        self,
        *,
        settings: TenantFiscalSettings,
        preparer,
        period_from: date,
        period_to: date,
        now: datetime,
        ident: UUID,
        lines: list[PDVSLine],
    ) -> etree._Element:
        # Match filled ePorezna exports: root xmlns + verzijaSheme only.
        root = etree.Element(
            f"{{{NS}}}ObrazacPDVS",
            nsmap={None: NS},
            verzijaSheme="1.0",
        )

        meta = etree.SubElement(root, f"{{{META_NS}}}Metapodaci", nsmap={None: META_NS})

        def meta_field(tag: str, dc: str, text: str) -> None:
            el = etree.SubElement(meta, f"{{{META_NS}}}{tag}")
            el.set("dc", dc)
            el.text = text

        autor = f"{preparer.first_name} {preparer.last_name}".strip()
        meta_field("Naslov", "http://purl.org/dc/elements/1.1/title", PDVS_TITLE)
        meta_field("Autor", "http://purl.org/dc/elements/1.1/creator", autor)
        meta_field(
            "Datum",
            "http://purl.org/dc/elements/1.1/date",
            now.strftime("%Y-%m-%dT%H:%M:%S"),
        )
        meta_field("Format", "http://purl.org/dc/elements/1.1/format", "text/xml")
        meta_field("Jezik", "http://purl.org/dc/elements/1.1/language", "hr-HR")
        meta_field(
            "Identifikator",
            "http://purl.org/dc/elements/1.1/identifier",
            str(ident),
        )
        meta_field(
            "Uskladjenost",
            "http://purl.org/dc/terms/conformsTo",
            "ObrazacPDVS-v1-0",
        )
        meta_field("Tip", "http://purl.org/dc/elements/1.1/type", "Elektronički obrazac")
        adresant = etree.SubElement(meta, f"{{{META_NS}}}Adresant")
        adresant.text = "Ministarstvo Financija, Porezna uprava, Zagreb"

        zaglavlje = etree.SubElement(root, f"{{{NS}}}Zaglavlje")
        razdoblje = etree.SubElement(zaglavlje, f"{{{NS}}}Razdoblje")
        etree.SubElement(razdoblje, f"{{{NS}}}DatumOd").text = period_from.isoformat()
        etree.SubElement(razdoblje, f"{{{NS}}}DatumDo").text = period_to.isoformat()

        obveznik = etree.SubElement(zaglavlje, f"{{{NS}}}Obveznik")
        etree.SubElement(obveznik, f"{{{NS}}}Ime").text = settings.issuer_first_name
        etree.SubElement(obveznik, f"{{{NS}}}Prezime").text = settings.issuer_last_name
        etree.SubElement(obveznik, f"{{{NS}}}OIB").text = settings.issuer_oib
        adresa = etree.SubElement(obveznik, f"{{{NS}}}Adresa")
        etree.SubElement(adresa, f"{{{NS}}}Mjesto").text = settings.issuer_place
        etree.SubElement(adresa, f"{{{NS}}}Ulica").text = settings.issuer_street
        etree.SubElement(adresa, f"{{{NS}}}Broj").text = settings.issuer_street_number or ""

        sastavio = etree.SubElement(zaglavlje, f"{{{NS}}}ObracunSastavio")
        etree.SubElement(sastavio, f"{{{NS}}}Ime").text = preparer.first_name
        etree.SubElement(sastavio, f"{{{NS}}}Prezime").text = preparer.last_name
        etree.SubElement(sastavio, f"{{{NS}}}Email").text = preparer.email

        etree.SubElement(zaglavlje, f"{{{NS}}}Ispostava").text = settings.tax_office_code

        tijelo = etree.SubElement(root, f"{{{NS}}}Tijelo")
        isporuke = etree.SubElement(tijelo, f"{{{NS}}}Isporuke")
        for red_br, line in enumerate(lines, start=1):
            isporuka = etree.SubElement(isporuke, f"{{{NS}}}Isporuka")
            etree.SubElement(isporuka, f"{{{NS}}}RedBr").text = str(red_br)
            etree.SubElement(isporuka, f"{{{NS}}}KodDrzave").text = line.country_code
            etree.SubElement(isporuka, f"{{{NS}}}PDVID").text = line.vat_id
            etree.SubElement(isporuka, f"{{{NS}}}I1").text = _money(line.goods_amount)
            etree.SubElement(isporuka, f"{{{NS}}}I2").text = _money(line.services_amount)

        goods_total, services_total = PDVSLineMapper().totals(lines)
        ukupno = etree.SubElement(tijelo, f"{{{NS}}}IsporukeUkupno")
        etree.SubElement(ukupno, f"{{{NS}}}I1").text = _money(goods_total)
        etree.SubElement(ukupno, f"{{{NS}}}I2").text = _money(services_total)

        return root
