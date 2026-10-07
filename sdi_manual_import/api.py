import json

import frappe
from frappe import _
from frappe.utils import flt

from sdi_manual_import.xml_parser import parse_fatturapa_xml
from sdi_manual_import.metadata_parser import get_data_registrazione, file_root, is_metadata_file


def _save_xml_attachment(doc, xml_content, original_filename=None):
    """Salva l'XML originale come allegato del record, per uso futuro (es. PDF)."""
    _file = frappe.get_doc(
        {
            "doctype": "File",
            "file_name": original_filename or f"{doc.name}.xml",
            "attached_to_doctype": doc.doctype,
            "attached_to_name": doc.name,
            "is_private": True,
            "content": xml_content,
        }
    )
    _file.save(ignore_permissions=True)


@frappe.whitelist()
def upload_supplier_invoice_xml(xml_content, company=None, metadata_content=None, original_filename=None):
    """
    Riceve il contenuto testuale di un XML FatturaPA fornitore (e opzionalmente
    il contenuto del file metadati companion), lo converte in JSON e crea il
    record 'Fattura Fornitori SDI'. Conserva anche l'XML originale come allegato,
    con il nome file originale se fornito.
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

    _save_xml_attachment(doc, xml_content, original_filename)

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


def _get_declared_imponibile(invoice_data):
    """
    Somma l'imponibile dichiarato nel riepilogo IVA dell'XML (dati_riepilogo),
    il valore "ufficiale" secondo il fornitore, da confrontare con la somma
    che ERPNext calcola dalle righe (net_total).
    """
    from italian_invoice.utilities.fatture import get_fattura_body

    body = get_fattura_body(invoice_data)
    if not body:
        return None

    riepilogo = body.get("dati_beni_servizi", {}).get("dati_riepilogo", [])
    try:
        return round(sum(float(r.get("imponibile_importo", 0)) for r in riepilogo), 2)
    except (TypeError, ValueError):
        return None


def _get_tolerance_account(company):
    """
    Risolve il nome esatto del conto '8410093 - DIFFERENZE ARROTONDAMENTO FATTURE' per
    la company, includendo l'abbreviazione (come per gli account/template
    aziendali, il nome reale ha sempre il suffisso "- ABBR").
    """
    abbr = frappe.db.get_value("Company", company, "abbr")
    account_name = f"8410093 - DIFFERENZE ARROTONDAMENTO FATTURE - {abbr}"
    if not frappe.db.exists("Account", account_name):
        frappe.throw(
            _("Account '{0}' non trovato. Crealo nel Piano dei Conti prima di procedere.").format(account_name)
        )
    return account_name


def _inject_rounding_tolerance(pi, invoice_data):
    """
    Confronta l'imponibile dichiarato nell'XML (dati_riepilogo) con la somma
    che ERPNext calcola dalle righe (net_total). Se c'e' uno scarto (tipicamente
    1-2 centesimi, residuo fisiologico anche dopo _fix_prezzo_unitario), lo
    inietta come riga 'Purchase Taxes and Charges' con category:
    - "Valuation and Total" se la fattura movimenta il magazzino (update_stock):
      lo scarto viene spalmato sulla valorizzazione degli articoli, stile SAP PPV
    - "Total" altrimenti: lo scarto va a conto economico, senza toccare il magazzino

    Note di credito: nell'XML gli importi sono positivi, mentre le righe della
    Purchase Invoice di reso hanno quantita' negative (net_total < 0). Il dichiarato
    va quindi portato in negativo, altrimenti lo "scarto" e' il doppio dell'imponibile
    (es. +8,16 dichiarato contro -8,16 delle righe = 16,32 di falso arrotondamento).
    Lo scarto e' calcolato sugli importi della nota di credito stessa: se la fattura
    originaria aveva una riga di arrotondamento, la nota di credito ne ha una speculare.
    """
    declared_imponibile = _get_declared_imponibile(invoice_data)
    if declared_imponibile is None:
        return

    if pi.get("is_return"):
        declared_imponibile = -abs(declared_imponibile)

    diff = round(declared_imponibile - flt(pi.net_total), 2)
    if diff == 0:
        return

    account = _get_tolerance_account(pi.company)
    category = "Valuation and Total" if pi.get("update_stock") else "Total"

    pi.append("taxes", {
        "charge_type": "Actual",
        "account_head": account,
        "tax_amount": diff,
        "category": category,
        "add_deduct_tax": "Add",
        "description": _("Differenza di arrotondamento SDI vs somma righe ({0})").format(diff),
    })


# ---------------------------------------------------------------------------
# Elementi della testata XML che non sono righe articolo: cassa previdenziale,
# bollo, ritenuta d'acconto. Conti e articolo sono configurabili qui.
# ---------------------------------------------------------------------------

# Il conto si cerca per numero (campo "Account Number" del piano dei conti) nella
# company della fattura, quindi vale per qualunque abbreviazione.
CONTO_BOLLO = "8405005"       # 8405005 - IMPOSTA DI BOLLO
CONTO_RITENUTA = "4805085"    # 4805085 - ERARIO C/RIT. LAVORO AUTONOMO
ITEM_CASSA = "Contributo Cassa Previdenziale"


def _as_list(value):
    if not value:
        return []
    return value if isinstance(value, list) else [value]


def _get_dati_generali_documento(invoice_data):
    from italian_invoice.utilities.fatture import get_fattura_body

    body = get_fattura_body(invoice_data)
    if not body:
        return {}
    return body.get("dati_generali", {}).get("dati_generali_documento", {}) or {}


def _get_riepilogo(invoice_data):
    from italian_invoice.utilities.fatture import get_fattura_body

    body = get_fattura_body(invoice_data)
    if not body:
        return []
    return _as_list(body.get("dati_beni_servizi", {}).get("dati_riepilogo"))


def _segno(pi):
    """-1 sulle note di credito: righe e tasse della PI di reso sono negative."""
    return -1 if pi.get("is_return") else 1


def _get_account_by_number(company, number):
    account = frappe.db.get_value(
        "Account", {"company": company, "account_number": number, "is_group": 0}, "name"
    )
    if not account:
        account = frappe.db.get_value(
            "Account",
            {"company": company, "name": ["like", f"{number} - %"], "is_group": 0},
            "name",
        )
    return account


def _get_or_create_item(item_code, item_name):
    """Articolo di servizio per le righe che non hanno un articolo in anagrafica
    (contributo cassa). Se non si riesce a crearlo ripiega sull'articolo generico
    gia' usato dall'importer."""
    if frappe.db.exists("Item", item_code):
        return item_code

    try:
        item_group = (
            frappe.db.get_single_value("Stock Settings", "item_group")
            or frappe.db.get_value("Item Group", {"is_group": 0}, "name")
        )
        item = frappe.get_doc({
            "doctype": "Item",
            "item_code": item_code,
            "item_name": item_name,
            "item_group": item_group,
            "stock_uom": frappe.db.get_single_value("Stock Settings", "stock_uom") or "Nos",
            "is_stock_item": 0,
        })
        item.insert(ignore_permissions=True, ignore_mandatory=True)
        return item.name
    except Exception:
        frappe.log_error(frappe.get_traceback(), "SDI import: creazione articolo cassa")
        from italian_invoice.utilities.fatture_passive import get_default_item_code

        return get_default_item_code()


def _remove_empty_group_tax_rows(pi):
    """prepare_invoice_taxes crea, per le righe con aliquota 0 e natura, una riga
    tassa a importo 0 sul conto IVA con tax_rate 0 - che nel piano dei conti e' il
    conto padre 'CREDITI TRIBUTARI' (gruppo). La natura e' gia' sulle righe
    articolo e nel riepilogo XML: la riga vuota non serve e punta a un conto che
    non e' movimentabile."""
    da_togliere = [
        t for t in pi.get("taxes") or []
        if not flt(t.tax_amount)
        and t.account_head
        and frappe.db.get_value("Account", t.account_head, "is_group")
    ]
    for riga in da_togliere:
        pi.remove(riga)
    for idx, riga in enumerate(pi.get("taxes") or [], 1):
        riga.idx = idx


