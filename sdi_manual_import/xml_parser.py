"""
Convertitore generico XML FatturaPA → JSON, nel formato snake_case
già usato da italian_invoice (fattura_elettronica_header, cedente_prestatore, ecc.)
"""
import re
import xmltodict

# Tag che nel FatturaPA possono ripetersi: forziamo sempre la lista,
# anche quando c'è una sola occorrenza, per compatibilità con italian_invoice
# che itera su queste chiavi assumendole liste.
FORCE_LIST_TAGS = (
    "DettaglioLinee",
    "DatiRiepilogo",
    "FatturaElettronicaBody",
    "CodiceArticolo",
    "DatiPagamento",
    "DettaglioPagamento",
    "AltriDatiGestionali",
    "Allegati",
)


def _camel_to_snake(name: str) -> str:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    return name.lower()


def _strip_namespace(tag: str) -> str:
    """Rimuove eventuale prefisso namespace, es. 'p:FatturaElettronica' -> 'FatturaElettronica'"""
    return tag.split(":")[-1]


def _convert(obj):
    if isinstance(obj, dict):
        result = {}
        for key, value in obj.items():
            # Ignoriamo attributi XML (@versione, @xmlns...) e nodi di solo testo misto
            if key.startswith("@") or key == "#text":
                continue
            clean_key = _camel_to_snake(_strip_namespace(key))
            result[clean_key] = _convert(value)
        return result
    elif isinstance(obj, list):
        return [_convert(item) for item in obj]
    else:
        return obj


def parse_fatturapa_xml(xml_content: str) -> dict:
    """
    Converte un XML FatturaPA (fattura fornitore ricevuta) nel dict
    con le chiavi snake_case attese da italian_invoice.utilities.fatture

    Solleva ValueError se l'XML non sembra un FatturaPA valido.
    """
    raw = xmltodict.parse(xml_content, force_list=FORCE_LIST_TAGS)

    if not raw:
        raise ValueError("XML vuoto o non parsabile")

    root_key = next(iter(raw))
    root = raw[root_key]
    converted = _convert(root)

    if "fattura_elettronica_header" not in converted:
        raise ValueError(
            "Il file non contiene FatturaElettronicaHeader: non è un FatturaPA valido"
        )

    return converted
