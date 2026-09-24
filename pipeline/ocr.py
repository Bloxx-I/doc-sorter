"""Text extraction: embedded PDF text first, then for scanned pages the chosen engine with fallbacks.

Engines: Apple Vision (built into macOS, ~0.2 s/page), GLM-OCR on an AI endpoint (most accurate,
~8 s/page locally or faster on a remote server), PaddleOCR (separate env), Tesseract (last resort).
"""

import base64
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pymupdf

PROJECT = Path(__file__).resolve().parent.parent
PADDLE_WORKER = PROJECT / "pipeline" / "paddle_worker.py"
MIN_EMBEDDED_CHARS = 40


def glm_ocr_page(png_bytes, endpoint, timeout=300):
    """GLM-OCR through the OpenAI-compatible vision API (LM Studio, Ollama or a remote server)."""
    image = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
    return endpoint.chat([{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": image}},
        {"type": "text", "text": "Text Recognition:"}]}], timeout=timeout, temperature=0, max_tokens=4096)


def vision_available():
    try:
        import Vision  # noqa: F401  (pyobjc-framework-Vision)
        return True
    except ImportError:
        return False


def vision_page(png_bytes):
    """Apple's on-device text recognition (the same engine as Live Text in Photos)."""
    import Vision
    from Foundation import NSData
    data = NSData.dataWithBytes_length_(png_bytes, len(png_bytes))
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setRecognitionLanguages_(["de-DE", "en-US"])
    req.setUsesLanguageCorrection_(True)
    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        raise RuntimeError(str(err))
    return "\n".join(o.topCandidates_(1)[0].string() for o in (req.results() or []))


def paddle_python(config):
    """Python that has PaddleOCR: the dev checkout's paddle-venv, or the installed app's own env."""
    value = config.get("paddle_python", "paddle-venv/bin/python")
    if not value:
        return Path("/nonexistent")
    path = Path(value)
    return path if path.is_absolute() else PROJECT / path


def paddle_ocr_pages(image_paths, python, timeout=600):
    done = subprocess.run([str(python), str(PADDLE_WORKER), *map(str, image_paths)],
                          capture_output=True, text=True, timeout=timeout)
    if done.returncode != 0 or "@@RESULT@@" not in done.stdout:
        raise RuntimeError((done.stderr or "PaddleOCR fehlgeschlagen").strip().splitlines()[-1][:300])
    return json.loads(done.stdout.rsplit("@@RESULT@@", 1)[1])


def _looks_scanned(page, embedded):
    """Mostly one big image and little real text: a scan with a stamp-like text layer."""
    if len(embedded) >= 600:
        return False
    area = abs(page.rect)
    covered = sum(abs(pymupdf.Rect(info["bbox"]) & page.rect) for info in page.get_image_info())
    return area > 0 and covered / area > 0.5


def tesseract_cmd():
    """Finder-launched apps don't get Homebrew in PATH, so look there explicitly."""
    for path in (shutil.which("tesseract"), "/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract"):
        if path and Path(path).is_file():
            return path
    return None


def tesseract_page(image_path):
    import pytesseract
    from PIL import Image
    pytesseract.pytesseract.tesseract_cmd = tesseract_cmd() or "tesseract"
    with Image.open(image_path) as image:
        return pytesseract.image_to_string(image, lang="deu+eng")


ENGINES = ("vision", "glm", "paddle")


