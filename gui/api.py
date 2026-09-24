"""Bridge between the web frontend and DocumentService.

Every public method is callable from JavaScript (pywebview js_api, or /api/<name> in browser mode)
and returns JSON-serialisable data. Private attributes keep pywebview from exposing internals.
"""

import subprocess
import threading
from pathlib import Path

ICLOUD = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
SETTING_KEYS = ("incoming_dirs", "output_dir", "ocr_mode", "endpoints")


class Api:
    def __init__(self, service):
        self._service = service
        self._window = None
        self._dialog_lock = threading.Lock()

    def _attach(self, window):
        self._window = window

    # ------------------------------------------------------------ inbox
    def state(self):
        db = self._service.db
        pending = [{"id": p["id"], "name": Path(p["source_path"]).name, "filename": p["filename"],
                    "sender": p["sender"], "type": p["document_type"], "date": p["date_extracted"],
                    "keyword": p["keyword"], "method": p["ocr_method"], "created": p["created_at"]}
                   for p in db.pending()]
        errors = [e for e in db.events(20) if e["action"] == "error"][:3]
        paused = self._service.paused
        until = self._service.paused_until
        return {"pending": pending, "processing": self._service.processing(), "errors": errors,
                "incoming": self._service.config["incoming_dirs"], "output": self._service.config["output_dir"],
                "paused": paused, "paused_until": None if not paused or until == float("inf") else until}

    def proposal(self, proposal_id):
        proposal = self._service.db.get_proposal(proposal_id)
        if not proposal:
            return None
        suggestions = self._service.suggestions(proposal)
        proposal.pop("embedding", None)
        proposal["name"] = Path(proposal["source_path"]).name
        return {"proposal": proposal, "suggestions": suggestions, "tree": self._service.folder_tree(),
                "external": self._service.external_folders()}

    def page(self, proposal_id, number=0):
        return self._service.page_image(proposal_id, int(number))

    def filename_for(self, metadata):
        return self._service.rename_preview(metadata)

    def approve(self, proposal_id, filename, destinations, metadata=None):
        paths = self._service.approve(proposal_id, filename, destinations, metadata)
        return {"paths": paths, "proposal_id": proposal_id}

    def skip(self, proposal_id):
        self._service.reject(proposal_id)
        return True

    def reanalyze(self, proposal_id):
        self._service.reanalyze(proposal_id)
        return True

    def undo(self, proposal_id):
        return self._service.undo(proposal_id)

    def scan(self):
        self._service.scan(force=True)   # an explicit click also works while paused
        return True

    def pause(self, minutes=None):
        self._service.pause(None if minutes in (None, 0) else int(minutes))
        return True

    def resume(self):
        self._service.resume()
        return True

    def pick_destination(self, start=""):
        """Finder dialog (with 'New Folder'): returns the destination key for the chosen folder."""
        start_dir = Path(start) if start and Path(start).is_absolute() else self._service.root / (start or ".")
        while not start_dir.is_dir() and start_dir != start_dir.parent:   # planned folders don't exist yet
            start_dir = start_dir.parent
        chosen = self.choose_folder(str(start_dir))
        if not chosen:
            return None
        self._service.resolve_destination(chosen)   # rejects inbox folders
        destination = self._service.as_destination(chosen)
        return {"path": destination, "external": Path(destination).is_absolute(), "tree": self._service.folder_tree()}

    # ------------------------------------------------------------ archive / search / log
    def tree(self):
        return self._service.folder_tree()

    def folder_documents(self, relative):
        return self._service.folder_documents(relative)

    def history(self, limit=300):
        rows = self._service.db.history(limit)
        for row in rows:
            row["relative"] = self._relative(Path(row["destination_path"]).parent)
            row["exists"] = Path(row["destination_path"]).exists()
        return rows

    def _relative(self, folder):
        try:
            relative = str(Path(folder).resolve().relative_to(self._service.root))
            return self._service.root.name if relative == "." else relative
        except ValueError:
            return str(folder)

    def events(self, limit=200):
        return self._service.db.events(limit)

    def search(self, query):
        query = (query or "").strip()
        if not query:
            return {"mode": "leer", "results": []}
        embedding, mode, note = None, "Stichwortsuche", ""
        if self._service.embedding_enabled:
            try:
                embedding = self._service.embedding(query, kind="search_query")
                mode = "Semantische Suche"
            except Exception as exc:
                note = f"Embedding-Modell nicht erreichbar ({exc})"
        results = self._service.db.search(query, embedding)
        for result in results:
            result["exists"] = Path(result["destination_path"]).exists()
            result["relative"] = self._relative(Path(result["destination_path"]).parent)
        return {"mode": mode, "note": note, "results": results}

    # ------------------------------------------------------------ settings
    def settings(self):
        config = self._service.config
        return {**{key: config.get(key, "") for key in SETTING_KEYS}, "icloud": str(ICLOUD) if ICLOUD.is_dir() else None,
                "setup_pending": bool(config.get("setup_pending"))}

    def save_settings(self, values):
        self._service.reconfigure({key: values[key] if key in ("incoming_dirs", "endpoints") else str(values[key]).strip()
                                   for key in SETTING_KEYS if key in values})
        self._service.config.pop("setup_pending", None)
        self._service.warm_up()
        return self.settings()

    # ------------------------------------------------------------ AI endpoints & setup wizard
    def providers(self):
        from pipeline.ai import DEFAULT_MODELS, PROVIDERS, installed_providers
        return {"providers": PROVIDERS, "defaults": DEFAULT_MODELS, "installed": installed_providers(),
                "setup_pending": bool(self._service.config.get("setup_pending"))}

    def test_endpoint(self, endpoint, start=True):
        """Connect to an endpoint (starting LM Studio/Ollama if needed) and list its models."""
        from pipeline.ai import Endpoint, is_local
        ep = Endpoint(**endpoint)
        if start:
            ep.ensure_running()
        try:
            models = ep.models(timeout=5)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "models": [], "remote": not is_local(ep.url)}
        return {"ok": True, "models": models, "remote": not is_local(ep.url)}

    def pull_models(self, endpoint, models):
        """Download models into Ollama (streams) or LM Studio (lms get). Progress is polled via pull_status."""
        from pipeline.ai import lms_cli, ollama_pull
        self._pull = {"running": True, "model": "", "status": "", "fraction": None, "error": None, "done": []}

        def run():
            try:
                for model in models:
                    self._pull.update(model=model, status="Starte Download", fraction=None)
                    if endpoint.get("provider") == "ollama":
                        ollama_pull(model, endpoint.get("url"), lambda st, fr: self._pull.update(status=st, fraction=fr))
                    else:
                        cli = lms_cli()
                        if not cli:
                            raise RuntimeError("LM Studio-Kommandozeile (lms) nicht gefunden – LM Studio einmal öffnen")
                        self._pull.update(status="LM Studio lädt …")
                        done = subprocess.run([cli, "get", model, "-y"], capture_output=True, text=True)
                        if done.returncode != 0:
                            raise RuntimeError((done.stderr or done.stdout).strip().splitlines()[-1][:200])
                    self._pull["done"].append(model)
            except Exception as exc:
                self._pull["error"] = str(exc)
            finally:
                self._pull["running"] = False
        threading.Thread(target=run, daemon=True).start()
        return True

    def login_item(self):
        from gui.menubar import login_item_enabled
        return login_item_enabled()

    def set_login_item(self, enabled):
        from gui.menubar import set_login_item
        set_login_item(bool(enabled))
        return self.login_item()

    def pull_status(self):
        return getattr(self, "_pull", {"running": False})

    def health(self):
        return self._service.health()

    def choose_folder(self, current=""):
        if not self._window or not self._dialog_lock.acquire(blocking=False):
            return None   # browser mode, or a dialog is already open
        try:
            import webview
            self._activate()
            start = current if current and Path(current).is_dir() else str(Path.home())
            chosen = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=start)
            return chosen[0] if chosen else None
        finally:
            self._dialog_lock.release()

    @staticmethod
    def _activate():
        """A modal panel of an inactive app stays invisible, so bring the app forward first."""
        try:
            import AppKit
            from PyObjCTools import AppHelper
            AppHelper.callAfter(AppKit.NSApp.activateIgnoringOtherApps_, True)
        except Exception:
            pass

    # ------------------------------------------------------------ finder
    def open_path(self, path):
        if path and Path(path).exists():
            subprocess.run(["open", path], check=False)
            return True
        return False

    def open_url(self, url):
        if str(url).startswith(("https://", "http://")):
            subprocess.run(["open", url], check=False)
            return True
        return False

    def reveal(self, path):
        if path and Path(path).exists():
            subprocess.run(["open", "-R", path], check=False)
            return True
        return False
