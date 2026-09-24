"""Safe, collision-free placement into one or more chosen folders."""

import os
import shutil
import tempfile
from pathlib import Path


def place_document(source, folders, filename):
    """Copy source into every (already validated, existing) folder, then remove the source."""
    source = Path(source).resolve()
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise ValueError("Quelldatei fehlt oder ist kein PDF")
    if not folders:
        raise ValueError("Mindestens ein Zielordner ist erforderlich")
    if Path(filename).name != filename or not filename.lower().endswith(".pdf") or filename.startswith("."):
        raise ValueError("Ungültiger PDF-Dateiname")
    folders = list(dict.fromkeys(Path(f).resolve() for f in folders))
    for folder in folders:
        if not folder.is_dir():
            raise ValueError(f"Zielordner fehlt: {folder}")
    created = []
    try:
        for folder in folders:
            stem = Path(filename).stem
            fd, temp_name = tempfile.mkstemp(prefix=".doc-sorter-", suffix=".tmp", dir=folder)
            try:
                with os.fdopen(fd, "wb") as out, source.open("rb") as inp:
                    shutil.copyfileobj(inp, out)
                    out.flush()
                    os.fsync(out.fileno())
                shutil.copystat(source, temp_name)
                index = 1
                while True:
                    candidate = folder / (filename if index == 1 else f"{stem}__{index}.pdf")
                    try:
                        os.link(temp_name, candidate)
                        break
                    except FileExistsError:
                        index += 1
                created.append(candidate)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
        source.unlink()
        return [str(path) for path in created]
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise
