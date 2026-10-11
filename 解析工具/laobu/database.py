import json
import sqlite3
from pathlib import Path
from . import SCHEMA_VERSION
from .common import now, parse_title, sha256

DDL = """
CREATE TABLE IF NOT EXISTS series(id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS videos(
 id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
 original_filename TEXT NOT NULL, path TEXT NOT NULL, series_id TEXT REFERENCES series(id),
 episode INTEGER, total_episodes INTEGER, metadata_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', current_run TEXT, created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL, last_error TEXT, manual_metadata INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS source_paths(
 video_id TEXT REFERENCES videos(id), path TEXT NOT NULL, last_seen TEXT NOT NULL,
 PRIMARY KEY(video_id,path));
CREATE TABLE IF NOT EXISTS processing_jobs(
 id TEXT PRIMARY KEY, video_id TEXT REFERENCES videos(id), status TEXT NOT NULL,
 stage TEXT, config_hash TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
 elapsed_seconds REAL, error TEXT, runtime_json TEXT);
CREATE TABLE IF NOT EXISTS segments(
 video_id TEXT, run_id TEXT, segment_id TEXT, start REAL, end REAL, text TEXT,
 data_json TEXT NOT NULL, PRIMARY KEY(video_id,run_id,segment_id));
CREATE TABLE IF NOT EXISTS ocr_segments(
 video_id TEXT, run_id TEXT, segment_id TEXT, start REAL, end REAL, text TEXT,
 data_json TEXT NOT NULL, PRIMARY KEY(video_id,run_id,segment_id));
CREATE TABLE IF NOT EXISTS alignments(
 video_id TEXT, run_id TEXT, alignment_id TEXT, start REAL, end REAL, asr TEXT, ocr TEXT,
 category TEXT, data_json TEXT NOT NULL, PRIMARY KEY(video_id,run_id,alignment_id));
CREATE TABLE IF NOT EXISTS review_items(
 id TEXT PRIMARY KEY, video_id TEXT REFERENCES videos(id), run_id TEXT, start REAL,
 end REAL, kind TEXT, status TEXT NOT NULL DEFAULT 'pending', text TEXT,
 data_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS annotations(
 id INTEGER PRIMARY KEY AUTOINCREMENT, video_id TEXT REFERENCES videos(id),
 review_id TEXT REFERENCES review_items(id), start REAL, end REAL, author TEXT,
 kind TEXT NOT NULL, text TEXT, evidence_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS legacy_records(
 video_id TEXT, source TEXT, row_number INTEGER, data_json TEXT NOT NULL,
 PRIMARY KEY(video_id,source,row_number));
CREATE VIRTUAL TABLE IF NOT EXISTS corpus_fts USING fts5(
 video_id UNINDEXED, run_id UNINDEXED, kind UNINDEXED, start UNINDEXED, end UNINDEXED,
 text, tokenize='trigram');
"""

