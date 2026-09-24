"""AI endpoints: LM Studio, Ollama or any OpenAI-compatible server (e.g. a fast machine at home).

Every task (analysis, OCR, embeddings) has its own endpoint, so the heavy work can be moved to
another computer while the rest stays local. All three providers speak the OpenAI API (/v1/...).
"""

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib import error, request
from urllib.parse import urlparse

PROVIDERS = {
    "lmstudio": {"label": "LM Studio", "url": "http://127.0.0.1:1234/v1"},
    "ollama": {"label": "Ollama", "url": "http://127.0.0.1:11434/v1"},
    "custom": {"label": "Eigener Server", "url": "http://192.168.1.10:1234/v1"},
}

# Sensible model names per provider (Ollama uses its own library names).
def _defaults():
    """Analysis model = the catalogue tier that fits this Mac (Phi is no longer the default)."""
    from pipeline.models import TIERS, recommended_tier
    tier = next(t for t in TIERS if t["id"] == recommended_tier())
    return {
        "lmstudio": {"llm": tier["lmstudio"]["model"], "ocr": "glm-ocr", "embedding": "text-embedding-nomic-embed-text-v1.5"},
        "ollama": {"llm": tier["ollama"]["model"], "ocr": "glm-ocr", "embedding": "nomic-embed-text"},
        "custom": {"llm": "", "ocr": "", "embedding": ""},
    }


DEFAULT_MODELS = _defaults()
TASKS = ("llm", "ocr", "embedding")

_start_lock = threading.Lock()
_last_start = {}


def is_local(url):
    return urlparse(url).hostname in ("127.0.0.1", "localhost", "::1")


class Endpoint:
    def __init__(self, provider="lmstudio", url=None, model="", api_key="", **_):
        self.provider = provider if provider in PROVIDERS else "custom"
        self.url = (url or PROVIDERS[self.provider]["url"]).rstrip("/")
        self.model = model
        self.api_key = api_key or ""

    @classmethod
    def from_config(cls, config, task):
        return cls(**(config.get("endpoints", {}).get(task) or {}))

    # ------------------------------------------------------------ HTTP
    def _post(self, path, payload, timeout):
        self.ensure_running()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = request.Request(self.url + path, data=json.dumps(payload).encode(), headers=headers)
        with request.urlopen(req, timeout=timeout) as response:
            return json.load(response)

    def chat(self, messages, timeout=180, **options):
        payload = {"model": self.model, "messages": messages, "stream": False, **options}
        return self._post("/chat/completions", payload, timeout)["choices"][0]["message"]["content"] or ""

    def embed(self, text, timeout=60):
        return self._post("/embeddings", {"model": self.model, "input": text}, timeout)["data"][0]["embedding"]

    def models(self, timeout=3):
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        req = request.Request(self.url + "/models", headers=headers)
        with request.urlopen(req, timeout=timeout) as response:
            # Ollama answers {"data": null} while no model is installed yet – that still means "connected".
            return [m["id"] for m in (json.load(response).get("data") or [])]

    def reachable(self):
        try:
            self.models(timeout=2)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------ auto start
    def ensure_running(self):
        """Start LM Studio's server / Ollama when the endpoint is local and not answering yet."""
        if self.provider not in ("lmstudio", "ollama") or not is_local(self.url) or self.reachable():
            return
        with _start_lock:
            if self.reachable():
                return
            if time.time() - _last_start.get(self.provider, 0) < 60:   # don't hammer while it boots
                return
            _last_start[self.provider] = time.time()
            start_provider(self.provider)
            for _ in range(45):
                time.sleep(1)
                if self.reachable():
                    return

    def describe(self):
        return f"{PROVIDERS[self.provider]['label']} · {self.url}"


def lms_cli():
    for path in (Path.home() / ".lmstudio" / "bin" / "lms", Path(shutil.which("lms") or "")):
        if path and path.is_file():
            return str(path)
    return None


def start_provider(provider):
    if provider == "lmstudio":
        cli = lms_cli()
        if cli:   # headless: starts the LM Studio server without opening the window
            subprocess.Popen([cli, "server", "start"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif app_path("LM Studio"):
            subprocess.run(["open", "-g", str(app_path("LM Studio"))], capture_output=True)
    elif provider == "ollama":
        if app_path("Ollama"):
            subprocess.run(["open", "-g", str(app_path("Ollama"))], capture_output=True)
        elif ollama_cli():
            subprocess.Popen([ollama_cli(), "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)


def app_path(name):
    """Apps may live in /Applications or, without admin rights, in ~/Applications."""
    for folder in (Path("/Applications"), Path.home() / "Applications"):
        if (folder / f"{name}.app").exists():
            return folder / f"{name}.app"
    return None


def ollama_cli():
    for path in (shutil.which("ollama"), "/usr/local/bin/ollama", "/opt/homebrew/bin/ollama"):
        if path and Path(path).is_file():
            return path
    app = app_path("Ollama")
    bundled = app / "Contents" / "Resources" / "ollama" if app else None
    return str(bundled) if bundled and bundled.is_file() else None


def installed_providers():
    return {"lmstudio": bool(app_path("LM Studio") or lms_cli()),
            "ollama": bool(app_path("Ollama") or ollama_cli())}


def ollama_pull(model, base_url="http://127.0.0.1:11434/v1", progress=None):
    """Download an Ollama model, reporting (status, fraction) while it streams."""
    root = base_url.rstrip("/").removesuffix("/v1")
    req = request.Request(root + "/api/pull", data=json.dumps({"model": model, "stream": True}).encode(),
                          headers={"Content-Type": "application/json"})
    with request.urlopen(req, timeout=3600) as response:
        for line in response:
            info = json.loads(line or b"{}")
            if "error" in info:
                raise RuntimeError(info["error"])
            if progress:
                done, total = info.get("completed"), info.get("total")
                progress(info.get("status", ""), (done / total) if done and total else None)


def endpoints_from_legacy(config):
    """Configs from before per-task endpoints only had lm_base_url + model names."""
    url = config.get("lm_base_url", PROVIDERS["lmstudio"]["url"])
    provider = "lmstudio" if url.startswith("http://127.0.0.1:1234") or url.startswith("http://localhost:1234") else \
        "ollama" if ":11434" in url else "custom"
    return {
        "llm": {"provider": provider, "url": url, "model": config.get("llm_model", DEFAULT_MODELS[provider]["llm"])},
        "ocr": {"provider": provider, "url": url, "model": config.get("ocr_model", DEFAULT_MODELS[provider]["ocr"])},
        "embedding": {"provider": provider, "url": url,
                      "model": config.get("embedding_model", DEFAULT_MODELS[provider]["embedding"])},
    }


__all__ = ["Endpoint", "PROVIDERS", "DEFAULT_MODELS", "TASKS", "installed_providers", "ollama_pull",
           "endpoints_from_legacy", "is_local", "error"]
