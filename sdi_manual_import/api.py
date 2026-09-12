import json

import frappe
from frappe import _

from sdi_manual_import.xml_parser import parse_fatturapa_xml
from sdi_manual_import.metadata_parser import get_data_registrazione, file_root, is_metadata_file


def _save_xml_attachment(doc, xml_content):
    """Salva l'XML originale come allegato del record, per uso futuro (es. PDF)."""
    _file = frappe.get_doc(
        {
            "doctype": "File",
            "file_name": f"{doc.name}.xml",
            "attached_to_doctype": doc.doctype,
            "attached_to_name": doc.name,
            "is_private": True,
            "content": xml_content,
        }
    )
    _file.save(ignore_permissions=True)


@frappe.whitelist()
def upload_supplier_invoice_xml(xml_content, company=None, metadata_content=None):
    """
    Riceve il contenuto testuale di un XML FatturaPA fornitore (e opzionalmente
    il contenuto del file metadati companion), lo converte in JSON e crea il
    record 'Fattura Fornitori SDI'. Conserva anche l'XML originale come allegato.
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

    data_registrazione = None
    if metadata_content:
        try:
            data_registrazione = get_data_registrazione(metadata_content)
        except Exception:
            data_registrazione = None

    doc = frappe.get_doc(
        {
            "doctype": "Fattura Fornitori SDI",
            "dati_fattura": json.dumps(invoice_json),
            "partita_iva_fornitore": supplier_vat,
            "denominazione_fornitore": denominazione,
            "company": company,
            "via_webhook": 1,
            "custom_data_ricezione": data_registrazione,
        }
    )
    doc.insert(ignore_permissions=True)

    _save_xml_attachment(doc, xml_content)

    frappe.db.commit()

    return doc.name


@frappe.whitelist()
def get_or_create_supplier(supplier_vat_id, invoice_data):
    """Wrapper whitelisted per fatture_passive.get_or_create_supplier."""
    if isinstance(invoice_data, str):
        invoice_data = json.loads(invoice_data)

    from italian_invoice.utilities.fatture_passive import get_or_create_supplier as _get_or_create_supplier

    return _get_or_create_supplier(supplier_vat_id, invoice_data)


def _fix_prezzo_unitario(invoice_data):
    """Corregge prezzo_unitario = prezzo_totale / quantita per ogni riga."""
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
    Wrapper attorno a fatture_passive.process_supplier_invoice che:
    1. Corregge prezzo_unitario = prezzo_totale / quantita per ogni riga
    2. Ripristina il calcolo normale del Rounding Adjustment
    3. Usa Data Registrazione (SDI) come Posting Date, se disponibile
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

    if fattura_fornitori_sdi:
        data_registrazione = frappe.db.get_value(
            "Fattura Fornitori SDI", fattura_fornitori_sdi, "custom_data_ricezione"
        )
        if data_registrazione:
            posting_date = (
                data_registrazione.date()
                if hasattr(data_registrazione, "date")
                else data_registrazione
            )
            pi.posting_date = posting_date

    pi.disable_rounded_total = 0
    pi.calculate_taxes_and_totals()
    pi.save(ignore_permissions=True)
    frappe.db.commit()

    return pi_name


@frappe.whitelist()
def import_from_folder(company=None):
    """
    Scansiona 'sdi_passive_incoming' e importa ogni fattura XML trovata.
    Abbina automaticamente ogni fattura al suo file metadati companion
    confrontando la "radice" del nome file (parte prima del primo punto),
    che identifica univocamente la transazione SDI indipendentemente da
    estensioni/maiuscole (vedi sdi_manual_import.metadata_parser.file_root).
    """
    import os
    import shutil

    if not company:
        company = frappe.defaults.get_user_default("Company")
        if not company:
            frappe.throw(_("Nessuna Company di default impostata per l'utente"))

    base = frappe.utils.get_site_path("private", "files")
    incoming_dir = os.path.join(base, "sdi_passive_incoming")
    processed_dir = os.path.join(base, "sdi_passive_processati")
    error_dir = os.path.join(base, "sdi_passive_errori")

    for d in (incoming_dir, processed_dir, error_dir):
        if not os.path.exists(d):
            os.makedirs(d)

    all_files = sorted(os.listdir(incoming_dir))

    junk = {".ds_store", "thumbs.db", "desktop.ini"}
    all_files = [f for f in all_files if f.lower() not in junk and not f.startswith(".")]

    metadata_files = [f for f in all_files if is_metadata_file(f)]
    invoice_files = [f for f in all_files if f not in metadata_files]

    metadata_by_root = {file_root(mf): mf for mf in metadata_files}

    results = []
    for filename in invoice_files:
        filepath = os.path.join(incoming_dir, filename)
        root = file_root(filename)
        metadata_filename = metadata_by_root.get(root)
        has_metadata = metadata_filename is not None
        metadata_filepath = os.path.join(incoming_dir, metadata_filename) if has_metadata else None

        try:
            with open(filepath, encoding="utf-8") as f:
                xml_content = f.read()

            metadata_content = None
            if has_metadata:
                with open(metadata_filepath, encoding="utf-8") as f:
                    metadata_content = f.read()

            doc_name = upload_supplier_invoice_xml(
                xml_content, company=company, metadata_content=metadata_content
            )

            shutil.move(filepath, os.path.join(processed_dir, filename))
            if has_metadata:
                shutil.move(metadata_filepath, os.path.join(processed_dir, metadata_filename))

            results.append({
                "file": filename,
                "status": "success",
                "doc": doc_name,
                "metadata_found": has_metadata,
            })

        except Exception as e:
            frappe.db.rollback()
            message = str(e)
            is_duplicate = "già importata" in message

            dest_dir = processed_dir if is_duplicate else error_dir
            shutil.move(filepath, os.path.join(dest_dir, filename))
            if has_metadata and os.path.exists(metadata_filepath):
                shutil.move(metadata_filepath, os.path.join(dest_dir, metadata_filename))

            results.append({
                "file": filename,
                "status": "duplicate" if is_duplicate else "error",
                "message": message,
            })

    return results


@frappe.whitelist()
def download_pdf(docname):
    """
    Genera un PDF a partire dall'XML originale allegato al record,
    usando il Foglio di Stile AssoSoftware. Formato A4.
    """
    doc = frappe.get_doc("Fattura Fornitori SDI", docname)
    frappe.has_permission(doc=doc, throw=True)

    files = frappe.get_all(
        "File",
        filters={
            "attached_to_doctype": "Fattura Fornitori SDI",
            "attached_to_name": docname,
            "file_name": ["like", "%.xml"],
        },
        fields=["name"],
        limit=1,
    )
    if not files:
        frappe.throw(_("XML originale non trovato per questa fattura"))

    file_doc = frappe.get_doc("File", files[0].name)
    xml_bytes = file_doc.get_content()
    if isinstance(xml_bytes, str):
        xml_bytes = xml_bytes.encode("utf-8")

    from lxml import etree

    xslt_path = frappe.get_app_path("sdi_manual_import", "xsl", "fogliostileassosoftware.xsl")

    parser = etree.XMLParser(recover=True)
    xml_doc = etree.fromstring(xml_bytes, parser=parser)
    xslt_doc = etree.parse(xslt_path)
    transform = etree.XSLT(xslt_doc)
    html_result = transform(xml_doc)
    html_str = str(html_result)

    from frappe.utils.pdf import get_pdf

    pdf_options = {
        "page-size": "A4",
        "margin-top": "5mm",
        "margin-bottom": "5mm",
        "margin-left": "5mm",
        "margin-right": "5mm",
        "zoom": "0.90",
        "enable-local-file-access": None,
    }

    pdf_content = get_pdf(html_str, options=pdf_options)

    frappe.local.response.filename = f"{docname}.pdf"
    frappe.local.response.filecontent = pdf_content
    frappe.local.response.type = "download"
