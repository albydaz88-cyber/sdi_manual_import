"""
Parser per il file XML dei metadati fattura scaricabile da Fatture e Corrispettivi
(Agenzia delle Entrate) tramite "Consultazioni e Download Massivi", companion
del file XML della fattura elettronica.
"""
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo

NAMESPACE = {"m": "urn:xml.fatturazione.sogei.it"}

SITE_TIMEZONE = "Europe/Rome"


def file_root(filename):
    """
    Isola la parte del nome file prima del primo punto, che identifica
    univocamente la transazione SDI a cui appartiene sia il file fattura
    che il suo file metadati companion, indipendentemente da estensioni
    (.xml, .XML, .xml.p7m) e maiuscole/minuscole del nome successivo.

    Esempi:
    'IT02663950984_AKSPL.xml' -> 'IT02663950984_AKSPL'
    'IT02663950984_AKSPL.xml.p7m_metaDato' -> 'IT02663950984_AKSPL'
    'IT03278040245_72RKA.XML_metaDato' -> 'IT03278040245_72RKA'
    'IT03278040245_72RKA' (senza punti) -> 'IT03278040245_72RKA'
    """
    idx = filename.find(".")
    return filename[:idx] if idx != -1 else filename


def is_metadata_file(filename):
    """I file metadati terminano sempre con '_metaDato' (case-insensitive)."""
    return filename.lower().endswith("_metadato")


def _extract_field(xml_content, field_name):
    root = ET.fromstring(xml_content)
    for metadato in root.findall("m:metadato", NAMESPACE):
        nome = metadato.find("m:nome", NAMESPACE)
        valore = metadato.find("m:valore", NAMESPACE)
        if nome is not None and nome.text == field_name:
            return valore.text if valore is not None else None
    return None


def _parse_datetime(value):
    """Converte una data ISO con offset (es. '2025-01-26T04:17:12.000+01:00')
    in datetime naive nel fuso orario del sito, pronta per un campo Datetime di Frappe."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None

    if dt.tzinfo is not None:
        dt = dt.astimezone(ZoneInfo(SITE_TIMEZONE)).replace(tzinfo=None)

    return dt


def get_data_registrazione(metadata_xml_content):
    """
    Estrae la Data Registrazione (campo 'dataregistrazione') dal file
    metadati: la data in cui lo SDI registra ufficialmente il documento nel
    proprio sistema informativo, strettamente legata alla data di ricezione
    fiscale ai fini della detraibilita' IVA. None se non trovata/non valida.
    """
    raw_value = _extract_field(metadata_xml_content, "dataregistrazione")
    return _parse_datetime(raw_value)
