# 📄 Dokumenten-Sortierer für macOS

Lege Briefe und Rechnungen als PDF in einen Ordner – der Sortierer liest sie (auch Scans), gibt ihnen einen sauberen
Namen wie `2026_09_12_Telekom_Rechnung-Mobilfunk.pdf` und schlägt vor, wo sie hingehören. Du wählst das Ziel in einer
Mindmap deiner Ordner und bestätigst mit <kbd>Enter</kbd>. Alles läuft lokal auf dem Mac – oder auf einem eigenen KI-Server.

## Installation

**Terminal** öffnen (⌘ + Leertaste → „Terminal“ tippen → Enter), diese Zeile hineinkopieren und Enter drücken:

```bash
curl -fsSL https://raw.githubusercontent.com/Bloxx-I/doc-sorter/main/install.sh | bash
```

Der Installer fragt, womit die KI rechnen soll (**Ollama** wird bei Bedarf automatisch installiert, **LM Studio** oder
ein **eigener Server**), legt die App unter `~/Programme/Dokumenten-Sortierer` an und startet sie. Ein Assistent
führt dann durch Modelle, Texterkennung und Ordner. Voraussetzung: macOS 13 oder neuer, ca. 4 GB freier Platz für die Modelle.

## Update

Am einfachsten in der App: **Menüleisten-Symbol → „Update auf … installieren“** (erscheint automatisch, wenn es eine
neue Version gibt – dann steht ein ↑ neben dem Symbol) oder **Einstellungen → System → Nach Updates suchen**.

Oder im Terminal – das ist derselbe Befehl wie bei der Installation; Einstellungen und Verlauf bleiben erhalten:

```bash
curl -fsSL https://raw.githubusercontent.com/Bloxx-I/doc-sorter/main/install.sh | bash
```

## Weitere Befehle

- **Mit PaddleOCR** (zusätzliche Texterkennung, ~1 GB – geht auch später per Knopf unter Einstellungen → Texterkennung): `curl -fsSL https://raw.githubusercontent.com/Bloxx-I/doc-sorter/main/install.sh | bash -s -- --with-paddle`
- **Entfernen:** `curl -fsSL https://raw.githubusercontent.com/Bloxx-I/doc-sorter/main/install.sh | bash -s -- --uninstall`

## Im Alltag

- Oben in der **Menüleiste** sitzt ein Ablage-Symbol (mit Zahl, wenn Dokumente warten). Fenster schließen = läuft
  im Hintergrund weiter. Menü: öffnen, Ablage-Karte, Suche, Protokoll, **Überwachung pausieren** (15/30/60 Min. oder
  bis zum Fortsetzen), Eingang prüfen, Einstellungen, **Beim Anmelden starten**, Beenden.
- Kommt ein neues PDF, springt das Fenster nach vorne. Name prüfen, Ziel anklicken, <kbd>Enter</kbd>.
- **Mehrere Eingangsordner** unter Einstellungen → Ordner. „Rückgängig“ legt die Datei dorthin zurück, woher sie kam.
- **Viele Dokumente auf einmal:** Der Sortierer arbeitet den ganzen Stapel im Hintergrund ab und meldet sich erst,
  wenn alles analysiert ist – du sortierst dann in einem Rutsch, während Nachzügler weiter im Hintergrund laufen.
- **Meine Namen & Firmen** (Einstellungen → Ordner): an wen die Post geht – wird nie als Absender verwendet.
- **Rechenleistung** (Einstellungen → System): lokale KI nur am Netzteil bzw. nur, wenn der Mac gerade ruht.
- **Alles zurücksetzen** (Einstellungen → System): alle sortierten Dokumente wandern unter ihrem Originalnamen zurück
  in den Eingang, leere Ordner der App verschwinden, der Verlauf wird gelöscht – praktisch zum erneuten Testen.

## KI: lokal oder auf einem schnellen Server

Unter **Einstellungen → KI-Rechenleistung** hat jede Aufgabe ihren eigenen Endpunkt – *LM Studio*, *Ollama* oder
*Eigener Server* (jeder OpenAI-kompatible Server, z. B. LM Studio/Ollama/vLLM auf einem Rechner im Heimnetz, optional
mit API-Schlüssel). So lässt sich z. B. nur die Analyse auf den schnellen Server legen. Ist ein **lokales** LM Studio
oder Ollama eingetragen und läuft nicht, startet der Sortierer es selbst (`lms server start` bzw. Ollama-App).

| Texterkennung für Scans | Geschwindigkeit | Hinweis |
| --- | --- | --- |
| **Apple Vision** (Standard) | ~0,2 s/Seite | in macOS eingebaut, kein Download |
| GLM-OCR | ~8 s/Seite lokal | am genauesten (Tabellen); auf einem Server schneller |
| PaddleOCR | ~25 s/Aufruf | optional, lädt seine Modelle bei jedem Aufruf |
| **Direkt an die KI** | ~1–2 min/Dokument (Qwen 3.8, gründlich) | keine OCR: die Seiten gehen als Bilder an ein Modell, das Bilder versteht – es liest Angaben und Text selbst |

