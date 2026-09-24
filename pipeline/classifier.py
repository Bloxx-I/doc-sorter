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


def explicit_date(text, document_type):
    labels = (["Rechnungsdatum", "Ausstellungsdatum", "Datum"] if "rechnung" in str(document_type or "").casefold()
              else ["Datum", "Vertragsdatum", "Ausstellungsdatum"])
    for label in labels:
        match = re.search(rf"\b{label}\b\s*:?\s*(\d{{1,2}}[.]\d{{1,2}}[.]\d{{4}}|\d{{4}}-\d{{2}}-\d{{2}})", text, re.I)
        if match:
            raw = match.group(1)
            try:
                return (datetime.strptime(raw, "%d.%m.%Y").date() if "." in raw else date.fromisoformat(raw)).isoformat()
            except ValueError:
                pass
    return None


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

    def classify_and_rename(self, document_text, known_senders=()):
        if not document_text.strip():
            raise ValueError("Kein Dokumenttext erkannt; bitte OCR-Einstellung prüfen")
        hint = ""
        if known_senders:
            hint = ("Bereits bekannte Absender (verwende exakt diese Schreibweise, falls es derselbe ist): "
                    + "; ".join(known_senders) + "\n")
        prompt = (
            "Analysiere dieses deutsche Dokument. Antworte ausschließlich mit einem JSON-Objekt "
            "mit date (JJJJ-MM-TT oder null), sender (Rechnungssteller/Vertragspartner, kurzer Firmenname "
            "ohne Rechtsform wie GmbH/AG), type (z.B. Rechnung, Vertrag, Angebot, Verbrauchsabrechnung, "
            "Kontoauszug, Bescheid), keyword (kurzes Thema, 1-3 Wörter, z.B. Mobilfunk, Strom, Kfz-Versicherung). "
            "Bei Rechnungen ist date das Rechnungsdatum, nicht Fälligkeit oder Leistungszeitraum. "
            "Erfinde keine Angaben; nutze null bei Unsicherheit.\n" + hint + "Dokumenttext:\n" + document_text[:12000]
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
                date.fromisoformat(result["date"])
            except ValueError:
                result["date"] = None
        result["sender"] = normalise_sender(result["sender"], known_senders)
        result["date"] = explicit_date(document_text, result["type"]) or result["date"]
        result["filename"] = make_filename(result)
        return result
