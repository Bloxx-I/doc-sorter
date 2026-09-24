"""End-to-end checks use temporary folders and a fake local LM Studio endpoint."""

import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pymupdf

from watch.watcher import DocumentService


class ModelHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        size = int(self.headers["Content-Length"])
        json.loads(self.rfile.read(size))
        if self.path.endswith("/embeddings"):
            result = {"data": [{"embedding": [1.0, 0.0, 0.5]}]}
        else:
            result = {"choices": [{"message": {"content": json.dumps({
                "date": "2024-03-15", "sender": "Telekom Deutschland GmbH",
                "type": "Rechnung", "keyword": "Mobilfunkrechnung"})}}]}
        data = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.incoming = root / "Eingang"
        self.output = root / "Dokumente"
        self.incoming.mkdir()
        self.output.mkdir()
        (self.output / "Rechnungen").mkdir()
        (self.output / "Steuern").mkdir()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.config_path = root / "config.json"
        self.config_path.write_text(json.dumps({
            "incoming_dir": str(self.incoming), "output_dir": str(self.output),
            "database": str(root / "history.db"), "ocr_mode": "local",
            "lm_base_url": f"http://127.0.0.1:{self.server.server_port}/v1",
            "llm_model": "fake-phi", "embedding_model": "fake-embedding"}))
        self.service = DocumentService(self.config_path)
        self.service.start()

    def tearDown(self):
        self.service.stop()
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _pdf(self, name):
        path = self.incoming / name
        document = pymupdf.open()
        page = document.new_page()
        page.insert_text((72, 72), "Rechnung vom 15.03.2024 Telekom Deutschland GmbH Mobilfunk " * 3)
        document.save(path)
        document.close()
        self.service.scan()
        for _ in range(100):
            pending = self.service.db.pending()
            if any(p["source_path"] == str(path.resolve()) for p in pending):
                return path, next(p for p in pending if p["source_path"] == str(path.resolve()))
            time.sleep(0.1)
        self.fail("No proposal generated")

    def test_review_multiple_destinations_collision_and_search(self):
        source, proposal = self._pdf("rechnung.pdf")
        self.assertTrue(source.exists(), "Review must leave source untouched")
        self.assertEqual(proposal["filename"], "2024_03_15_Telekom_Deutschland_Mobilfunkrechnung.pdf")
        paths = self.service.approve(proposal["id"], proposal["filename"], ["Rechnungen", "Steuern"])
        self.assertFalse(source.exists())
        self.assertEqual(len(paths), 2)
        self.assertEqual(len(self.service.db.history()), 2)
        for path in paths:
            self.assertTrue(Path(path).is_file())
        source2, proposal2 = self._pdf("zweite.pdf")
        paths2 = self.service.approve(proposal2["id"], proposal2["filename"], ["Rechnungen"])
        self.assertTrue(paths2[0].endswith("__2.pdf"))
        for _ in range(50):
            if any(e["action"] == "indexed" for e in self.service.db.events()):
                break
            time.sleep(0.1)
        self.assertTrue(self.service.db.search("Telefonkosten", [1.0, 0.0, 0.5]))
        self.assertEqual(len(self.service.db.history()), 3)
        self.assertTrue(any(e["action"] == "placed" for e in self.service.db.events()))

    def test_invalid_target_preserves_source(self):
        source, proposal = self._pdf("schutz.pdf")
        with self.assertRaises(ValueError):
            self.service.approve(proposal["id"], proposal["filename"], ["../außerhalb"])
        self.assertTrue(source.exists())

    def test_new_folder_is_created_on_approve_and_undo_reopens(self):
        source, proposal = self._pdf("neu.pdf")
        paths = self.service.approve(proposal["id"], proposal["filename"], ["Rechnungen/2024/03"])
        self.assertTrue((self.output / "Rechnungen" / "2024" / "03").is_dir())
        self.assertTrue(Path(paths[0]).is_file())
        restored = self.service.undo(proposal["id"])
        self.assertTrue(Path(restored).is_file())
        self.assertFalse(Path(paths[0]).exists())
        self.assertEqual(self.service.db.get_proposal(proposal["id"])["status"], "pending")
        self.assertEqual(self.service.db.history(), [])

    def test_suggests_year_folder_for_new_date(self):
        (self.output / "Rechnungen" / "2023" / "11").mkdir(parents=True)
        source, proposal = self._pdf("alt.pdf")
        self.service.approve(proposal["id"], proposal["filename"], ["Rechnungen/2023/11"])
        source2, proposal2 = self._pdf("neuer.pdf")
        suggestions = self.service.suggestions(proposal2)
        top = suggestions[0]
        self.assertEqual(top["path"], "Rechnungen/2024/03")
        self.assertTrue(top["new"])
        self.assertTrue(top["reasons"])

    def test_skipped_file_is_not_proposed_again(self):
        source, proposal = self._pdf("skip.pdf")
        self.service.reject(proposal["id"])
        self.service.scan()
        time.sleep(0.5)
        self.assertEqual(self.service.db.pending(), [])
        self.assertEqual(self.service.processing(), [])

    def test_rejects_hidden_or_escaping_destinations(self):
        source, proposal = self._pdf("evil.pdf")
        for bad in ("../raus", ".versteckt", "Rechnungen/../../raus"):
            with self.assertRaises(ValueError):
                self.service.approve(proposal["id"], proposal["filename"], [bad])
        self.assertTrue(source.exists())

    def test_external_destination_outside_ablage(self):
        other_drive = Path(self.tmp.name) / "Volumes" / "Archiv"
        other_drive.mkdir(parents=True)
        source, proposal = self._pdf("extern.pdf")
        paths = self.service.approve(proposal["id"], proposal["filename"], [str(other_drive), "Rechnungen"])
        self.assertTrue(Path(paths[0]).parent.samefile(other_drive))
        self.assertIn(str(other_drive.resolve()), self.service.external_folders())
        with self.assertRaises(ValueError):   # a picked folder must exist, and inboxes are never targets
            self.service.resolve_destination(str(self.incoming))

    def test_second_inbox_and_undo_returns_there(self):
        second = Path(self.tmp.name) / "Scanner"
        second.mkdir()
        self.service.reconfigure({"incoming_dirs": [str(self.incoming), str(second)], "output_dir": str(self.output)})
        document = pymupdf.open()
        document.new_page().insert_text((72, 72), "Rechnung vom 15.03.2024 Telekom Mobilfunk " * 3)
        document.save(second / "scan.pdf")
        for _ in range(100):
            pending = self.service.db.pending()
            if pending:
                break
            time.sleep(0.1)
        self.assertEqual(Path(pending[0]["source_path"]).parent, second.resolve())
        self.service.approve(pending[0]["id"], pending[0]["filename"], ["Rechnungen"])
        restored = self.service.undo(pending[0]["id"])
        self.assertEqual(Path(restored).parent, second.resolve())

    def test_pause_ignores_new_files_until_resumed(self):
        self.service.pause(15)
        self.assertTrue(self.service.paused)
        document = pymupdf.open()
        document.new_page().insert_text((72, 72), "Rechnung vom 15.03.2024 Telekom Mobilfunk " * 3)
        document.save(self.incoming / "waehrend_pause.pdf")
        time.sleep(1.5)
        self.assertEqual(self.service.db.pending(), [])
        self.assertEqual(self.service.processing(), [])
        self.service.paused_until = time.time() - 1   # the 15 minutes are over
        self.assertFalse(self.service.paused)
        for _ in range(100):
            if self.service.db.pending():
                break
            time.sleep(0.1)
        self.assertEqual(len(self.service.db.pending()), 1)

    def test_reset_all_restores_documents_and_history(self):
        source, proposal = self._pdf("eins.pdf")
        paths = self.service.approve(proposal["id"], proposal["filename"], ["Rechnungen", "Neu/2026"])
        source2, proposal2 = self._pdf("zwei.pdf")
        self.service.reject(proposal2["id"])
        self.assertFalse(source.exists())
        self.assertEqual(self.service.reset_preview()["restorable"], 1)
        result = self.service.reset_all()
        self.assertEqual(result["restored"], 1)
        self.assertTrue(source.exists(), "document is back under its original name")
        self.assertFalse(any(Path(p).exists() for p in paths), "all copies are gone")
        self.assertFalse((self.output / "Neu").exists(), "folders the app created are removed when empty")
        self.assertTrue((self.output / "Rechnungen").exists(), "pre-existing folders stay")
        self.assertEqual(self.service.db.history(), [])
        for _ in range(150):   # both documents are analysed again, the skipped one included
            if len(self.service.db.pending()) == 2:
                break
            time.sleep(0.1)
        self.assertEqual(sorted(Path(p["source_path"]).name for p in self.service.db.pending()), ["eins.pdf", "zwei.pdf"])


class NormaliseSenderTest(unittest.TestCase):
    def test_legal_forms_and_known_senders(self):
        from pipeline.classifier import normalise_sender
        self.assertEqual(normalise_sender("Telekom Deutschland GmbH", ["Telekom"]), "Telekom")
        self.assertEqual(normalise_sender("Muster GmbH & Co. KG"), "Muster")
        self.assertEqual(normalise_sender("HUK-Coburg AG"), "HUK-Coburg")
        self.assertIsNone(normalise_sender(None))



class EndpointTest(unittest.TestCase):
    def test_ollama_without_models_counts_as_connected(self):
        """Fresh Ollama answers {"data": null} – the wizard must still see a working connection."""
        from pipeline.ai import Endpoint

        class Empty(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"object": "list", "data": null}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Empty)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            endpoint = Endpoint(provider="custom", url=f"http://127.0.0.1:{server.server_port}/v1")
            self.assertEqual(endpoint.models(), [])
            self.assertTrue(endpoint.reachable())
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