Jede Seite wird per OCR gelesen – eingebettete Textschichten alter Scanner sind oft falsch. Optional (Einstellungen →
Texterkennung → „Nur digitale PDFs“) wird der exakte Text von direkt erzeugten PDFs genutzt. Fällt eine Erkennung aus,
übernimmt die nächste.

### Denkaufwand

Unter **Einstellungen → System → Denkaufwand der KI** lassen sich die offiziellen Stufen von Qwen 3.8 wählen:
**xhigh** (Standard, gründlich), **medium**, **low** oder **Aus**. Es gibt kein Token-Limit – das Modell denkt so
lange, wie es braucht. Modelle ohne Denkmodus ignorieren die Einstellung.

## Bedienung

| Aktion | So geht's |
| --- | --- |
| Vorschlag übernehmen | `Enter` oder `Leertaste` |
| Ziel an/aus | Ordner in der Karte anklicken, oder `1`–`4` für die Vorschläge oben |
| Mehrere Ziele | einfach mehrere Ordner anklicken – die Datei wird in jeden kopiert |
| Ordner wählen | großer **Ordner wählen**-Knopf, `N`, oder das kleine ＋ an jedem Knoten: öffnet den Finder-Dialog (startet im markierten Ordner, „Neuer Ordner“ inklusive). Auch andere Laufwerke gehen – sie erscheinen in der Karte unter **Andere Orte** und werden künftig mit vorgeschlagen. |
| Karte | ziehen = verschieben, Pinch/⌘-Scroll = zoomen, `F` = einpassen, Doppelklick = auf-/zuklappen |
| Dokumente wechseln | `↑`/`↓` |
| Überspringen | `⌘⌫` – bleibt im Eingang, bis sich die Datei ändert |
| Rückgängig | im Toast nach dem Ablegen oder im Protokoll |

Farben in der Karte: **violett** = Ziel, **orange** = Vorschlag (mit Prozent und Begründung im Tooltip),
**türkis gestrichelt** = neuer Ordner. Das Dokument hängt als Kärtchen an jedem gewählten Ziel.

## Verarbeitung

```
PDF → eingebetteter Text ─┐
     └ Scan → Apple Vision | GLM-OCR | PaddleOCR (gewählte zuerst) → Tesseract
→ Phi-3.5: Datum, Absender, Art, Stichwort (ein Aufruf, JSON-Schema)
→ Absender normalisieren (Rechtsform weg, bekannte Schreibweise übernehmen)
→ Ordnervorschläge → Freigabe → Ablage(n) → SQLite-Protokoll → Suchindex
```

**Ordnervorschläge** kommen nicht vom Sprachmodell, sondern aus deinen bisherigen Entscheidungen:
gleicher Absender/Art/Stichwort in früheren Ablagen, Embedding-Ähnlichkeit zu abgelegten Dokumenten,
passende Ordnernamen und Jahres-/Monatsmuster (`Rechnungen/2025/06` + Dokument vom 09/2026 → `Rechnungen/2026/09`, neu).

| Aufgabe | Modell | Hinweis |
| --- | --- | --- |
| Umbenennung & Art | `phi-3.5-mini-instruct` (LM Studio) / `phi3.5` (Ollama) | austauschbar, z. B. `google/gemma-4-e4b` für besseres Deutsch |
| Suche & ähnliche Ablagen | `nomic-embed-text` | hybride Suche: Bedeutung + Stichworte |
| Genaue Texterkennung | `glm-ocr` | optional, sonst Apple Vision |

## Entwicklungs-Setup

```bash
uv venv --python 3.12 venv
uv pip install --python venv/bin/python -r requirements.txt -r requirements-paddle.txt
venv/bin/python main.py
```

Ein Checkout mit eigener `config.json` nutzt diese; ohne sie liegen Einstellungen und Verlauf in
`~/Library/Application Support/Dokumenten-Sortierer/` (so wie bei der installierten App).

## Entwicklung

- `venv/bin/python main.py --browser` – dieselbe Oberfläche unter http://127.0.0.1:8765 (nur localhost).
- `--config pfad/config.json` – andere Konfiguration, z. B. eine Sandbox mit Testordnern.
- `--debug` – WebKit-Inspector im nativen Fenster.
- Tests: `venv/bin/python -m unittest discover -s tests`

Aufbau: `pipeline/` (OCR, Klassifizierung, Ablage), `watch/watcher.py` (Watchdog + Service),
`storage/database.py` (SQLite), `gui/api.py` (Brücke zu JavaScript), `gui/web/` (Oberfläche, `mindmap.js`).

## Daten und Sicherheit

- Jede Zielkopie bekommt einen Eintrag in `storage/sort_history.db` (Quelle, Ziel, Zeit, erkannter Text).
- Namensgleichheit → `__2`, `__3` …; es wird nie überschrieben. Ziele außerhalb der Ablage oder versteckte Ordner werden abgelehnt.
- Dokumenttext geht nur an die eingestellten KI-Endpunkte (standardmäßig lokal); externe Server sind in den Einstellungen als „extern“ markiert. Die Oberfläche lädt nichts aus dem Internet.
- iCloud-Platzhalter (`.Datei.pdf.icloud`) werden vor der Analyse heruntergeladen.
