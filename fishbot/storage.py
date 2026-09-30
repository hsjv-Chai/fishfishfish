from contextlib import contextmanager
from pathlib import Path
import fcntl
import json
import sqlite3
import time


def initial_state(capital=50):
    return dict(cash=round(capital * 10000), position=None, high_water=round(capital * 10000),
                day="", day_base=round(capital * 10000), day_pause="", halt="",
                last_signal=-1, last_exit=0, pending=None, initialized=False,
                ledger_id=0, account_balance=0, known_ids=[], divergence_since=None,
                shadow_started=None, shadow_last_tick=None, shadow_continuous_ms=0,
                canary_done=False)


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at INTEGER, kind TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS equity (at INTEGER PRIMARY KEY, units INTEGER, exposed INTEGER);
            CREATE TABLE IF NOT EXISTS alerts (key TEXT PRIMARY KEY, active INTEGER, sent INTEGER, last_sent INTEGER, message TEXT);
        ''')
        self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.db:
            self.db.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, json.dumps(value, allow_nan=False)))

    def save(self, state, kind=None, data=None, now=None):
        with self.db:
            self.db.execute("INSERT INTO kv VALUES ('state',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (json.dumps(state, allow_nan=False),))
            if kind:
                self.db.execute("INSERT INTO events(at,kind,data) VALUES (?,?,?)",
                                (now or int(time.time() * 1000), kind, json.dumps(data or {}, allow_nan=False)))

    def event(self, kind, data, now=None):
        with self.db:
            self.db.execute("INSERT INTO events(at,kind,data) VALUES (?,?,?)",
                            (now or int(time.time() * 1000), kind, json.dumps(data, allow_nan=False)))

    def equity(self, at, value, exposed):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO equity VALUES (?,?,?)", (at, value, int(exposed)))

    def close(self):
        self.db.close()


@contextmanager
def instance_lock(database):
    path = Path(database + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another trader owns this database") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