def ocr_pipeline(pdf_path, config, progress=None, log=None):
    """Return (text, methods). progress(msg) reports the current step; log(msg) records fallbacks."""
    from pipeline.ai import Endpoint
    progress = progress or (lambda _msg: None)
    log = log or (lambda _msg: None)
    primary = config.get("ocr_mode", "vision")
    order = [primary] + [e for e in ENGINES if e != primary]   # chosen engine first, others as fallback
    endpoint = Endpoint.from_config(config, "ocr")
    pages, methods, scans = [], set(), []
    with pymupdf.open(pdf_path) as document:
        if not document.is_pdf or document.needs_pass:
            raise ValueError("Nur unverschlüsselte PDF-Dateien werden unterstützt")
        # "never" (default): every page is OCR'd – text layers from old scanners are often wrong.
        # "digital": text of born-digital pages (no page-sized scan image) is exact, so it is used as is.
        use_embedded = config.get("embedded_text", "never") == "digital"
        for page in document:
            embedded = page.get_text("text").strip() if use_embedded else ""
            if use_embedded and len(embedded) >= MIN_EMBEDDED_CHARS and not _looks_scanned(page, embedded):
                pages.append(embedded)
                methods.add("PDF-Text")
            else:
                pages.append("")   # a scan's own text layer is never used
                scans.append((len(pages) - 1, page.get_pixmap(dpi=200, alpha=False).tobytes("png")))

    remaining = scans
    for engine in order:
        if not remaining:
            break
        try:
            done = _run_engine(engine, remaining, endpoint, config, progress)
        except Exception as exc:
            log(f"{ENGINE_LABELS[engine]} nicht verfügbar ({exc}); nächste OCR wird versucht")
            continue
        if done is None:
            continue
        recognised = set()
        for (index, _png), text in zip(remaining, done):
            text = text.strip()
            if len(text) > len(pages[index]):
                pages[index] = text
            if len(text) >= MIN_EMBEDDED_CHARS:
                recognised.add(index)
        methods.add(ENGINE_LABELS[engine])
        remaining = [(i, png) for i, png in remaining if i not in recognised]
    if remaining and tesseract_cmd():
        progress("Tesseract")
        with tempfile.TemporaryDirectory(prefix="doc-sorter-") as tmp:
            for index, png in remaining:
                path = Path(tmp) / f"page_{index}.png"
                path.write_bytes(png)
                text = tesseract_page(path).strip()
                if len(text) > len(pages[index]):
                    pages[index] = text
        methods.add("Tesseract")
    elif remaining and not any(pages):
        raise RuntimeError("Keine OCR verfügbar – bitte in den Einstellungen eine Texterkennung einrichten")
    return "\n\n".join(p for p in pages if p).strip(), ", ".join(sorted(methods))


ENGINE_LABELS = {"vision": "Apple Vision", "glm": "GLM-OCR", "paddle": "PaddleOCR"}


def _run_engine(engine, scans, endpoint, config, progress):
    """Texts for the given scanned pages, or None when the engine is not set up here."""
    total = len(scans)
    if engine == "vision":
        if not vision_available():
            return None
        texts = []
        for n, (_i, png) in enumerate(scans, 1):
            progress(f"Apple Vision · Seite {n}/{total}")
            texts.append(vision_page(png))
        return texts
    if engine == "glm":
        if not endpoint.model:
            return None
        texts = []
        for n, (_i, png) in enumerate(scans, 1):
            progress(f"GLM-OCR · Seite {n}/{total}")
            texts.append(glm_ocr_page(png, endpoint))
        return texts
    if engine == "paddle":
        python = paddle_python(config)
        if not python.exists():
            return None
        progress(f"PaddleOCR · {total} Seite(n)")
        with tempfile.TemporaryDirectory(prefix="doc-sorter-") as tmp:
            files = []
            for index, png in scans:
                path = Path(tmp) / f"page_{index}.png"
                path.write_bytes(png)
                files.append(path)
            return paddle_ocr_pages(files, python)
    return None


def ocr_status(config):
    """Availability of each OCR engine for the settings screen."""
    from pipeline.ai import Endpoint
    endpoint = Endpoint.from_config(config, "ocr")
    try:
        glm = endpoint.model in endpoint.models()
    except Exception:
        glm = False
    return {"vision": vision_available(), "glm": glm, "paddle": paddle_python(config).exists(),
            "tesseract": bool(tesseract_cmd())}
