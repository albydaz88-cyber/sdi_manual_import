import json

import frappe
from frappe import _

from sdi_manual_import.xml_parser import parse_fatturapa_xml


@frappe.whitelist()
def upload_supplier_invoice_xml(xml_content, company=None):
    """
    Riceve il contenuto testuale di un XML FatturaPA fornitore,
    lo converte in JSON e crea il record 'Fattura Fornitori SDI'.
    """
    if not company:
        company = frappe.defaults.get_user_default("Company")
        if not company:
            frappe.throw(_("Nessuna Company di default impostata per l'utente"))

    try:
        invoice_json = parse_fatturapa_xml(xml_content)
    except ValueError as e:
        frappe.throw(_("XML non valido: {0}").format(str(e)))

    from italian_invoice.utilities.fatture import (
        get_cedente_prestatore_from_json,
        get_invoice_number_from_json,
        get_supplier_vat_from_json,
    )

    supplier_vat = get_supplier_vat_from_json(invoice_json)
    cedente = get_cedente_prestatore_from_json(invoice_json)

    anagrafica = cedente.get("dati_anagrafici", {}).get("anagrafica", {}) or {}
    denominazione = anagrafica.get("denominazione") or anagrafica.get("nome")

    numero_fattura = get_invoice_number_from_json(invoice_json)

    existing = frappe.db.exists(
        "Fattura Fornitori SDI",
        {"partita_iva_fornitore": supplier_vat, "numero_fattura": numero_fattura},
    )
    if existing:
        frappe.throw(
            _("Questa fattura risulta già importata: {0}").format(existing)
        )

    doc = frappe.get_doc(
        {
            "doctype": "Fattura Fornitori SDI",
            "dati_fattura": json.dumps(invoice_json),
            "partita_iva_fornitore": supplier_vat,
            "denominazione_fornitore": denominazione,
            "company": company,
            "via_webhook": 1,
        }
    )
    doc.insert(ignore_permissions=True)
    frappe.db.commit()

    return doc.name


@frappe.whitelist()
def get_or_create_supplier(supplier_vat_id, invoice_data):
    """
    Wrapper whitelisted per italian_invoice.utilities.fatture_passive.get_or_create_supplier,
    che non è esposta come endpoint HTTP nell'app originale.
    """
    if isinstance(invoice_data, str):
        invoice_data = json.loads(invoice_data)

    from italian_invoice.utilities.fatture_passive import get_or_create_supplier as _get_or_create_supplier

    return _get_or_create_supplier(supplier_vat_id, invoice_data)


def _fix_prezzo_unitario(invoice_data):
    """
    Corregge prezzo_unitario = prezzo_totale / quantita per ogni riga.
    PrezzoUnitario nel FatturaPA puo' avere fino a 8 decimali e non sempre
    e' coerente con PrezzoTotale (arrotondamenti residui del fornitore),
    generando rumore decimale nel totale finale della Purchase Invoice.
    """
    from italian_invoice.utilities.fatture import get_fattura_body

    body = get_fattura_body(invoice_data)
    if not body:
        return

    linee = body.get("dati_beni_servizi", {}).get("dettaglio_linee", [])
    for riga in linee:
        try:
            quantita = float(riga.get("quantita") or 0)
            prezzo_totale = float(riga.get("prezzo_totale") or 0)
            if quantita:
                riga["prezzo_unitario"] = prezzo_totale / quantita
        except (TypeError, ValueError):
            continue


@frappe.whitelist()
def process_supplier_invoice_fixed(
    invoice_data, fattura_fornitori_sdi=None, item_mappings=None, remember_mappings=None
):
    """
    Wrapper attorno a italian_invoice.utilities.fatture_passive.process_supplier_invoice
    che:
    1. Corregge prezzo_unitario = prezzo_totale / quantita per ogni riga (vedi _fix_prezzo_unitario)
    2. Ripristina il calcolo normale del Rounding Adjustment (disable_rounded_total = 0),
       uniformando le fatture importate a quelle create manualmente da UI.
    """
    if isinstance(invoice_data, str):
        invoice_data = json.loads(invoice_data)

    _fix_prezzo_unitario(invoice_data)

    from italian_invoice.utilities.fatture_passive import (
        process_supplier_invoice as _process_supplier_invoice,
    )

    pi_name = _process_supplier_invoice(
        invoice_data,
        fattura_fornitori_sdi=fattura_fornitori_sdi,
        item_mappings=item_mappings,
        remember_mappings=remember_mappings,
    )

    pi = frappe.get_doc("Purchase Invoice", pi_name)
    pi.disable_rounded_total = 0
    pi.calculate_taxes_and_totals()
    pi.save(ignore_permissions=True)
    frappe.db.commit()

    return pi_name
