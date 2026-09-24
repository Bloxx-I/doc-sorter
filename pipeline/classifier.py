"""One LM Studio call extracts metadata used for naming and folder suggestions."""

import json
import re
from datetime import date, datetime
from urllib import error, request


def safe_part(value, fallback="Unbekannt"):
    value = re.sub(r"[^\w-]+", "_", str(value or ""), flags=re.UNICODE).strip("_-")
    return value[:80] or fallback


def make_filename(data):
    try:
        day = date.fromisoformat(str(data.get("date", "")))
    except ValueError:
        day = None
    stamp = day.strftime("%Y_%m_%d") if day else "Datum_unbekannt"
    sender = safe_part(data.get("sender"))
    kind, keyword = str(data.get("type") or "").strip(), str(data.get("keyword") or "").strip()
    if kind and keyword and kind.casefold() not in keyword.casefold():
        keyword = f"{kind}-{keyword}"
    subject = safe_part(keyword or kind, "Dokument")
    return f"{stamp}_{sender}_{subject}.pdf"


MONTHS = {"januar": 1, "jan": 1, "februar": 2, "feb": 2, "märz": 3, "maerz": 3, "mär": 3, "april": 4, "apr": 4,
          "mai": 5, "juni": 6, "jun": 6, "juli": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9,
          "sept": 9, "oktober": 10, "okt": 10, "november": 11, "nov": 11, "dezember": 12, "dez": 12,
          "january": 1, "february": 2, "march": 3, "mar": 3, "may": 5, "june": 6, "july": 7, "october": 10,
          "oct": 10, "december": 12, "dec": 12}
_MONTH_NAMES = "|".join(sorted(MONTHS, key=len, reverse=True))
DATE = (r"(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4}|\d{2})\b"                       # 12.09.2026, 12.9.26
        r"|(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b"                                # 2026-09-12, 2026/09/12
        r"|(\d{1,2})\.?\s+(" + _MONTH_NAMES + r")\.?,?\s+(\d{4})"                # 12. September 2026, 12 Sep 2026
        r"|\b(" + _MONTH_NAMES + r")\.?\s+(\d{1,2}),?\s+(\d{4})")                # September 12, 2026
COMPACT = r"(?<![\w-])((?:19|20)\d{2})(\d{2})(\d{2})(?![\w-])"                   # 20260723 (only as last resort)


def plausible(day):
    """Reject model inventions like 0001-01-01 or far-future dates."""
    return day is not None and 1990 <= day.year <= date.today().year + 1


def _parse(match):
    try:
        if match.group(1):
            year = int(match.group(3))
            year += 2000 if year < 100 else 0
            day = date(year, int(match.group(2)), int(match.group(1)))
        elif match.group(4):
            day = date(int(match.group(4)), int(match.group(5)), int(match.group(6)))
        elif match.group(7):
            day = date(int(match.group(9)), MONTHS[match.group(8).lower()], int(match.group(7)))
        else:
            day = date(int(match.group(12)), MONTHS[match.group(10).lower()], int(match.group(11)))
    except (ValueError, KeyError):
        return None
    return day if plausible(day) else None


def explicit_date(text, document_type):
    """A date right after a label like 'Rechnungsdatum:' beats anything the model says."""
    invoice = re.search(r"rechnung|invoice", str(document_type or ""), re.I)
    labels = (["Rechnungsdatum", "Invoice Date", "Ausstellungsdatum", "Belegdatum", "Datum", "Date"] if invoice
              else ["Datum", "Vertragsdatum", "Ausstellungsdatum", "Bescheiddatum", "Belegdatum", "Date"])
    # 1st pass: date right behind the label.  2nd pass: OCR of tables often puts all labels of a
    # column first and the values some lines later – then take the first date that follows, skipping
    # delivery/due dates that carry their own label.
    for window in (40, 400):
        for label in labels:
            for match in re.finditer(rf"(?<![\w-]){re.escape(label)}\b\s*[:.]?\s*(?:vom\s+)?", text, re.I):
                area = text[match.end():match.end() + window]
                for found in re.finditer(DATE, area, re.I):
                    before = area[max(0, found.start() - 25):found.start()].casefold()
                    if window > 40 and re.search(r"liefer|fällig|faellig|zahlbar|leistung|bis|due|delivery", before):
                        continue
                    if day := _parse(found):
                        return day.isoformat()
                    break
    return None


