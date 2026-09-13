from __future__ import annotations

from decimal import Decimal

from lxml import etree

NS = "http://www.apis-it.hr/fin/2012/types/F73"
NSMAP = {"tns": NS}


def _amount(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):.2f}"


def _rate(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):.2f}"


def recipient_oib_for_f1(document_number: str) -> str:
    """CIS OibPrimateljaRacuna: domestic 11-digit OIB only."""
    digits = "".join(ch for ch in (document_number or "") if ch.isdigit())
    return digits if len(digits) == 11 else ""


def build_racun_xml(
    *,
    oib: str,
    issued_at_iso: str,
    sequence_number: int,
    vat_rate: Decimal,
    vat_base: Decimal,
    vat_amount: Decimal,
    total: Decimal,
    payment_code: str,
    operator_oib: str,
    zki: str,
    business_premise_code: str,
    payment_device_code: str,
    message_id: str,
    message_at_iso: str,
    in_vat_system: bool = True,
    recipient_oib: str = "",
    subsequent_delivery: bool = False,
) -> etree._Element:
    root = etree.Element(f"{{{NS}}}RacunZahtjev", nsmap=NSMAP)
    root.set("Id", "racun")

    zaglavlje = etree.SubElement(root, f"{{{NS}}}Zaglavlje")
    etree.SubElement(zaglavlje, f"{{{NS}}}IdPoruke").text = message_id
    etree.SubElement(zaglavlje, f"{{{NS}}}DatumVrijeme").text = message_at_iso

    racun = etree.SubElement(root, f"{{{NS}}}Racun")
    etree.SubElement(racun, f"{{{NS}}}Oib").text = oib
    etree.SubElement(racun, f"{{{NS}}}USustPdv").text = "true" if in_vat_system else "false"
    etree.SubElement(racun, f"{{{NS}}}DatVrijeme").text = issued_at_iso
    etree.SubElement(racun, f"{{{NS}}}OznSlijed").text = "P"

    br_rac = etree.SubElement(racun, f"{{{NS}}}BrRac")
    etree.SubElement(br_rac, f"{{{NS}}}BrOznRac").text = str(sequence_number)
    etree.SubElement(br_rac, f"{{{NS}}}OznPosPr").text = business_premise_code
    etree.SubElement(br_rac, f"{{{NS}}}OznNapUr").text = payment_device_code

    if in_vat_system:
        pdv = etree.SubElement(racun, f"{{{NS}}}Pdv")
        porez = etree.SubElement(pdv, f"{{{NS}}}Porez")
        etree.SubElement(porez, f"{{{NS}}}Stopa").text = _rate(vat_rate)
        etree.SubElement(porez, f"{{{NS}}}Osnovica").text = _amount(vat_base)
        etree.SubElement(porez, f"{{{NS}}}Iznos").text = _amount(vat_amount)

    etree.SubElement(racun, f"{{{NS}}}IznosUkupno").text = _amount(total)
    etree.SubElement(racun, f"{{{NS}}}NacinPlac").text = payment_code
    etree.SubElement(racun, f"{{{NS}}}OibOper").text = operator_oib
    etree.SubElement(racun, f"{{{NS}}}ZastKod").text = zki
    etree.SubElement(racun, f"{{{NS}}}NakDost").text = (
        "true" if subsequent_delivery else "false"
    )
    oib_primatelja = recipient_oib_for_f1(recipient_oib)
    if oib_primatelja:
        etree.SubElement(racun, f"{{{NS}}}OibPrimateljaRacuna").text = oib_primatelja
    return root


def parse_jir_from_response(xml_text: str) -> str:
    root = etree.fromstring(xml_text.encode("utf-8"))
    for elem in root.iter():
        if elem.tag.endswith("Jir") and elem.text:
            return elem.text.strip()
    raise ValueError("JIR not found in fiscalization response")