def _add_cassa_items(pi, invoice_data):
    """Il contributo della cassa previdenziale (DatiCassaPrevidenziale) non e' una
    riga articolo, ma e' compreso nell'imponibile del riepilogo IVA. Senza questa
    riga lo scarto finiva nella 'differenza di arrotondamento' (es. 40,00 su 1.000
    di imponibile con cassa al 4%) e il costo non risultava per natura.
    Si aggiunge come articolo di servizio, con l'aliquota/natura indicate per la cassa."""
    dati = _get_dati_generali_documento(invoice_data)
    aggiunte = False

    for cassa in _as_list(dati.get("dati_cassa_previdenziale")):
        importo = flt(cassa.get("importo_contributo_cassa"))
        if not importo:
            continue

        tipo = cassa.get("tipo_cassa") or ""
        percentuale = cassa.get("al_cassa")
        descrizione = f"Contributo cassa previdenziale {tipo}".strip()
        if percentuale:
            descrizione += f" ({percentuale}%)"

        item_code = _get_or_create_item(ITEM_CASSA, "Contributo cassa previdenziale")
        uom = frappe.db.get_value("Item", item_code, "stock_uom") or "Nos"
        qty = _segno(pi)

        pi.append("items", {
            "item_code": item_code,
            "item_name": frappe.db.get_value("Item", item_code, "item_name") or item_code,
            "description": descrizione,
            "qty": qty,
            "rate": abs(importo),
            "price_list_rate": abs(importo),
            "uom": uom,
            "stock_uom": uom,
            "conversion_factor": 1,
            "tax_rate": flt(cassa.get("aliquota_iva")),
            "custom_motivo_esenzione_iva": cassa.get("natura") or None,
        })
        aggiunte = True

    return aggiunte


