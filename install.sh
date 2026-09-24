#!/bin/bash
# Dokumenten-Sortierer – Installer für macOS
#
#   curl -fsSL https://raw.githubusercontent.com/Bloxx-I/doc-sorter/main/install.sh | bash
#
# Nochmal ausführen = Update (Einstellungen und Verlauf bleiben erhalten).
# Optionen (nach "bash -s --"):  --with-paddle   PaddleOCR zusätzlich installieren (~1 GB)
#                                --ollama | --lmstudio | --server   KI-Quelle ohne Nachfrage wählen
#                                --uninstall     App entfernen (Einstellungen bleiben)
set -euo pipefail

REPO="Bloxx-I/doc-sorter"
BRANCH="main"
APP_NAME="Dokumenten-Sortierer"
SUPPORT="$HOME/Library/Application Support/$APP_NAME"
APP_DIR="$SUPPORT/app"
VENV="$SUPPORT/venv"
BUNDLE="$HOME/Applications/$APP_NAME.app"
UV="$HOME/.local/bin/uv"

bold=$(tput bold 2>/dev/null || true); dim=$(tput dim 2>/dev/null || true); reset=$(tput sgr0 2>/dev/null || true)
step() { printf "\n${bold}▸ %s${reset}\n" "$1"; }
info() { printf "  %s\n" "$1"; }
fail() { printf "\n${bold}✗ %s${reset}\n" "$1" >&2; exit 1; }
ask() {  # read from the keyboard even when the script comes through "curl | bash"
  local answer=""
  if [ -r /dev/tty ]; then read -r -p "  $1 " answer < /dev/tty || true; fi
  printf "%s" "$answer"
}

WITH_PADDLE=0; PROVIDER=""
for arg in "$@"; do
  case "$arg" in
    --with-paddle) WITH_PADDLE=1 ;;
    --ollama) PROVIDER=ollama ;;
    --lmstudio) PROVIDER=lmstudio ;;
    --server) PROVIDER=custom ;;
    --uninstall)
      pkill -f "$APP_DIR/main.py" 2>/dev/null || true
      rm -rf "$BUNDLE" "$APP_DIR" "$VENV" "$HOME/Library/LaunchAgents/local.docsorter.mac.plist"
      echo "Entfernt. Einstellungen und Verlauf liegen weiter in: $SUPPORT"
      exit 0 ;;
  esac
done

[ "$(uname -s)" = "Darwin" ] || fail "Dieser Installer ist nur für macOS."
MACOS_MAJOR=$(sw_vers -productVersion | cut -d. -f1)
[ "$MACOS_MAJOR" -ge 13 ] || fail "Benötigt macOS 13 (Ventura) oder neuer."

cat <<EOF

${bold}📄  Dokumenten-Sortierer${reset}
${dim}Liest PDFs, benennt sie sinnvoll und sortiert sie in deine Ordner.${reset}
EOF

# ------------------------------------------------------------------ 1. uv (Python-Verwaltung, ohne Admin)
step "Werkzeuge vorbereiten"
if [ ! -x "$UV" ] && ! command -v uv >/dev/null 2>&1; then
  info "Installiere uv (verwaltet Python, landet in ~/.local/bin) …"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null 2>&1 || fail "uv konnte nicht installiert werden (Internet?)."
fi
[ -x "$UV" ] || UV="$(command -v uv)"
info "uv bereit"

# ------------------------------------------------------------------ 2. Programm herunterladen
step "Programm herunterladen"
pkill -f "$APP_DIR/main.py" 2>/dev/null || true   # a running old version would keep old code
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
curl -fsSL "https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz" | tar -xz -C "$TMP" \
  || fail "Download von GitHub fehlgeschlagen."
