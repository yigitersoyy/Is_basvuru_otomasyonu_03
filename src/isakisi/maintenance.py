import io
import json
import re
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken

from .db import atomic_write, pack, sha, stamp
from .settings import Problem


def cipher(key):
    if not key:
        raise Problem("Önce ISAKISI_BACKUP_KEY ortam değişkenini ayarlayın.")
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError):
        raise Problem("Yedek anahtarı geçerli değil.") from None


def snapshot(source, target):
    with closing(sqlite3.connect(source)) as conn, closing(sqlite3.connect(target)) as copy:
        conn.backup(copy)


def backup(store, destination, key):
    crypto = cipher(key)
    destination = destination.resolve()
    if destination.exists():
        raise Problem("Yeni bir yedek dosyası adı seçin.")
    if destination in {store.path.resolve(), store.ledger_path.resolve()} or destination.is_relative_to(store.blobs.resolve()):
        raise Problem("Yedeği belge dizininin dışında saklayın.")
    with tempfile.TemporaryDirectory(dir=store.root) as folder:
        from pathlib import Path
        work = Path(folder)
        entries = {}
        with store.connect(write=True) as conn:
            snapshot(store.path, work / "database")
            snapshot(store.ledger_path, work / "ledger")
            for row in conn.execute("SELECT DISTINCT digest FROM documents"):
                entries[f"documents/{row[0]}"] = store.get(row[0])
        entries.update({name: (work / name).read_bytes() for name in ("database", "ledger")})
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as out:
            out.writestr("manifest.json", pack({name: sha(blob) for name, blob in entries.items()}))
            for name, content in entries.items():
                out.writestr(name, content)
        atomic_write(destination, crypto.encrypt(archive.getvalue()))


def restore(store, source, key):
    from pathlib import Path

    try:
        data = cipher(key).decrypt(source.read_bytes())
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            if not {"database", "ledger"}.issubset(manifest):
                raise ValueError
            entries = {}
            for name, expected in manifest.items():
                if name not in {"database", "ledger"} and not re.fullmatch(r"documents/[0-9a-f]{64}", name):
                    raise ValueError
                blob = archive.read(name)
                if sha(blob) != expected or (name.startswith("documents/") and name[10:] != expected):
                    raise ValueError
                entries[name] = blob
    except (InvalidToken, ValueError, OSError, KeyError, TypeError, zipfile.BadZipFile):
        raise Problem("Yedek anahtarı veya dosya bütünlüğü doğrulanamadı.") from None
    with tempfile.TemporaryDirectory(dir=store.root) as folder:
        work = Path(folder)
        database = work / "database"
        ledger = work / "ledger"
        database.write_bytes(entries.pop("database"))
        ledger.write_bytes(entries.pop("ledger"))
        try:
            with closing(sqlite3.connect(database)) as db, closing(sqlite3.connect(ledger)) as old_ledger:
                for conn in (db, old_ledger):
                    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError
                required = {r[0] for r in db.execute("SELECT DISTINCT digest FROM documents")}
                if set(entries) != {f"documents/{name}" for name in required}:
                    raise ValueError
                attempts = db.execute("""SELECT id,application_id FROM submission_attempts
                   WHERE state IN ('queued','checking','submitting')""").fetchall()
                for attempt, app in attempts:
                    db.execute("UPDATE submission_attempts SET state='submission_unknown',updated=? WHERE id=?", (stamp(), attempt))
                    db.execute("UPDATE applications SET state='submission_unknown',updated=? WHERE id=?", (stamp(), app))
                    db.execute("""INSERT INTO application_events(application_id,state,evidence,note,created)
                      VALUES (?,'submission_unknown','restore','Yedek sonrası kullanıcı teyidi gerekli.',?)""", (app, stamp()))
                db.execute("INSERT OR REPLACE INTO control VALUES ('restore_lock',?)", (stamp(),))
                db.execute("DELETE FROM control WHERE key='session_key'")
                db.execute("UPDATE tasks SET state='failed',error='restore' WHERE kind='submit' AND state IN ('running','pending')")
                db.execute("UPDATE tasks SET state='pending',lease_until=NULL,worker=NULL WHERE state='running'")
                db.execute("UPDATE source_runs SET status='interrupted',finished=? WHERE status='running'", (stamp(),))
                db.commit()
                reservations = old_ledger.execute("SELECT * FROM outgoing").fetchall()
        except (sqlite3.Error, ValueError):
            raise Problem("Yedek şeması veya belge listesi geçersiz.") from None
        for blob in entries.values():
            store.put(blob)
        with store.connect(write=True, ledger=True) as live_ledger:
            live_ledger.executemany("INSERT OR IGNORE INTO outgoing VALUES (?,?,?)", reservations)
        atomic_write(store.path, database.read_bytes())


def cleanup(store):
    settings = store.settings
    with store.connect(write=True) as conn:
        conn.execute("UPDATE posting_versions SET raw=NULL WHERE raw IS NOT NULL AND created<?",
                     (stamp(-settings.raw_retention_days * 86400),))
        cutoff = stamp(-settings.log_retention_days * 86400)
        conn.execute("DELETE FROM tasks WHERE state IN ('done','failed') AND created<?", (cutoff,))
        conn.execute("DELETE FROM source_runs WHERE status<>'running' AND started<?", (cutoff,))
        expired = conn.execute("""SELECT b.id FROM bundles b JOIN applications a ON a.id=b.application_id
            WHERE a.state IN ('rejected','withdrawn','simulated') AND a.updated<? AND b.payload<>'{}'""",
                               (stamp(-settings.closed_retention_days * 86400),)).fetchall()
        for (bundle_id,) in expired:
            conn.execute("DELETE FROM documents WHERE bundle_id=?", (bundle_id,))
            conn.execute("UPDATE bundles SET payload='{}' WHERE id=?", (bundle_id,))
    # Clean up detached content after its database transaction has committed.
    with store.connect(write=True) as conn:
        referenced = {r[0] for r in conn.execute("SELECT DISTINCT digest FROM documents")}
        cutoff = datetime.fromisoformat(stamp(-86400)).timestamp()
        for path in store.blobs.iterdir():
            if re.fullmatch(r"[0-9a-f]{64}", path.name) and path.name not in referenced and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
