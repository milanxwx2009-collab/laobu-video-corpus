import csv
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")

def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def atomic(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def save_json(path, obj):
    atomic(path, json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False))

def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))

def save_csv(path, rows, fields):
    import io
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v for k, v in row.items()})
    atomic(path, "\ufeff" + buffer.getvalue())

def stamp(seconds, srt=False):
    ms = max(0, round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02}{',' if srt else '.'}{ms:03}"

def norm(text):
    # Preserve score/decimal separators between digits: 2.5 must never equal 25.
    text = text.replace("．", ".").replace("：", ":")
    text = re.sub(r"(?<!\d)[.:]|[.:](?!\d)", "", text)
    text = re.sub(r"(?<!\d)%|[+\-$€¥￥](?!\d)", "", text)
    return re.sub(r"[^\w.:+%$€¥￥-]+|_", "", text, flags=re.UNICODE).casefold()

def fingerprint(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]

def parse_title(filename):
    title_match = re.search(r"《([^》]+)》", filename)
    ep_match = re.search(r"(?:第\s*)?(\d+)\s*[/_／]\s*(\d+)\s*[集段]", filename)
    title = title_match.group(1) if title_match else re.sub(r"_{1,4}[0-9a-f]{32}$", "", Path(filename).stem)
    series = title if title_match and ep_match else None
    # An unbracketed explicit episode label still supplies a conservative series name.
    if ep_match and not title_match:
        series = re.sub(r"^\(老布的青春\)", "", filename[:ep_match.start()]).strip("（() ） ") or None
        title = series or title
    return title, series, int(ep_match[1]) if ep_match else None, int(ep_match[2]) if ep_match else None

class CorpusLock:
    """One writer, including scans/reviews/exports; OS releases lock after a crash."""
    def __init__(self, folder):
        self.folder = Path(folder)
    def __enter__(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        self.file = (self.folder / "writer.lock").open("a+b")
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.file.seek(0, os.SEEK_END) == 0:
                self.file.write(b"0")
                self.file.flush()
        except OSError:
            self.file.close()
            raise RuntimeError("另一个命令正在写入此语料库，请等待其结束。")
        return self
    def __exit__(self, *_):
        self.file.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        self.file.close()
