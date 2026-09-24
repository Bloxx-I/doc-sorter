"""Watchdog queues stable PDFs and prepares proposals without moving files."""

import base64
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from urllib import request

import pymupdf
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from pipeline.classifier import DocumentClassifier, make_filename
from pipeline.ai import TASKS, Endpoint, endpoints_from_legacy, is_local
from pipeline.ocr import ENGINES, ocr_pipeline, ocr_status, vision_page
from pipeline.sort import place_document
from storage.database import SortHistoryDB

DEFAULTS = {
    "ocr_mode": "vision",
    "compute_policy": "always",   # always | plugged | plugged_idle  (only matters for local AI)
    "own_names": [],
    "embedded_text": "never",
    "analysis_mode": "ocr",       # ocr = OCR, then text to the AI | vision = page images straight to the AI
    "reasoning_effort": "xhigh",  # off | low | medium | xhigh (official Qwen 3.8 levels)     # never = always OCR | digital = use text of born-digital PDFs              # the user's names/companies: recipients, never senders
    "paddle_python": "paddle-venv/bin/python",
    "database": "storage/sort_history.db",
}
LEGACY_KEYS = ("lm_base_url", "llm_model", "ocr_model", "embedding_model")
YEAR = re.compile(r"^(19|20)\d{2}$")
MONTH = re.compile(r"^(0[1-9]|1[0-2])$")


MAX_VISION_PAGES = 8


def render_pages(pdf_path, dpi=150):
    """Page images for vision models (first pages; letters and invoices put everything important there)."""
    with pymupdf.open(pdf_path) as document:
        if not document.is_pdf or document.needs_pass:
            raise ValueError("Nur unverschlüsselte PDF-Dateien werden unterstützt")
        return [page.get_pixmap(dpi=dpi, alpha=False).tobytes("png") for page in list(document)[:MAX_VISION_PAGES]]


def on_ac_power():
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
        return "AC Power" in out.splitlines()[0] if out else True
    except Exception:
        return True


def idle_seconds():
    """Seconds since the last keyboard/mouse input (HIDIdleTime)."""
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem", "-d", "4"], capture_output=True, text=True, timeout=5).stdout
        match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
        return int(match.group(1)) / 1e9 if match else 1e9
    except Exception:
        return 1e9