def _add_bollo_e_ritenuta(pi, invoice_data):
    """Bollo e ritenuta d'acconto dalla testata XML, come righe 'Purchase Taxes and
    Charges' di tipo Actual (descrizione che inizia per 'Bollo' / 'Ritenuta':
    registri_iva le riconosce e non le tratta come IVA).

    - Bollo: DatiBollo e' solo un'indicazione. Se il fornitore lo addebita come riga
      (rivalsa, di solito natura N1) e' gia' tra gli articoli; se lo addebita solo in
      testata e' compreso in ImportoTotaleDocumento ma non nelle righe. Per non
      contarlo due volte si aggiunge solo se la differenza tra il totale XML e il
      totale della PI e' esattamente l'importo del bollo.
    - Ritenuta: riga 'Deduct' sul conto erario c/ritenute. Riduce il dovuto al
      fornitore, non e' compresa in ImportoTotaleDocumento. Non usare in
      contemporanea 'Apply Tax Withholding Amount' di ERPNext sulla stessa fattura
      (raddoppierebbe la ritenuta)."""
    dati = _get_dati_generali_documento(invoice_data)
    segno = _segno(pi)

    # --- Bollo ---
    bollo = flt((dati.get("dati_bollo") or {}).get("importo_bollo"))
    totale_xml = flt(dati.get("importo_totale_documento"))
    if bollo and totale_xml:
        totale_atteso = segno * abs(totale_xml)
        mancante = round(totale_atteso - flt(pi.grand_total), 2)
        if abs(mancante - segno * bollo) < 0.005:
            conto = _get_account_by_number(pi.company, CONTO_BOLLO)
            if conto:
                pi.append("taxes", {
                    "charge_type": "Actual",
                    "account_head": conto,
                    "tax_amount": segno * bollo,
                    "category": "Total",
                    "add_deduct_tax": "Add",
                    "description": "Bollo (DatiBollo XML)",  # non tradurre: vedi RIGHE_NON_IVA_RE
                })
            else:
                frappe.msgprint(
                    _("Bollo di {0} presente nell'XML ma conto {1} non trovato: riga non aggiunta").format(
                        bollo, CONTO_BOLLO
                    ),
                    alert=True,
                    indicator="orange",
                )

    # --- Ritenuta d'acconto ---
    ritenute = _as_list(dati.get("dati_ritenuta"))
    totale_ritenuta = sum(flt(r.get("importo_ritenuta")) for r in ritenute)
    if totale_ritenuta:
        conto = _get_account_by_number(pi.company, CONTO_RITENUTA)
        if conto:
            prima = ritenute[0]
            # La descrizione deve iniziare per "Ritenuta": registri_iva la usa per
            # riconoscere le righe che non sono IVA (non tradurre questa stringa)
            parti = ["Ritenuta d'acconto", prima.get("tipo_ritenuta")]
            if prima.get("aliquota_ritenuta"):
                parti.append(f"{prima['aliquota_ritenuta']}%")
            descrizione = " ".join(p for p in parti if p)
            if prima.get("causale_pagamento"):
                descrizione += f" (causale {prima['causale_pagamento']})"
            pi.append("taxes", {
                "charge_type": "Actual",
                "account_head": conto,
                "tax_amount": segno * totale_ritenuta,
                "category": "Total",
                "add_deduct_tax": "Deduct",
                "description": descrizione,
            })
            # La ritenuta e' gia' in fattura: evita che ERPNext ne aggiunga una seconda
            # se il fornitore ha una Tax Withholding Category
            pi.apply_tds = 0
        else:
            frappe.msgprint(
                _("Ritenuta di {0} presente nell'XML ma conto {1} non trovato: riga non aggiunta").format(
                    totale_ritenuta, CONTO_RITENUTA
                ),
                alert=True,
                indicator="orange",
            )


def _set_td16_se_reverse_charge(pi, invoice_data):
    """Fattura con natura N6.x (inversione contabile interna): la PI va integrata e
    registrata anche nelle vendite. Si imposta TD16 sul tipo documento, cosi' alla
    submit registri_iva genera il Documento Integrativo (se esiste un Sezionale IVA
    di vendita auto-generato con TD16 tra i tipi documento che lo attivano).

    Richiede che TD16 sia classificato 'AutoFattura' in 'Tipologia di documento
    e-Invoice' (il campo Tipo Documento della PI accetta solo quelle)."""
    if not any((r.get("natura") or "").upper().startswith("N6") for r in _get_riepilogo(invoice_data)):
        return

    tipologia = frappe.db.get_value("Tipologia di documento e-Invoice", "TD16", "tipologia")
    if tipologia != "AutoFattura":
        frappe.msgprint(
            _(
                "Fattura in inversione contabile (N6): impostare a mano il tipo documento TD16. "
                "TD16 deve prima essere classificato 'AutoFattura' in Tipologia di documento e-Invoice."
            ),
            alert=True,
            indicator="orange",
        )
        return

    pi.custom_tipo_di_documento = "TD16"