def first_date(text):
    """Fallback: the first plausible date in the letterhead area, then compact 20260723 style."""
    for match in re.finditer(DATE, text[:4000], re.I):
        if day := _parse(match):
            return day.isoformat()
    for match in re.finditer(COMPACT, text[:6000]):
        try:
            day = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            continue
        if plausible(day):
            return day.isoformat()
    return None


COMPANY = re.compile(r"\b(GmbH|AG|SE|KG|OHG|e\.\s?K\.|UG|Ltd\.?|Co\.,? Ltd|Inc\.?|LLC|S\.A\.|B\.V\.|Stadtwerke|Bank|"
                     r"Sparkasse|Versicherung|Finanzamt|Amt|Universität)\b", re.I)


def is_own(name, own_names):
    """True when name is (part of) one of the user's own names/companies, i.e. the recipient."""
    folded = re.sub(r"[^a-z0-9]", "", str(name or "").casefold())
    if not folded:
        return False
    for own in own_names:
        o = re.sub(r"[^a-z0-9]", "", own.casefold())
        if len(o) >= 4 and (o in folded or folded in o):
            return True
    return False


def letterhead_sender(text, own_names):
    """First company-like line that is not the recipient (used when the model mixed them up)."""
    lines = [re.sub(r"^\W*\b\w\b\s+", "", line.strip()) for line in text.splitlines()]   # "I Sparkasse" -> "Sparkasse"
    head, foot = lines[:60], lines[60:][::-1]   # letterhead first, then the footer from the bottom up
    for line, in_footer in [(l, False) for l in head] + [(l, True) for l in foot]:
        if not COMPANY.search(line) or is_own(line, own_names) or len(line) >= 90:
            continue
        if in_footer and re.search(r"bank|sparkasse|iban|bic|konto", line, re.I):
            continue   # footers list the bank account, not the sender
        name = re.split(r"\s+[-–·|•]\s+|\s*•\s*|,\s*(?=\D)", line.lstrip("([ "))[0].strip()
        return re.sub(r"\s*\(.*$", "", name).strip(" ,;") or None
    return None


TYPE_DE = {"invoice": "Rechnung", "bill": "Rechnung", "commercial invoice": "Rechnung", "proforma": "Proforma-Rechnung",
           "receipt": "Quittung", "kassenbon": "Kassenbon", "bon": "Kassenbon", "delivery note": "Lieferschein",
           "contract": "Vertrag", "offer": "Angebot", "quote": "Angebot", "quotation": "Angebot",
           "statement": "Kontoauszug", "bank statement": "Kontoauszug", "letter": "Brief", "order": "Bestellung",
           "order confirmation": "Auftragsbestätigung", "reminder": "Mahnung", "waybill": "Frachtbrief",
           "air waybill": "Frachtbrief", "credit note": "Gutschrift", "policy": "Versicherung"}


STOPWORDS = {"und", "oder", "für", "fuer", "mit", "von", "der", "die", "das", "des", "zur", "zum", "im", "in", "an", "auf", "&"}


def tidy_words(value, max_words, max_chars):
    """At most max_words whole words within max_chars, never ending on 'und' or a cut-off word."""
    words = [w.strip(",;:") for w in str(value or "").replace("_", " ").split()]
    kept = []
    for word in words[:max_words]:
        if len(" ".join(kept + [word])) > max_chars:
            break
        kept.append(word)
    while kept and (kept[-1].casefold() in STOPWORDS or kept[-1].endswith("-")):
        kept.pop()
    return " ".join(kept) or None


def _date_in_text(iso, text):
    day = date.fromisoformat(iso)
    return any(_parse(m) == day for m in re.finditer(DATE, text, re.I))


LEGAL_FORMS = re.compile(
    r"[\s,]+(gmbh\s*&\s*co\.?\s*kg(aa)?|gmbh|mbh|ag|se|kg|ohg|ug(\s*\(haftungsbeschränkt\))?|e\.\s?v\.|"
    r"gbr|ltd\.?|inc\.?|llc|s\.a\.|b\.v\.|plc|co\.?)$", re.I)


def normalise_sender(sender, known=()):
    """'Telekom Deutschland GmbH' -> 'Telekom' when 'Telekom' was used before; always drop legal forms."""
    if not sender:
        return sender
    name = sender.strip()
    while True:
        stripped = LEGAL_FORMS.sub("", name).strip(" ,")
        if stripped == name or not stripped:
            break
        name = stripped
    folded = name.casefold()
    for candidate in sorted(known, key=len, reverse=True):
        c = candidate.casefold()
        if c and (folded == c or folded.startswith(c + " ") or folded.startswith(c + "-")):
            return candidate
    return name


