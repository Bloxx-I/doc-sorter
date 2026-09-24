"""Model catalogue: which local models to recommend for how much memory, plus existence checks.

Names are the ones `ollama pull` / `lms get` understand (checked against the registry / hub). Every entry reads
images. Benchmarked on real invoices (dates + senders): Qwen 3.5 4B 8/8 via OCR in <1 s but unreliable when
reading page images itself ("direct": False), Gemma 4 E4B 8/8 both ways. Gemma 4 26B is the same family, larger.
"""

import json
import re
import subprocess
from urllib import error, request

EMBEDDING = {"ollama": "nomic-embed-text", "lmstudio": "text-embedding-nomic-embed-text-v1.5"}

TIERS = [
    {"id": "small", "label": "Klein", "min_ram": 8, "vision": True, "direct": False,
     "text": "Für Macs mit 8 GB. Sehr schnell über Apple-Texterkennung, zuverlässige Ergebnisse.",
     "ollama": {"model": "qwen3.5:4b", "size": "3,4 GB"},
     "lmstudio": {"model": "qwen/qwen3.5-4b", "size": "≈3 GB"}},
    {"id": "medium", "label": "Mittel", "min_ram": 16, "vision": True, "direct": True,
     "text": "Für Macs mit 16 GB. Liest Dokumente direkt als Bild, sehr zuverlässig.",
     "ollama": {"model": "gemma4:e4b", "size": "9,6 GB"},
     "lmstudio": {"model": "google/gemma-4-e4b", "size": "≈5 GB"}},
    {"id": "large", "label": "Groß", "min_ram": 32, "vision": True, "direct": True,
     "text": "Für Macs mit 32 GB oder mehr. Beste lokale Qualität.",
     "ollama": {"model": "gemma4:26b", "size": "18,6 GB"},
     "lmstudio": {"model": "google/gemma-4-26b-a4b", "size": "≈17 GB"}},
]


def ram_gb():
    try:
        return int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout) / 2**30
    except (ValueError, OSError):
        return 8


def recommended_tier(ram=None):
    ram = ram or ram_gb()
    fitting = [t for t in TIERS if ram >= t["min_ram"]]
    return (fitting or TIERS[:1])[-1]["id"]


def catalogue(provider):
    """Tiers for one provider with the model name, size and whether this Mac has enough memory."""
    ram = ram_gb()
    return {"ram": round(ram), "recommended": recommended_tier(ram), "embedding": EMBEDDING.get(provider),
            "tiers": [{**{k: v for k, v in t.items() if k not in ("ollama", "lmstudio")},
                       **t.get(provider, t["ollama"]), "fits": ram >= t["min_ram"]} for t in TIERS]}


def model_exists(provider, name):
    """Ask the Ollama registry / LM Studio hub whether a model name can be downloaded."""
    name = (name or "").strip()
    if not re.fullmatch(r"[\w.\-/:@]+", name):
        return False
    if provider == "ollama":
        repo, _, tag = name.partition(":")
        repo = repo if "/" in repo else f"library/{repo}"
        url = f"https://registry.ollama.ai/v2/{repo}/manifests/{tag or 'latest'}"
        req = request.Request(url, method="HEAD",
                              headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"})
    else:
        req = request.Request(f"https://lmstudio.ai/models/{name.split('@')[0]}", headers={"User-Agent": "doc-sorter"})
    try:
        with request.urlopen(req, timeout=10) as response:
            return response.status == 200
    except error.HTTPError:
        return False
    except Exception:
        return None   # offline: unknown


def supports_images(endpoint, model):
    """Best effort: does this model read images? (LM Studio type=vlm, Ollama capability 'vision', llama.cpp multimodal)."""
    base = endpoint.url.rstrip("/").removesuffix("/v1")
    headers = {"Authorization": f"Bearer {endpoint.api_key}"} if endpoint.api_key else {}
    try:
        if endpoint.provider == "lmstudio":
            with request.urlopen(request.Request(base + "/api/v0/models", headers=headers), timeout=5) as r:
                return any(m["id"] == model and m.get("type") == "vlm" for m in json.load(r)["data"])
        if endpoint.provider == "ollama":
            req = request.Request(base + "/api/show", data=json.dumps({"model": model}).encode(),
                                  headers={"Content-Type": "application/json"})
            with request.urlopen(req, timeout=5) as r:
                return "vision" in (json.load(r).get("capabilities") or [])
        with request.urlopen(request.Request(endpoint.url.rstrip("/") + "/models", headers=headers), timeout=5) as r:
            data = json.load(r)
        for m in (data.get("models") or []) + (data.get("data") or []):
            if model in (m.get("id"), m.get("model"), m.get("name")):
                return "multimodal" in (m.get("capabilities") or []) or None
    except Exception:
        return None
    return None