@frappe.whitelist()
def process_supplier_invoice_fixed(
    invoice_data, fattura_fornitori_sdi=None, item_mappings=None, remember_mappings=None
):
    """
    Wrapper attorno a fatture_passive.process_supplier_invoice che:
    1. Corregge prezzo_unitario = prezzo_totale / quantita per ogni riga
    2. Ripristina il calcolo normale del Rounding Adjustment
    3. Usa Data Registrazione (SDI) come Posting Date, se disponibile
    4. Usa DataScadenzaPagamento come Due Date, se presente nell'XML
    5. Ripristina il rate sulle righe tasse IVA Actual
    6. Aggiunge il contributo cassa previdenziale come articolo (e' compreso
       nell'imponibile del riepilogo IVA)
    7. Inietta lo scarto di arrotondamento residuo (imponibile XML vs somma
       righe ERPNext) come riga di tolleranza, spalmata sul magazzino o a
       conto economico a seconda che la fattura movimenti stock. Sulle note di
       credito il dichiarato XML e' portato in negativo.
    8. Aggiunge bollo (se addebitato in testata) e ritenuta d'acconto
    9. Elimina le righe tassa vuote su conti padre (es. CREDITI TRIBUTARI)
    10. Imposta TD16 sulle fatture in inversione contabile interna (natura N6)
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
            pi.set_posting_time = 1

    _remove_empty_group_tax_rows(pi)

    # La cassa entra nel net_total PRIMA del confronto con l'imponibile dichiarato
    if _add_cassa_items(pi, invoice_data):
        pi.calculate_taxes_and_totals()

    _inject_rounding_tolerance(pi, invoice_data)

    pi.disable_rounded_total = 0
    pi.calculate_taxes_and_totals()

    # Bollo e ritenuta si valutano sul totale gia' quadrato con l'XML
    _add_bollo_e_ritenuta(pi, invoice_data)
    pi.calculate_taxes_and_totals()

    _set_td16_se_reverse_charge(pi, invoice_data)

    pi.save(ignore_permissions=True)

    import re

    tax_rows = frappe.get_all(
        "Purchase Taxes and Charges",
        filters={"parent": pi.name, "parenttype": "Purchase Invoice"},
        fields=["name", "rate", "description"],
    )
    for row in tax_rows:
        # Solo le righe IVA: "Ritenuta d'acconto 20%" non deve diventare una riga con rate 20
        if not row.rate and (row.description or "").upper().startswith("IVA"):
            match = re.search(r"(\d+(?:\.\d+)?)\s*%", row.description or "")
            if match:
                frappe.db.set_value(
                    "Purchase Taxes and Charges", row.name, "rate", float(match.group(1))
                )

    due_date = _extract_due_date(invoice_data)
    if due_date:
        frappe.db.set_value("Purchase Invoice", pi.name, "due_date", due_date)

    frappe.db.commit()

    return pi_name


def _extract_due_date(invoice_data):
    """
    Estrae la data di scadenza pagamento (DataScadenzaPagamento) dal JSON.
    Se sono presenti piu' rate, usa la scadenza piu' tardiva.
    """
    import datetime

    from italian_invoice.utilities.fatture import get_fattura_body

    body = get_fattura_body(invoice_data)
    if not body:
        return None

    dati_pagamento = body.get("dati_pagamento") or []
    if isinstance(dati_pagamento, dict):
        dati_pagamento = [dati_pagamento]

    scadenze = []
    for dp in dati_pagamento:
        dettagli = dp.get("dettaglio_pagamento") or []
        if isinstance(dettagli, dict):
            dettagli = [dettagli]
        for d in dettagli:
            scadenza = d.get("data_scadenza_pagamento")
            if scadenza:
                try:
                    scadenze.append(datetime.date.fromisoformat(scadenza))
                except (ValueError, TypeError):
                    continue

    return max(scadenze) if scadenze else None


@frappe.whitelist()
def import_from_folder(company=None):
    """
    Scansiona 'sdi_passive_incoming' e importa ogni fattura XML trovata.
    Abbina automaticamente ogni fattura al suo file metadati companion
    confrontando la "radice" del nome file (parte prima del primo punto).
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
                xml_content,
                company=company,
                metadata_content=metadata_content,
                original_filename=filename,
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
