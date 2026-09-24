#!/usr/bin/env python3
"""Start the document sorter: native window + menu bar icon (--hidden: menu bar only, --browser: web UI)."""

import json
import socket
import subprocess
import sys
import threading
from pathlib import Path

from gui.api import Api
from watch.watcher import DocumentService

ROOT = Path(__file__).resolve().parent


def style_native_window(window):
    """Unified title bar: content runs underneath the traffic lights, like Finder or Mail."""
    try:
        import AppKit
        from PyObjCTools import AppHelper

        def apply():
            ns = window.native
            ns.setStyleMask_(ns.styleMask() | AppKit.NSWindowStyleMaskFullSizeContentView)
            ns.setTitlebarAppearsTransparent_(True)
            ns.setTitleVisibility_(AppKit.NSWindowTitleHidden)
            ns.setHasShadow_(True)
            ns.setMovableByWindowBackground_(False)
            # pywebview paints the title bar opaque; clear it so the web content shows through
            titlebar = ns.standardWindowButton_(AppKit.NSWindowCloseButton).superview()
            container = ns.contentView().superview().subviews().lastObject()
            for view in (titlebar, titlebar.superview(), container):
                if view is not None and view.respondsToSelector_("setBackgroundColor:"):
                    view.setBackgroundColor_(AppKit.NSColor.clearColor())
                if view is not None:
                    view.setWantsLayer_(True)
                    view.layer().setBackgroundColor_(None)
        AppHelper.callAfter(apply)
    except Exception as exc:  # cosmetic only
        print("Fensterstil nicht angewendet:", exc)


def notify(title, text):
    script = f'display notification {json_str(text)} with title {json_str(title)}'
    subprocess.run(["osascript", "-e", script], capture_output=True, check=False)


def json_str(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def watch_for_new_documents(service, menubar, stop):
    """Pops the window up (and posts a notification) whenever a new proposal is ready."""
    seen = {p["id"] for p in service.db.pending()}
    while not stop.wait(1.5):
        pending = service.db.pending()
        new = [p for p in pending if p["id"] not in seen]
        seen = {p["id"] for p in pending}
        if new:
            menubar.show("inbox")
            notify("Neues Dokument", f"{new[0]['filename']} wartet auf Ablage")


SUPPORT = Path.home() / "Library" / "Application Support" / "Dokumenten-Sortierer"


def config_path():
    """--config wins; a development checkout uses ./config.json; an installed app keeps its settings
    and history in Application Support so updates never touch them."""
    if "--config" in sys.argv:
        return Path(sys.argv[sys.argv.index("--config") + 1])
    if (ROOT / "config.json").exists():
        return ROOT / "config.json"
    path = SUPPORT / "config.json"
    if not path.exists():
        SUPPORT.mkdir(parents=True, exist_ok=True)
        documents = Path.home() / "Documents" / "Dokumente"
        path.write_text(json.dumps({
            "incoming_dirs": [str(documents / "Eingang")], "output_dir": str(documents),
            "database": str(SUPPORT / "sort_history.db"), "ocr_mode": "vision",
            "paddle_python": sys.executable if _has_paddle() else "",
            "endpoints": default_endpoints(), "setup_pending": True}, indent=2, ensure_ascii=False))
    return path


def _has_paddle():
    import importlib.util
    return importlib.util.find_spec("paddleocr") is not None


def default_endpoints():
    from pipeline.ai import DEFAULT_MODELS, PROVIDERS, installed_providers
    provider = "lmstudio" if installed_providers()["lmstudio"] or not installed_providers()["ollama"] else "ollama"
    return {task: {"provider": provider, "url": PROVIDERS[provider]["url"], "model": DEFAULT_MODELS[provider][task],
                   "api_key": ""} for task in ("llm", "ocr", "embedding")}


INSTANCE_PORT = 47631  # a running sorter listens here; a second launch just asks it to show its window


def signal_running_instance():
    try:
        with socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=0.5) as conn:
            conn.sendall(b"show")
        return True
    except OSError:
        return False


def listen_for_second_launch(menubar):
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", INSTANCE_PORT))
    server.listen()

    def loop():
        while True:
            conn, _ = server.accept()
            with conn:
                if conn.recv(16) == b"show":
                    menubar.show()
    threading.Thread(target=loop, daemon=True).start()


def main():
    if "--browser" not in sys.argv and signal_running_instance():
        return 0
    service = DocumentService(config_path())
    api = Api(service)
    service.start()
    if "--browser" in sys.argv:
        server = serve_browser(api)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            service.stop()
        return 0

    import webview
    from gui.menubar import MenuBar, name_process
    name_process()
    hidden = "--hidden" in sys.argv   # login item: start in the menu bar only
    window = webview.create_window(
        "Dokumenten-Sortierer", url=str(ROOT / "gui" / "web" / "index.html"), js_api=api,
        width=1480, height=940, min_size=(1100, 700), background_color="#f4f4f6",
        transparent=True, vibrancy=True, text_select=True, hidden=hidden)
    api._attach(window)
    menubar = MenuBar(window, service)
    stop = threading.Event()
    window.events.shown += lambda: style_native_window(window)
    window.events.closing += menubar.on_closing   # red button hides, the app keeps watching
    window.events.closed += stop.set
    threading.Thread(target=watch_for_new_documents, args=(service, menubar, stop), daemon=True).start()
    listen_for_second_launch(menubar)
    try:
        webview.start(menubar.install, debug="--debug" in sys.argv)
    finally:
        stop.set()
        service.stop()
    return 0


def serve_browser(api):
    from gui.server import serve
    return serve(api)


if __name__ == "__main__":
    raise SystemExit(main())
