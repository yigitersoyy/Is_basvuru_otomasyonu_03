import json
import secrets
import time

from .db import pack, stamp
from .settings import Problem


def enqueue(conn, key, kind, payload):
    now = stamp()
    return conn.execute(
        "INSERT OR IGNORE INTO tasks(key,kind,payload,state,available,created) VALUES (?,?,?,'pending',?,?)",
        (key, kind, pack(payload), now, now)
    ).rowcount


def schedule(store, only=None, force=False):
    from .catalog import source_row

    sources = [source for source in store.settings.sources() if source.read and (only is None or source.id == only)]
    if only and not sources:
        raise Problem("Etkin kaynak bulunamadı.")
    total = 0
    with store.connect(write=True) as conn:
        for source in sources:
            source_row(conn, source)
            previous = conn.execute("SELECT started FROM source_runs WHERE source_id=? ORDER BY id DESC LIMIT 1",
                                    (source.id,)).fetchone()
            if previous and not force and previous[0] > stamp(-source.interval_seconds):
                continue
            active = conn.execute("""SELECT 1 FROM tasks WHERE kind='discover' AND state IN ('pending','running')
                AND json_extract(payload,'$.source')=?""", (source.id,)).fetchone()
            if not active:
                total += enqueue(conn, f"discover:{source.id}:{secrets.token_hex(8)}", "discover", {"source": source.id})
    return total


def claim(store):
    now = stamp()
    with store.connect(write=True) as conn:
        conn.execute("""UPDATE tasks SET state='pending',worker=NULL,lease_until=NULL,error='interrupted'
                        WHERE state='running' AND lease_until<? AND kind IN ('discover','reconcile') AND attempts<3""", (now,))
        conn.execute("""UPDATE tasks SET state='failed',worker=NULL,lease_until=NULL,error='interrupted'
                        WHERE state='running' AND lease_until<?""", (now,))
        conn.execute("""UPDATE source_runs SET status='interrupted',finished=?
            WHERE status='running' AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.state='running'
                AND t.kind='discover' AND json_extract(t.payload,'$.source')=source_runs.source_id)""", (now,))
        row = conn.execute("""SELECT * FROM tasks WHERE state='pending' AND available<=?
            ORDER BY CASE kind WHEN 'submit' THEN 0 WHEN 'reconcile' THEN 1 ELSE 2 END,id LIMIT 1""", (now,)).fetchone()
        if not row:
            return None
        task = dict(row)
        task["worker"] = secrets.token_hex(16)
        task["attempts"] += 1
        conn.execute("UPDATE tasks SET state='running',worker=?,lease_until=?,attempts=? WHERE id=?",
                     (task["worker"], stamp(300), task["attempts"], task["id"]))
        return task


def work_once(store):
    from .applications import recover_attempts, reconcile, run_attempt
    from .matching import refresh
    from .sources import ReadFailure, scan

    task = claim(store)
    recover_attempts(store)
    if not task:
        return False

    def renew(conn):
        changed = conn.execute("UPDATE tasks SET lease_until=? WHERE id=? AND worker=? AND lease_until>=?",
                               (stamp(300), task["id"], task["worker"], stamp())).rowcount
        if not changed:
            raise Problem("Görevin süresi doldu.")

    state, error, wait = "done", None, 0
    payload = json.loads(task["payload"])
    try:
        if task["kind"] == "discover":
            source = next((s for s in store.settings.sources() if s.id == payload["source"] and s.read), None)
            if not source:
                raise Problem("Kaynağın okuma izni kapalı.")
            scan(store, source, lease=renew)
            with store.connect(write=True) as conn:
                refresh(conn)
        elif task["kind"] == "submit":
            run_attempt(store, payload["attempt"])
        else:
            reconcile(store, payload["attempt"])
    except ReadFailure as failure:
        error = failure.code
        retryable = error in {"network", "429", "500", "502", "503", "504"}
        state = "pending" if retryable and task["attempts"] < 3 else "failed"
        wait = max(15 * 2 ** (task["attempts"] - 1), failure.retry_after)
    except Exception:
        state, error = "failed", "operation"
    with store.connect(write=True) as conn:
        conn.execute("""UPDATE tasks SET state=?,error=?,available=?,lease_until=NULL,worker=NULL
                        WHERE id=? AND worker=?""", (state, error, stamp(wait), task["id"], task["worker"]))
    return True


def worker(store, once=False):
    while True:
        if not work_once(store):
            if once:
                return
            time.sleep(1)
