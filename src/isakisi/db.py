import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .settings import Problem, Settings

SCHEMA = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")


def stamp(seconds=0):
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="microseconds")


def pack(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha(data: bytes):
    return hashlib.sha256(data).hexdigest()


def digest(value):
    return sha(pack(value).encode())


def atomic_write(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.app_data_dir
        self.path = self.root / "isakisi.sqlite3"
        self.ledger_path = self.root / "outgoing.sqlite3"
        self.blobs = self.root / "documents"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.blobs.mkdir(exist_ok=True, mode=0o700)

    @contextmanager
    def connect(self, *, write=False, ledger=False):
        conn = sqlite3.connect(self.ledger_path if ledger else self.path, timeout=12)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self):
        with self.connect(write=True) as conn:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
            conn.execute("INSERT OR IGNORE INTO alembic_version VALUES ('0001')")
            if self.settings.discovery_mode == "live" and conn.execute(
                "SELECT 1 FROM control WHERE key='demo_profile'"
            ).fetchone():
                raise Problem("Canlı kullanım için örnek profilden ayrı bir APP_DATA_DIR seçin.")
        with self.connect(write=True, ledger=True) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS outgoing (target_key TEXT PRIMARY KEY, attempt TEXT NOT NULL, created TEXT NOT NULL)")

    def put(self, data: bytes):
        key = sha(data)
        if not (self.blobs / key).exists():
            atomic_write(self.blobs / key, data)
        return key

    def get(self, key):
        try:
            data = (self.blobs / key).read_bytes()
        except OSError:
            raise Problem("Paket dosyasına ulaşılamıyor.") from None
        if sha(data) != key:
            raise Problem("Paket dosyasının özeti uyuşmuyor.")
        return data

    def session_key(self):
        with self.connect(write=True) as conn:
            return get_secret(conn)


def get_secret(conn):
    import secrets
    conn.execute("INSERT OR IGNORE INTO control VALUES ('session_key', ?)", (secrets.token_hex(32),))
    return conn.execute("SELECT value FROM control WHERE key='session_key'").fetchone()[0]


def notify(conn, key, message, link):
    conn.execute("INSERT OR IGNORE INTO notifications(key,message,link,created) VALUES (?,?,?,?)",
                 (key, message, link, stamp()))