class Database:
    def __init__(self, output):
        self.output = Path(output)
        (self.output / "数据库").mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.output / "数据库/corpus.sqlite", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"数据库版本 {version} 高于工具支持版本 {SCHEMA_VERSION}")
        # Version 0 -> 1 is the initial migration; future migrations must be explicit.
        if version == 0:
            self.db.executescript(DDL)
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self.db.commit()

    def rows(self, sql, params=()):
        return [dict(r) for r in self.db.execute(sql, params)]

    def video(self, vid):
        rows = self.rows("SELECT * FROM videos WHERE id=?", (vid,))
        if not rows:
            raise ValueError(f"未知素材ID：{vid}")
        return rows[0]

    def series(self, name):
        if not name:
            return None
        row = self.db.execute("SELECT id FROM series WHERE name=?", (name,)).fetchone()
        if row:
            return row[0]
        number = self.db.execute("SELECT COUNT(*)+1 FROM series").fetchone()[0]
        sid = f"LBS-{number:04}"
        self.db.execute("INSERT INTO series VALUES (?,?)", (sid, name))
        return sid

    def scan(self, folder, probe, only=None):
        paths = [Path(only)] if only else sorted(p for p in Path(folder).rglob("*") if p.is_file() and p.suffix.lower() == ".mp4")
        result = []
        for path in paths:
            path = path.resolve()
            try:
                digest = sha256(path)
                old = self.db.execute("SELECT * FROM videos WHERE sha256=?", (digest,)).fetchone()
                with self.db:
                    if old:
                        vid = old["id"]
                        self.db.execute("UPDATE videos SET path=?,updated_at=? WHERE id=?", (str(path), now(), vid))
                    else:
                        meta = probe(path)
                        title, series, ep, total = parse_title(path.name)
                        sid = self.series(series)
                        number = max([int(r[0].split('-')[1]) for r in self.db.execute("SELECT id FROM videos")] + [0]) + 1
                        vid = f"LB-{number:04}"
                        self.db.execute("""INSERT INTO videos(id,sha256,title,original_filename,path,series_id,
                            episode,total_episodes,metadata_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                            (vid, digest, title, path.name, str(path), sid, ep, total, json.dumps(meta, ensure_ascii=False), now(), now()))
                    self.db.execute("INSERT OR REPLACE INTO source_paths VALUES (?,?,?)", (vid, str(path), now()))
                result.append({"id": vid, "path": str(path), "duplicate": bool(old), "error": None})
            except Exception as e:
                result.append({"path": str(path), "error": f"{type(e).__name__}: {e}"})
        return result

    def recover(self):
        with self.db:
            self.db.execute("UPDATE videos SET status='interrupted' WHERE status='running'")
            self.db.execute("UPDATE processing_jobs SET status='interrupted',finished_at=? WHERE status='running'", (now(),))

    def commit_run(self, video, run, packet):
        vid = video["id"]
        with self.db:
            for table, rows, key, textkey in [
                ("segments", packet["asr"]["segments"], "id", "text"),
                ("ocr_segments", packet["ocr"]["segments"], "id", "text")]:
                for s in rows:
                    self.db.execute(f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?)", (vid, run, str(s[key]), s["start"], s["end"], s[textkey], json.dumps(s, ensure_ascii=False)))
            for a in packet["alignments"]:
                self.db.execute("INSERT INTO alignments VALUES (?,?,?,?,?,?,?,?,?)", (vid, run, a["id"], a["start"], a["end"], a["asr_raw"], a["ocr_raw"], a["category"], json.dumps(a, ensure_ascii=False)))
            for r in packet["reviews"]:
                self.db.execute("INSERT OR IGNORE INTO review_items VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (r["id"], vid, run, r["start"], r["end"], r["kind"], "pending", r.get("text", ""), json.dumps(r, ensure_ascii=False), now()))
            self.db.execute("DELETE FROM corpus_fts WHERE video_id=?", (vid,))
            for kind, rows in [("asr", packet["asr"]["segments"]), ("ocr", packet["ocr"]["segments"])]:
                self.db.executemany("INSERT INTO corpus_fts VALUES (?,?,?,?,?,?)", [(vid, run, kind, s["start"], s["end"], s["text"]) for s in rows])
            self.db.execute("UPDATE videos SET status='completed',current_run=?,last_error=NULL,updated_at=? WHERE id=?", (run, now(), vid))

    def reviews(self, vid):
        # Include unresolved records from previous runs; reviewing never changes raw evidence.
        rows = self.rows("SELECT * FROM review_items WHERE video_id=? ORDER BY start,id", (vid,))
        for r in rows:
            # Persistent identical issues may have been created in an earlier run.
            r["first_seen_run"] = r["run_id"]
            r["is_legacy"] = r["run_id"] == "legacy"
        return rows

    def search(self, query):
        # Trigram MATCH only supports >=3 characters; LIKE keeps short Chinese names usable.
        if len(query) >= 3:
            literal = '"' + query.replace('"', '""') + '"'
            return self.rows("SELECT * FROM corpus_fts WHERE corpus_fts MATCH ? LIMIT 200", (literal,))
        literal = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return self.rows("SELECT * FROM corpus_fts WHERE text LIKE ? ESCAPE '\\' LIMIT 200", ("%" + literal + "%",))