mkdir -p "$SUPPORT"
rm -rf "$APP_DIR"
mv "$TMP"/*-"$BRANCH" "$APP_DIR"
rm -f "$APP_DIR/config.json"   # settings live in Application Support, never in the code folder
info "nach $APP_DIR"

# ------------------------------------------------------------------ 3. Python-Umgebung
step "Python-Umgebung einrichten (einmalig 1–3 Minuten)"
"$UV" venv --quiet --allow-existing --python 3.12 "$VENV"
"$UV" pip install --quiet --python "$VENV/bin/python" -r "$APP_DIR/requirements.txt"
if [ "$WITH_PADDLE" = 1 ]; then
  info "PaddleOCR (optional, groß) …"
  "$UV" pip install --quiet --python "$VENV/bin/python" -r "$APP_DIR/requirements-paddle.txt"
fi
info "fertig"

# ------------------------------------------------------------------ 4. App im Programme-Ordner
step "App anlegen"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"
cat > "$BUNDLE/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>local.docsorter.mac</string>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>$APP_NAME</string>
  <key>CFBundleExecutable</key><string>launcher</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
EOF
cat > "$BUNDLE/Contents/MacOS/launcher" <<EOF
#!/bin/zsh
cd "$APP_DIR" || exit 1
exec "$VENV/bin/python" "$APP_DIR/main.py" "\$@"
EOF
chmod +x "$BUNDLE/Contents/MacOS/launcher"
[ -f "$APP_DIR/assets/AppIcon.icns" ] && cp "$APP_DIR/assets/AppIcon.icns" "$BUNDLE/Contents/Resources/"
touch "$BUNDLE"
info "$BUNDLE"

# ------------------------------------------------------------------ 5. KI-Quelle
if [ -f "$SUPPORT/config.json" ] && ! grep -q '"setup_pending": true' "$SUPPORT/config.json"; then
  step "Update fertig 🎉"
  info "Deine Einstellungen und dein Verlauf sind erhalten geblieben."
  [ -n "${DOCSORTER_NO_LAUNCH:-}" ] || open "$BUNDLE"
  exit 0
fi
step "KI-Quelle"
HAS_LMS=0; HAS_OLLAMA=0
[ -d "/Applications/LM Studio.app" ] && HAS_LMS=1
{ [ -d "/Applications/Ollama.app" ] || command -v ollama >/dev/null 2>&1; } && HAS_OLLAMA=1
if [ -z "$PROVIDER" ]; then
  echo   "  Womit soll die KI rechnen?"
  echo   "    1) Ollama     – schlank, läuft unsichtbar im Hintergrund $( [ $HAS_OLLAMA = 1 ] && echo '(installiert)' || echo '(wird installiert)')"
  echo   "    2) LM Studio  – App mit Oberfläche $( [ $HAS_LMS = 1 ] && echo '(installiert)' || echo '(Download-Seite öffnet sich)')"
  echo   "    3) Eigener Server im Netzwerk – Adresse gibst du gleich in der App ein"
  choice=$(ask "Auswahl [1]:")
  case "$choice" in 2) PROVIDER=lmstudio ;; 3) PROVIDER=custom ;; *) PROVIDER=ollama ;; esac
fi

if [ "$PROVIDER" = "ollama" ] && [ $HAS_OLLAMA = 0 ]; then
  info "Lade Ollama …"
  curl -fsSL -o "$TMP/Ollama.dmg" "https://ollama.com/download/Ollama.dmg" || fail "Ollama-Download fehlgeschlagen."
  MNT=$(hdiutil attach -nobrowse -readonly "$TMP/Ollama.dmg" | tail -1 | awk -F'\t' '{print $NF}')
  if ! cp -R "$MNT/Ollama.app" /Applications/ 2>/dev/null; then
    mkdir -p "$HOME/Applications" && cp -R "$MNT/Ollama.app" "$HOME/Applications/"
  fi
  hdiutil detach -quiet "$MNT" || true
  open -g -a Ollama || true
  info "Ollama installiert"
elif [ "$PROVIDER" = "lmstudio" ] && [ $HAS_LMS = 0 ]; then
  info "Bitte LM Studio installieren – die Download-Seite öffnet sich."
  open "https://lmstudio.ai/download" || true
fi
info "Modelle und Ordner richtest du gleich im Assistenten ein."

# Vorauswahl für den Einrichtungsassistenten
"$VENV/bin/python" - "$SUPPORT" "$PROVIDER" <<'PY'
import json, sys
from pathlib import Path
support, provider = Path(sys.argv[1]), sys.argv[2]
sys.path.insert(0, str(support / "app"))
from pipeline.ai import DEFAULT_MODELS, PROVIDERS
path = support / "config.json"
if path.exists():
    config = json.loads(path.read_text())
    if not config.get("setup_pending"):
        sys.exit(0)   # already set up: an update never touches the user's choices
else:
    docs = Path.home() / "Documents" / "Dokumente"
    config = {"incoming_dirs": [str(docs / "Eingang")], "output_dir": str(docs),
              "database": str(support / "sort_history.db"), "ocr_mode": "vision", "paddle_python": ""}
try:
    import paddleocr  # noqa: F401
    config["paddle_python"] = sys.executable
except ImportError:
    pass
config["endpoints"] = {task: {"provider": provider, "url": PROVIDERS[provider]["url"],
                              "model": DEFAULT_MODELS[provider][task], "api_key": ""} for task in ("llm", "ocr", "embedding")}
config["setup_pending"] = True
path.write_text(json.dumps(config, indent=2, ensure_ascii=False))
PY

# ------------------------------------------------------------------ 6. Start
step "Fertig 🎉"
info "Der Sortierer startet jetzt und führt dich durch die Einrichtung."
info "Später findest du ihn unter ~/Programme/$APP_NAME und oben in der Menüleiste."
[ -n "${DOCSORTER_NO_LAUNCH:-}" ] || open "$BUNDLE"