class DocumentClassifier:
    def __init__(self, endpoint):
        self.endpoint = endpoint   # pipeline.ai.Endpoint (LM Studio, Ollama or a remote server)

    SCHEMA = {"type": "json_schema", "json_schema": {"name": "document", "strict": True, "schema": {
        "type": "object", "additionalProperties": False, "required": ["date", "sender", "type", "keyword"],
        "properties": {"date": {"type": ["string", "null"]}, "sender": {"type": ["string", "null"]},
                       "type": {"type": ["string", "null"]}, "keyword": {"type": ["string", "null"]}}}}}

    def _chat(self, prompt, structured=True):
        options = {"temperature": 0, "max_tokens": 350}
        if structured:
            options["response_format"] = self.SCHEMA
        return self.endpoint.chat([{"role": "user", "content": prompt}], timeout=180, **options)

    def classify_and_rename(self, document_text, known_senders=(), own_names=()):
        if not document_text.strip():
            raise ValueError("Kein Dokumenttext erkannt; bitte OCR-Einstellung prüfen")
        # No example keywords and no list of earlier senders in the prompt: small models copy them into
        # unrelated documents. Known senders are matched afterwards, deterministically (normalise_sender).
        prompt = (
            "Lies das folgende deutsche Dokument und antworte ausschließlich mit einem JSON-Objekt mit diesen Feldern:\n"
            "- date: das Ausstellungsdatum des Dokuments als JJJJ-MM-TT (bei Rechnungen das Rechnungsdatum, "
            "nicht Fälligkeit, Zahlungsziel oder Leistungszeitraum), oder null\n"
            "- sender: wer das Dokument ausgestellt hat (Firma, Behörde oder Person), kurz und ohne Rechtsform, oder null\n"
            "- type: die Dokumentart in einem Wort, z.B. Rechnung, Vertrag, Angebot, Bescheid, Kontoauszug, Brief\n"
            "- keyword: worum es in DIESEM Dokument geht, 1-3 Wörter, nur aus dem Inhalt abgeleitet, oder null\n"
            "Der Absender steht im Briefkopf, Logo oder in der Fußzeile. Der Empfänger steht im Adressfeld "
            "(nach 'An', 'TO:', 'Rechnungsadresse', 'Lieferanschrift') – der Empfänger ist NIE der Absender.\n"
            + (f"Empfänger dieses Dokuments ist: {'; '.join(own_names)}. Diese(r) ist also nicht der Absender.\n"
               if own_names else "") +
            "Übernimm nichts, was nicht im Dokument steht.\n\n"
            "Dokument:\n\"\"\"\n" + document_text[:12000] + "\n\"\"\""
        )
        try:
            try:
                answer = self._chat(prompt)
            except error.HTTPError:
                answer = self._chat(prompt, structured=False)
        except (error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"KI nicht erreichbar ({self.endpoint.describe()}): {exc}") from exc
        match = re.search(r"\{.*\}", answer, re.S)
        if not match:
            raise ValueError("Das Analysemodell hat kein JSON zurückgegeben")
        try:
            raw = json.loads(match.group())
        except json.JSONDecodeError as exc:
            raise ValueError("Das Analysemodell hat ungültiges JSON zurückgegeben") from exc
        result = {key: raw.get(key).strip() if isinstance(raw.get(key), str) and raw.get(key).strip() else None
                  for key in ("date", "sender", "type", "keyword")}
        if result["date"]:
            try:
                if not plausible(date.fromisoformat(result["date"])):
                    result["date"] = None
            except ValueError:
                result["date"] = None
        # The model sometimes answers with a date that is nowhere in the text; only trust it if it is there.
        if result["date"] and not _date_in_text(result["date"], document_text):
            result["date"] = None
        if not result["sender"] or is_own(result["sender"], own_names):
            result["sender"] = letterhead_sender(document_text, own_names)
        result["sender"] = normalise_sender(result["sender"], known_senders)
        raw_type = str(result["type"] or "").strip()
        result["type"] = TYPE_DE.get(raw_type.casefold()) or tidy_words(TYPE_DE.get(raw_type.split()[0].casefold() if raw_type else "", raw_type), 1, 24)
        if result["type"] and re.search(r"\d|nr\b", result["type"], re.I):
            result["type"] = None
        result["keyword"] = tidy_words(result["keyword"], 3, 32)
        result["date"] = explicit_date(document_text, result["type"]) or result["date"] or first_date(document_text)
        result["filename"] = make_filename(result)
        return result