class Handler(FileSystemEventHandler):
    def __init__(self, service):
        self.service = service

    def on_created(self, event):
        if not event.is_directory:
            self.service.submit(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self.service.submit(event.dest_path)

    def on_modified(self, event):
        if not event.is_directory:
            self.service.submit(event.src_path)


def valid_relative(relative):
    parts = Path(relative).parts
    return relative == "." or (parts and all(p not in (".", "..") and not p.startswith(".") and "/" not in p
                                             for p in parts) and not Path(relative).is_absolute())


class DocumentService:
    def __init__(self, config_path):
        self.config_path = Path(config_path)
        self.config = {**DEFAULTS, **json.loads(self.config_path.read_text())}
        if self.config.get("ocr_mode") not in ENGINES:
            self.config["ocr_mode"] = "vision"
        if "endpoints" not in self.config:   # migrate single LM Studio URL to per-task endpoints
            self.config["endpoints"] = endpoints_from_legacy(self.config)
        for key in LEGACY_KEYS:
            self.config.pop(key, None)
        # Older configs have a single "incoming_dir"; now any number of folders can be watched.
        dirs = self.config.get("incoming_dirs") or [self.config.get("incoming_dir")]
        self.config["incoming_dirs"] = [str(Path(d).expanduser()) for d in dirs if d]
        self.config.pop("incoming_dir", None)
        db_path = Path(self.config["database"])
        self.db = SortHistoryDB(db_path if db_path.is_absolute() else self.config_path.parent / db_path)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="analyse")
        self.indexer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="index")
        self.lock = threading.RLock()
        self.in_flight = {}
        self.observer = None
        self.paused_until = None   # None = running, float = epoch seconds, math.inf = until resumed
        self._stopping = False

    @property
    def incoming_dirs(self):
        return [Path(d).resolve() for d in self.config["incoming_dirs"]]

    def is_incoming(self, folder):
        folder = Path(folder).resolve()
        return any(folder == d or folder.is_relative_to(d) for d in self.incoming_dirs)

    # ---------------------------------------------------------------- lifecycle
    def start(self):
        for incoming in self.incoming_dirs:
            incoming.mkdir(parents=True, exist_ok=True)
        Path(self.config["output_dir"]).expanduser().mkdir(parents=True, exist_ok=True)
        self._watch()
        self.warm_up()
        self.scan()
        if self.embedding_enabled:
            self.indexer.submit(self._backfill)

    def _watch(self):
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=5)
        self.observer = Observer()
        for incoming in self.incoming_dirs:
            if incoming.is_dir():
                self.observer.schedule(Handler(self), str(incoming), recursive=False)
        self.observer.start()

    def stop(self):
        self._stopping = True
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=5)
            self.observer = None
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.indexer.shutdown(wait=False, cancel_futures=True)

    def reconfigure(self, values):
        incoming = [Path(d).expanduser().resolve() for d in values.get("incoming_dirs", self.config["incoming_dirs"]) if str(d).strip()]
        output = Path(values.get("output_dir", self.config["output_dir"])).expanduser().resolve()
        if not incoming:
            raise ValueError("Mindestens ein Eingangsordner ist nötig")
        missing = [str(d) for d in incoming if not d.is_dir()]
        if missing or not output.is_dir():
            raise ValueError("Ordner nicht gefunden: " + ", ".join(missing or [str(output)]))
        if any(d == output or output.is_relative_to(d) for d in incoming):
            raise ValueError("Die Ablage darf nicht in einem Eingangsordner liegen")
        endpoints = values.get("endpoints", self.config["endpoints"])
        for task in TASKS:
            url = str((endpoints.get(task) or {}).get("url", ""))
            if not url.startswith(("http://", "https://")):
                raise ValueError(f"Ungültige Server-Adresse für {task}: {url or '(leer)'}")
        self.config.update({k: v for k, v in values.items() if k in DEFAULTS})
        self.config["endpoints"] = {task: {k: str(endpoints[task].get(k, "")).strip()
                                           for k in ("provider", "url", "model", "api_key")} for task in TASKS}
        self.config["incoming_dirs"] = list(dict.fromkeys(str(d) for d in incoming))
        self.config["output_dir"] = str(output)
        self.config_path.write_text(json.dumps(self.config, indent=2, ensure_ascii=False) + "\n")
        self._watch()
        self.scan()

    # ---------------------------------------------------------------- pause
    def pause(self, minutes=None):
        """Pause watching for n minutes, or until resume() when minutes is None."""
        self.paused_until = math.inf if minutes is None else time.time() + minutes * 60
        self.db.event("paused", detail="bis auf Weiteres" if minutes is None else f"für {minutes} Minuten")

    def resume(self):
        if self.paused_until is None:
            return
        self.paused_until = None
        self.db.event("resumed", detail="Überwachung fortgesetzt")
        self.scan()   # pick up everything that arrived meanwhile

    @property
    def paused(self):
        if self.paused_until is not None and time.time() >= self.paused_until:
            self.resume()
        return self.paused_until is not None

    # ---------------------------------------------------------------- analysis
    def scan(self, force=False):
        if self.paused and not force:
            return
        for incoming in self.incoming_dirs:
            if incoming.is_dir():
                for path in sorted(incoming.iterdir()):
                    self.submit(path, force=force)

    def submit(self, path, force=False):
        if self.paused and not force:
            return
        path = Path(path)
        if path.name.endswith(".pdf.icloud") and path.name.startswith("."):
            self._download_icloud(path)
            return
        if path.suffix.lower() != ".pdf" or path.name.startswith("."):
            return
        source = str(path.resolve())  # watchdog reports /private/var/…, scan() may give /var/…
        with self.lock:
            if source in self.in_flight or self.db.has_open_source(source):
                return
            try:
                stat = os.stat(source)
            except OSError:
                return
            if self.db.was_skipped(source, stat.st_size, stat.st_mtime_ns):
                return
            self.in_flight[source] = "Wartet"
        self.executor.submit(self._prepare, source)

    @staticmethod
    def _download_icloud(placeholder):
        """iCloud keeps '.Name.pdf.icloud' stubs; ask the system to fetch the real file."""
        real = placeholder.with_name(placeholder.name[1:-len(".icloud")])
        subprocess.run(["brctl", "download", str(real)], capture_output=True, timeout=30)

    def _stage(self, source, text):
        with self.lock:
            if source in self.in_flight:
                self.in_flight[source] = text

    def _prepare(self, source):
        try:
            self._stage(source, "Wird kopiert …")
            stable = None
            for _ in range(60):
                try:
                    stat = os.stat(source)
                except OSError:
                    return
                fingerprint = (stat.st_size, stat.st_mtime_ns)
                if stat.st_size and fingerprint == stable:
                    break
                stable = fingerprint
                time.sleep(1)
            else:
                raise TimeoutError("Datei wurde innerhalb von 60 Sekunden nicht vollständig geschrieben")
            if self.db.has_open_source(source):
                return
            if not self._wait_for_resources(source):
                return
            effort = self.config.get("reasoning_effort") or ("off" if self.config.get("thinking") is False else "xhigh")
            model = DocumentClassifier(Endpoint.from_config(self.config, "llm"), effort)
            own, known = self.config.get("own_names", []), self.db.known_senders()
            info = None
            if self.config.get("analysis_mode") == "vision":
                try:
                    pages = render_pages(source)
                    self._stage(source, f"KI liest {len(pages)} Seite(n) direkt")
                    info = model.classify_images(pages, known, own)
                    text = info.pop("text", "")
                    method = f"KI direkt · {model.endpoint.model}"
                except Exception as exc:
                    self.db.event("ocr_fallback", source, detail=f"Direktanalyse fehlgeschlagen ({exc}); OCR übernimmt")
                    info = None
            if info is None:
                self._stage(source, "Text wird gelesen")
                text, method = ocr_pipeline(source, self.config, progress=lambda m: self._stage(source, m),
                                            log=lambda m: self.db.event("ocr_fallback", source, detail=m))
                self._stage(source, "KI analysiert" + (" und denkt nach" if model.thinking else ""))
                info = model.classify_and_rename(text, known, own)
            stat = os.stat(source)
            if (stat.st_size, stat.st_mtime_ns) != fingerprint:
                raise RuntimeError("Quelldatei hat sich während der Analyse geändert; bitte erneut scannen")
            vector = None
            if self.embedding_enabled:
                self._stage(source, "Ähnliche Dokumente suchen")
                try:
                    vector = self.embedding(self._index_text(info, text))
                except Exception as exc:
                    self.db.event("index_error", source, detail=f"Embedding: {exc}")
            proposal_id = self.db.add_proposal(source, stat, info, text, method)
            if vector:
                self.db.set_proposal_embedding(proposal_id, vector)
            self.db.event("proposal", source, detail=f"Vorschlag #{proposal_id} · {method}")
        except Exception as exc:
            self.db.event("error", source, detail=str(exc))
        finally:
            with self.lock:
                self.in_flight.pop(source, None)

    # ---------------------------------------------------------------- power / idle policy
    WAITING = ("Wartet auf Netzteil", "Wartet, bis der Mac ruht")

    def uses_local_ai(self):
        endpoints = [Endpoint.from_config(self.config, "llm")]
        if self.config.get("ocr_mode") == "glm":
            endpoints.append(Endpoint.from_config(self.config, "ocr"))
        return any(is_local(e.url) for e in endpoints)

    def resources_ok(self):
        """(ok, reason) – heavy local AI only runs on AC power (and, if chosen, while the Mac is idle)."""
        policy = self.config.get("compute_policy", "always")
        if policy == "always" or not self.uses_local_ai():
            return True, None
        if not on_ac_power():
            return False, self.WAITING[0]
        if policy == "plugged_idle" and idle_seconds() < 120:
            return False, self.WAITING[1]
        return True, None

    def _wait_for_resources(self, source):
        while True:
            ok, reason = self.resources_ok()
            if ok:
                return True
            self._stage(source, reason)
            for _ in range(15):
                if self._stopping or not os.path.exists(source):
                    return False
                time.sleep(1)

    def busy(self):
        """Documents that are actually being worked on (not merely waiting for power/idle)."""
        with self.lock:
            return any(stage not in self.WAITING for stage in self.in_flight.values())

    @staticmethod
    def _index_text(info, text):
        return " ".join(str(info.get(k) or "") for k in ("sender", "type", "document_type", "keyword")) + " " + text

    def processing(self):
        with self.lock:
            return [{"source": s, "name": Path(s).name, "stage": st} for s, st in self.in_flight.items()]

    # ---------------------------------------------------------------- folders
    @property
    def root(self):
        return Path(self.config["output_dir"]).resolve()

    def resolve_destination(self, destination):
        """Relative paths live inside the Ablage (and may be created); absolute paths are folders
        picked in Finder anywhere else, e.g. on another drive (they must exist)."""
        if Path(destination).is_absolute():
            target = Path(destination).resolve()
            if not target.is_dir():
                raise ValueError(f"Ordner nicht gefunden: {destination}")
        else:
            target = (self.root / destination).resolve()
            if not valid_relative(destination) or not target.is_relative_to(self.root):
                raise ValueError(f"Zielordner ungültig: {destination}")
        if self.is_incoming(target):
            raise ValueError("Ein Eingangsordner kann kein Ablageziel sein")
        return target

    def as_destination(self, folder):
        """Inverse of resolve_destination: relative inside the Ablage, absolute elsewhere."""
        folder = Path(folder).resolve()
        if folder.is_relative_to(self.root):
            relative = str(folder.relative_to(self.root))
            return relative
        return str(folder)

    def _known_destination(self, folder):
        folder = Path(folder).resolve()
        if not folder.is_dir() or self.is_incoming(folder) or any(p.startswith(".") for p in folder.parts[1:]):
            return None
        return self.as_destination(folder)

    def external_folders(self):
        """Folders outside the Ablage that were used as destinations before (other drives …)."""
        return sorted({d for d in (self._known_destination(Path(p).parent) for p in self.db.destination_paths())
                       if d and Path(d).is_absolute()}, key=str.casefold)

    def folders(self):
        root, incoming = self.root, set(self.incoming_dirs)
        folders = ["."]
        for current, names, _ in os.walk(root, followlinks=False):
            names[:] = sorted((name for name in names if not name.startswith(".") and
                               not (Path(current) / name).is_symlink() and
                               (Path(current) / name).resolve() not in incoming), key=str.casefold)
            if len(folders) > 3000:
                break
            for name in names:
                folders.append(str((Path(current) / name).relative_to(root)))
        return folders

    def folder_tree(self):
        """Nested folder structure with the number of PDFs directly inside each folder."""
        root = self.root
        nodes = {}
        for relative in self.folders():
            path = root / relative
            try:
                count = sum(1 for e in os.scandir(path) if e.is_file() and e.name.lower().endswith(".pdf")
                            and not e.name.startswith("."))
            except OSError:
                count = 0
            nodes[relative] = {"path": relative, "name": root.name if relative == "." else Path(relative).name,
                               "count": count, "children": []}
            if relative != ".":
                parent = str(Path(relative).parent)
                nodes.get(parent, nodes["."])["children"].append(nodes[relative])
        return nodes["."]

    def folder_documents(self, relative, limit=300):
        """Files in a folder and its subfolders, newest first."""
        if not valid_relative(relative):
            raise ValueError("Ungültiger Ordner")
        folder = (self.root / relative).resolve()
        if not folder.is_relative_to(self.root) or not folder.is_dir():
            return []
        files = []
        for current, names, filenames in os.walk(folder):
            names[:] = [n for n in names if not n.startswith(".")]
            for name in filenames:
                if not name.startswith("."):
                    path = Path(current) / name
                    sub = str(Path(current).relative_to(folder))
                    files.append({"name": name, "path": str(path), "sub": "" if sub == "." else sub,
                                  "mtime": path.stat().st_mtime})
            if len(files) > limit * 3:
                break
        return sorted(files, key=lambda f: f["mtime"], reverse=True)[:limit]

    @staticmethod
    def _adapt_to_date(relative, day, existing):
        """Rechnungen/2024/03 + a 2026-09 document → Rechnungen/2026/09 (possibly new)."""
        if not day:
            return None
        parts = list(Path(relative).parts)
        if len(parts) >= 2 and YEAR.match(parts[-2]) and MONTH.match(parts[-1]):
            parts[-2:] = [f"{day.year}", f"{day.month:02d}"]
        elif parts and YEAR.match(parts[-1]):
            parts[-1] = f"{day.year}"
        else:
            # A folder whose children are year folders gets the matching year appended.
            children = [p for p in existing if str(Path(p).parent) == relative]
            years = [p for p in children if YEAR.match(Path(p).name)]
            if not years or len(years) < len(children) / 2:
                return None
            months = [p for p in existing if str(Path(p).parent) in years and MONTH.match(Path(p).name)]
            parts += [f"{day.year}"] + ([f"{day.month:02d}"] if months else [])
        adapted = str(Path(*parts))
        return adapted if adapted != relative else None

    def suggestions(self, proposal):
        """Ranked destination proposals with a human-readable reason each."""
        info = {"sender": proposal["sender"], "type": proposal["document_type"], "keyword": proposal["keyword"]}
        existing = self.folders()
        existing_set = set(existing)
        found = {}

        def add(path, score, reason):
            new = not Path(path).is_absolute() and path not in existing_set
            entry = found.setdefault(path, {"path": path, "score": 0.0, "reasons": [], "new": new})
            entry["score"] = max(entry["score"], score)
            if reason not in entry["reasons"]:
                entry["reasons"].append(reason)

        for path, score, count, hits in self.db.destinations_for(info, self._known_destination):
            confidence = min(1.0, 0.45 + score / 20) * (1 if "Art" in hits or not info["type"] else 0.8)
            add(path, confidence, f"{count}× hier abgelegt ({', '.join(hits)})")
        if proposal.get("embedding"):
            vector = json.loads(proposal["embedding"])
            # nomic similarities sit around 0.7 even for unrelated German letters, so only
            # neighbours close to the best match count, rescaled to a 0..0.9 confidence.
            neighbours = self.db.neighbours(vector, self._known_destination)
            top = neighbours[0][0] if neighbours else 0
            best = {}
            for similarity, path, name in neighbours:
                if similarity >= 0.72 and similarity >= top - 0.04 and path not in best:
                    best[path] = (similarity, name)
            for path, (similarity, name) in list(best.items())[:3]:
                add(path, min(0.9, max(0.3, (similarity - 0.55) / 0.4)), f"ähnlich wie {name}")
        tokens = [str(info[k] or "").casefold() for k in ("type", "keyword", "sender")]
        for folder in existing:
            if folder == ".":
                continue
            name = Path(folder).name.casefold()
            if any(t and len(t) > 2 and (name == t or name.startswith(t) or t.startswith(name.removesuffix("en")))
                   for t in tokens if len(name) > 2):
                add(folder, 0.4, "Ordnername passt")

        try:
            day = date.fromisoformat(str(proposal.get("date_extracted") or ""))
        except ValueError:
            day = None
        for entry in list(found.values()):
            adapted = self._adapt_to_date(entry["path"], day, existing)
            if adapted and valid_relative(adapted):
                add(adapted, entry["score"] + 0.05, f"Jahresordner für {day:%m/%Y}")
                if not Path(entry["path"]).name.isdigit():
                    continue
                entry["score"] -= 0.3  # the old year/month folder itself is a weaker match

        ranked = sorted(found.values(), key=lambda e: -e["score"])
        if not ranked and info["type"]:
            name = re.sub(r"[/\\:.]", "-", info["type"]).strip() or "Sonstiges"
            ranked = [{"path": name, "score": 0.3, "reasons": ["Neuer Ordner nach Dokumentart"],
                       "new": name not in existing_set}]
        for entry in ranked:
            entry["score"] = round(max(0.05, min(entry["score"], 0.99)), 2)
        return ranked[:4] or [{"path": ".", "score": 0.1, "reasons": ["Hauptordner"], "new": False}]

    def create_folder(self, parent, name):
        root = self.root
        if not name or name in (".", "..") or "/" in name or "\\" in name or name.startswith("."):
            raise ValueError("Ungültiger Ordnername")
        folder = (root / parent / name).resolve()
        if not folder.is_relative_to(root) or not (root / parent).is_dir():
            raise ValueError("Ordner liegt außerhalb der Ablage")
        folder.mkdir(exist_ok=False)
        self.db.event("folder", destination=str(folder), detail="Ordner in der GUI angelegt")
        return str(folder.relative_to(root))

    # ---------------------------------------------------------------- embeddings
    @property
    def embedding_enabled(self):
        return bool(self.config["endpoints"].get("embedding", {}).get("model", "").strip())

    def embedding(self, text, kind="search_document"):
        endpoint = Endpoint.from_config(self.config, "embedding")
        if not endpoint.model:
            return None
        content = f"{kind}: {text[:8000]}" if "nomic" in endpoint.model.casefold() else text[:8000]
        return endpoint.embed(content, timeout=60)

    def _index(self, proposal):
        try:
            if proposal.get("embedding"):
                vector = json.loads(proposal["embedding"])
            else:
                vector = self.embedding(self._index_text(proposal, proposal.get("ocr_text") or ""))
            self.db.update_embeddings(proposal["id"], vector)
            self.db.event("indexed", proposal["source_path"], detail="Semantischer Suchindex erstellt")
        except Exception as exc:
            self.db.event("index_error", proposal["source_path"], detail=str(exc))

    def _backfill(self):
        for record in self.db.unindexed():
            try:
                self.db.update_embedding(record["id"], self.embedding(self._index_text(record, record["ocr_text"])))
            except Exception as exc:
                self.db.event("index_error", detail=f"Altbestand #{record['id']}: {exc}")
                break

    # ---------------------------------------------------------------- decisions
    def approve(self, proposal_id, filename, destinations, metadata=None):
        with self.lock:
            proposal = self.db.get_proposal(proposal_id)
            if not proposal or proposal["status"] != "pending":
                raise ValueError("Vorschlag nicht mehr offen")
            source = Path(proposal["source_path"])
            stat = source.stat()
            if stat.st_size != proposal["source_size"] or stat.st_mtime_ns != proposal["source_mtime_ns"]:
                raise ValueError("Quelldatei hat sich seit der Vorschau geändert. Bitte erneut analysieren.")
            destinations = list(dict.fromkeys(destinations or []))
            targets = [self.resolve_destination(d) for d in destinations]
            created = []
            for target in targets:
                if not target.exists():
                    target.mkdir(parents=True)
                    created.append(target)
                    self.db.event("folder", destination=str(target), detail="Neuer Ordner bei Ablage angelegt")
            if metadata:
                self.db.update_proposal(proposal_id, {"sender": metadata.get("sender"),
                                                      "document_type": metadata.get("type"),
                                                      "keyword": metadata.get("keyword"),
                                                      "date_extracted": metadata.get("date"),
                                                      "filename": filename})
                proposal = self.db.get_proposal(proposal_id)
            try:
                paths = place_document(source, targets, filename)
            except Exception:
                for folder in reversed(created):
                    try:
                        folder.rmdir()
                    except OSError:
                        pass
                raise
            try:
                self.db.log_placements(proposal, paths)
            except Exception:
                shutil.copy2(paths[0], source)
                for path in paths:
                    Path(path).unlink(missing_ok=True)
                raise
            if self.embedding_enabled:
                self.indexer.submit(self._index, proposal)
            return paths

    def undo(self, proposal_id):
        """Move a filed document back into the inbox and forget its placements."""
        with self.lock:
            placements = self.db.placements_for(proposal_id)
            if not placements:
                raise ValueError("Keine Ablage zum Rückgängigmachen gefunden")
            existing = [Path(p["destination_path"]) for p in placements if Path(p["destination_path"]).is_file()]
            if not existing:
                raise ValueError("Die abgelegten Dateien existieren nicht mehr")
            origin = Path(placements[0]["source_path"]).parent
            inbox = origin if origin.is_dir() and self.is_incoming(origin) else self.incoming_dirs[0]
            target = inbox / placements[0]["original_filename"]
            index = 2
            while target.exists():
                target = target.with_name(f"{Path(placements[0]['original_filename']).stem}__{index}.pdf")
                index += 1
            shutil.move(str(existing[0]), target)
            for path in existing[1:]:
                path.unlink(missing_ok=True)
            self.db.undo_placements(proposal_id, str(target.resolve()), target.stat())
            return str(target)

    def reject(self, proposal_id):
        proposal = self.db.get_proposal(proposal_id)
        if not proposal or proposal["status"] != "pending":
            raise ValueError("Vorschlag nicht mehr offen")
        self.db.decide(proposal_id, "skipped")
        self.db.event("skipped", proposal["source_path"], detail="In der GUI übersprungen")

    def reanalyze(self, proposal_id):
        proposal = self.db.get_proposal(proposal_id)
        if not proposal or proposal["status"] != "pending":
            raise ValueError("Vorschlag nicht mehr offen")
        self.db.decide(proposal_id, "superseded")
        self.submit(proposal["source_path"])

    # ---------------------------------------------------------------- helpers for the GUI
    def page_image(self, proposal_id, page=0, width=900):
        proposal = self.db.get_proposal(proposal_id)
        if not proposal or not Path(proposal["source_path"]).is_file():
            return None
        with pymupdf.open(proposal["source_path"]) as document:
            page = max(0, min(page, len(document) - 1))
            pdf_page = document[page]
            zoom = width / pdf_page.rect.width
            png = pdf_page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).tobytes("png")
            return {"image": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
                    "page": page, "pages": len(document)}

    def rename_preview(self, metadata):
        return make_filename(metadata)

    def health(self):
        """Which endpoints answer and whether their models are there (for the status dots)."""
        status = ocr_status(self.config)
        status["endpoints"] = {}
        for task in TASKS:
            endpoint = Endpoint.from_config(self.config, task)
            try:
                models = endpoint.models()
                reachable = True
            except Exception:
                models, reachable = [], False
            status["endpoints"][task] = {"reachable": reachable, "models": models, "remote": not is_local(endpoint.url),
                                         "ready": reachable and (not endpoint.model or endpoint.model in models
                                                                 or endpoint.provider == "ollama"
                                                                 and endpoint.model.split(":")[0] in
                                                                 [m.split(":")[0] for m in models])}
        status["llm"] = status["endpoints"]["llm"]["ready"]
        status["embedding"] = status["endpoints"]["embedding"]["ready"]
        status["models"] = status["endpoints"]["llm"]["models"]
        return status

    def warm_up(self):
        """Start LM Studio/Ollama in the background if an endpoint needs it (called at app start)."""
        def run():
            if self.config.get("ocr_mode") == "vision":   # Vision loads its models (~30 s) on the first call
                try:
                    blank = pymupdf.open()
                    blank.new_page(width=200, height=80).insert_text((10, 40), "Warmup")
                    vision_page(blank[0].get_pixmap(dpi=72).tobytes("png"))
                except Exception:
                    pass
            for task in TASKS:
                try:
                    Endpoint.from_config(self.config, task).ensure_running()
                except Exception:
                    pass
        threading.Thread(target=run, daemon=True).start()
