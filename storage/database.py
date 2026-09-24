"""SQLite audit trail, pending review queue, and search data."""

import json
import math
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SortHistoryDB:
    def __init__(self, db_path):
        from pathlib import Path
        self.db_path = str(db_path)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self):
        con = sqlite3.connect(self.db_path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def _init_db(self):
        with self._connect() as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS proposals (
                id INTEGER PRIMARY KEY, source_path TEXT NOT NULL, source_size INTEGER NOT NULL,
                source_mtime_ns INTEGER NOT NULL, filename TEXT NOT NULL, sender TEXT,
                document_type TEXT, keyword TEXT, date_extracted TEXT, ocr_text TEXT NOT NULL,
                ocr_method TEXT, status TEXT NOT NULL DEFAULT 'pending', error TEXT,
                created_at TEXT NOT NULL, decided_at TEXT
            );
            CREATE INDEX IF NOT EXISTS proposals_source ON proposals(source_path, status);
            CREATE TABLE IF NOT EXISTS placements (
                id INTEGER PRIMARY KEY, proposal_id INTEGER REFERENCES proposals(id),
                original_filename TEXT NOT NULL, source_path TEXT NOT NULL,
                destination_path TEXT NOT NULL, sender TEXT, document_type TEXT,
                keyword TEXT, date_extracted TEXT, ocr_text TEXT, approved_at TEXT NOT NULL,
                embedding TEXT
            );
            CREATE INDEX IF NOT EXISTS placements_date ON placements(approved_at DESC);
            CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, at TEXT NOT NULL, action TEXT NOT NULL,
                source_path TEXT, destination_path TEXT, detail TEXT
            );
            """)
            already = con.execute("SELECT 1 FROM migrations WHERE name='legacy_sort_log' LIMIT 1").fetchone()
            legacy = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sort_log'").fetchone()
            if legacy and not already:
                con.execute("""INSERT INTO placements(original_filename,source_path,destination_path,
                    sender,document_type,keyword,date_extracted,ocr_text,approved_at)
                    SELECT original_filename,source_path,destination_path,sender,document_type,keyword,
                    date_extracted,ocr_text,sorted_at FROM sort_log WHERE destination_path IS NOT NULL""")
                con.execute("INSERT INTO migrations(name) VALUES('legacy_sort_log')")
            columns = {row["name"] for row in con.execute("PRAGMA table_info(proposals)")}
            if "embedding" not in columns:
                con.execute("ALTER TABLE proposals ADD COLUMN embedding TEXT")

    def event(self, action, source=None, destination=None, detail=None):
        with self._connect() as con:
            con.execute("INSERT INTO events(at,action,source_path,destination_path,detail) VALUES(?,?,?,?,?)",
                        (now(), action, source, destination, detail))

    def has_open_source(self, source):
        with self._connect() as con:
            return con.execute("SELECT 1 FROM proposals WHERE source_path=? AND status='pending' LIMIT 1",
                               (str(source),)).fetchone() is not None

    def was_skipped(self, source, size, mtime_ns):
        """A skipped file stays skipped until it changes."""
        with self._connect() as con:
            return con.execute("""SELECT 1 FROM proposals WHERE source_path=? AND status='skipped'
                AND source_size=? AND source_mtime_ns=? LIMIT 1""", (str(source), size, mtime_ns)).fetchone() is not None

    def set_proposal_embedding(self, proposal_id, embedding):
        with self._connect() as con:
            con.execute("UPDATE proposals SET embedding=? WHERE id=?", (json.dumps(embedding), proposal_id))

    def update_proposal(self, proposal_id, fields):
        allowed = {"filename", "sender", "document_type", "keyword", "date_extracted"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        with self._connect() as con:
            con.execute(f"UPDATE proposals SET {', '.join(k + '=?' for k in fields)} WHERE id=?",
                        (*fields.values(), proposal_id))

    def destination_paths(self):
        with self._connect() as con:
            return [row[0] for row in con.execute("SELECT DISTINCT destination_path FROM placements")]

    def reset_candidates(self):
        """Every filed document: its proposal, placements and original location."""
        with self._connect() as con:
            rows = con.execute("""SELECT proposal_id, original_filename, source_path, destination_path
                FROM placements ORDER BY id""").fetchall()
        grouped = {}
        for row in rows:
            key = row["proposal_id"] if row["proposal_id"] is not None else ("legacy", row["source_path"])
            entry = grouped.setdefault(key, {"original_filename": row["original_filename"],
                                             "source_path": row["source_path"], "destinations": []})
            entry["destinations"].append(row["destination_path"])
        return list(grouped.values())

    def created_folders(self):
        with self._connect() as con:
            return [row[0] for row in con.execute(
                "SELECT destination_path FROM events WHERE action='folder' AND destination_path IS NOT NULL ORDER BY id DESC")]

    def wipe(self):
        """Forget all proposals, placements and events (the files themselves are handled by the caller)."""
        with self._connect() as con:
            con.execute("DELETE FROM placements")
            con.execute("DELETE FROM proposals")
            con.execute("DELETE FROM events")

    def known_senders(self, limit=40):
        with self._connect() as con:
            rows = con.execute("""SELECT sender, COUNT(*) n FROM placements WHERE sender IS NOT NULL AND sender<>''
                GROUP BY sender ORDER BY n DESC LIMIT ?""", (limit,)).fetchall()
            return [row["sender"] for row in rows]

    def placements_for(self, proposal_id):
        with self._connect() as con:
            rows = con.execute("SELECT * FROM placements WHERE proposal_id=? ORDER BY id", (proposal_id,)).fetchall()
            return [dict(row) for row in rows]

    def undo_placements(self, proposal_id, restored_path, stat):
        """Forget the placements and reopen the proposal for the file that is back in the inbox."""
        with self._connect() as con:
            con.execute("DELETE FROM placements WHERE proposal_id=?", (proposal_id,))
            con.execute("""UPDATE proposals SET status='pending', decided_at=NULL, source_path=?,
                source_size=?, source_mtime_ns=? WHERE id=?""",
                        (restored_path, stat.st_size, stat.st_mtime_ns, proposal_id))
            con.execute("INSERT INTO events(at,action,source_path,destination_path,detail) VALUES(?,?,?,?,?)",
                        (now(), "undone", restored_path, None, "Ablage rückgängig gemacht"))

    def neighbours(self, embedding, destination_of, limit=8):
        """Placements whose text is most similar to the given embedding.
        destination_of(folder) maps a folder to its destination key, or None to ignore it."""
        from pathlib import Path
        with self._connect() as con:
            rows = con.execute("""SELECT destination_path, original_filename, embedding FROM placements
                WHERE embedding IS NOT NULL""").fetchall()
        scored = []
        for row in rows:
            vector = json.loads(row["embedding"])
            if len(vector) != len(embedding):
                continue
            dot = sum(a * b for a, b in zip(vector, embedding))
            norm = math.sqrt(sum(a * a for a in vector) * sum(b * b for b in embedding))
            parent = Path(row["destination_path"]).parent
            key = destination_of(parent) if norm and parent.is_dir() else None
            if key is not None:
                scored.append((dot / norm, key, Path(row["destination_path"]).name))
        scored.sort(reverse=True)
        return scored[:limit]

    def add_proposal(self, source, stat, info, text, method):
        with self._connect() as con:
            cur = con.execute("""INSERT INTO proposals(source_path,source_size,source_mtime_ns,
                filename,sender,document_type,keyword,date_extracted,ocr_text,ocr_method,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (str(source), stat.st_size, stat.st_mtime_ns, info["filename"], info.get("sender"),
                 info.get("type"), info.get("keyword"), info.get("date"), text, method, now()))
            return cur.lastrowid

    def pending(self):
        with self._connect() as con:
            rows = con.execute("SELECT * FROM proposals WHERE status='pending' ORDER BY id").fetchall()
            return [dict(row) for row in rows]

    def get_proposal(self, proposal_id):
        with self._connect() as con:
            row = con.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            return dict(row) if row else None

    def decide(self, proposal_id, status, error=None):
        with self._connect() as con:
            con.execute("UPDATE proposals SET status=?,error=?,decided_at=? WHERE id=?",
                        (status, error, now(), proposal_id))

    def log_placements(self, proposal, paths, embedding=None):
        with self._connect() as con:
            for path in paths:
                con.execute("""INSERT INTO placements(proposal_id,original_filename,source_path,
                    destination_path,sender,document_type,keyword,date_extracted,ocr_text,approved_at,embedding)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (proposal["id"], os.path.basename(proposal["source_path"]),
                     proposal["source_path"], path, proposal["sender"], proposal["document_type"],
                     proposal["keyword"], proposal["date_extracted"], proposal["ocr_text"], now(),
                     json.dumps(embedding) if embedding else None))
                con.execute("INSERT INTO events(at,action,source_path,destination_path,detail) VALUES(?,?,?,?,?)",
                            (now(), "placed", proposal["source_path"], path, "Benutzer bestätigt"))
            con.execute("UPDATE proposals SET status='approved',decided_at=? WHERE id=?", (now(), proposal["id"]))

    def update_embeddings(self, proposal_id, embedding):
        with self._connect() as con:
            con.execute("UPDATE placements SET embedding=? WHERE proposal_id=?",
                        (json.dumps(embedding), proposal_id))

    def unindexed(self):
        with self._connect() as con:
            rows = con.execute("""SELECT id,sender,document_type,keyword,ocr_text FROM placements
                WHERE embedding IS NULL AND ocr_text IS NOT NULL AND length(ocr_text)>0""").fetchall()
            return [dict(row) for row in rows]

    def update_embedding(self, placement_id, embedding):
        with self._connect() as con:
            con.execute("UPDATE placements SET embedding=? WHERE id=?", (json.dumps(embedding), placement_id))

    def history(self, limit=100):
        with self._connect() as con:
            rows = con.execute("""SELECT id,proposal_id,original_filename,source_path,destination_path,sender,
                document_type,keyword,date_extracted,approved_at FROM placements ORDER BY id DESC LIMIT ?""",
                (limit,)).fetchall()
            return [dict(row) for row in rows]

    def events(self, limit=100):
        with self._connect() as con:
            rows = con.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(row) for row in rows]

    def destinations_for(self, info, destination_of):
        """Folders where documents with the same sender/type/keyword were filed before."""
        from pathlib import Path
        wanted = {key: str(info.get(key) or "").casefold() for key in ("sender", "type", "keyword")}
        scored = {}
        with self._connect() as con:
            rows = con.execute("SELECT sender,document_type,keyword,destination_path FROM placements").fetchall()
        for row in rows:
            path = Path(row["destination_path"]).parent
            relative = destination_of(path) if path.is_dir() else None
            if relative is None:
                continue
            hits = [label for key, column, label in (("sender", "sender", "Absender"),
                                                       ("type", "document_type", "Art"),
                                                       ("keyword", "keyword", "Stichwort"))
                    if row[column] and wanted[key] and row[column].casefold() == wanted[key]]
            weight = sum({"Absender": 5, "Art": 2, "Stichwort": 3}[h] for h in hits)
            if weight:
                entry = scored.setdefault(relative, {"score": 0, "count": 0, "hits": set()})
                entry["score"] += weight
                entry["count"] += 1
                entry["hits"].update(hits)
        ranked = sorted(scored.items(), key=lambda item: -item[1]["score"])[:4]
        return [(path, data["score"], data["count"], sorted(data["hits"])) for path, data in ranked]

    def search(self, query, embedding=None, limit=50):
        """Hybrid search: cosine similarity of embeddings plus a bonus per matching query word."""
        with self._connect() as con:
            rows = con.execute("SELECT * FROM placements ORDER BY id DESC").fetchall()
        results = []
        terms = [t for t in query.casefold().split() if len(t) > 1]
        for row in rows:
            item = dict(row)
            haystack = " ".join(str(item.get(k) or "") for k in
                                ("original_filename", "destination_path", "sender", "document_type", "keyword", "ocr_text")).casefold()
            lexical = sum(1 for term in terms if term in haystack)
            semantic = 0.0
            if embedding and item["embedding"]:
                vector = json.loads(item["embedding"])
                if len(vector) == len(embedding):
                    dot = sum(a*b for a,b in zip(vector, embedding))
                    norm = math.sqrt(sum(a*a for a in vector) * sum(b*b for b in embedding))
                    semantic = dot / norm if norm else 0
            item.pop("ocr_text", None)
            item.pop("embedding", None)
            item["lexical"] = lexical
            item["score"] = round(semantic + 0.08 * lexical, 3) if embedding else lexical
            results.append(item)
        if embedding:
            top = max((r["score"] for r in results), default=0)
            results = [r for r in results if r["lexical"] or (r["score"] >= 0.45 and r["score"] >= top - 0.1)]
        else:
            results = [r for r in results if r["lexical"]]
        results.sort(key=lambda item: item["score"], reverse=True)
        return results[:limit]
