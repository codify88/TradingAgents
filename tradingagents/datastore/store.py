"""A point-in-time store of raw vendor responses, in one SQLite file.

Raw bodies are stored, not parsed frames, so a parser fix never needs a refetch
and the store never encodes a parser's bug. Keys are the endpoint plus its
parameters with the API key removed, in canonical order. WAL mode lets the
nightly run, the harvest and an interactive session share the file.

Design: docs/design/llmquant.md, layer 1.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS response (
    vendor      TEXT NOT NULL,
    endpoint    TEXT NOT NULL,
    params_key  TEXT NOT NULL,
    symbol      TEXT,
    fetched_at  TEXT NOT NULL,
    final       INTEGER NOT NULL,
    payload     BLOB NOT NULL,
    PRIMARY KEY (vendor, endpoint, params_key)
);
CREATE INDEX IF NOT EXISTS response_symbol ON response (symbol);

CREATE TABLE IF NOT EXISTS snapshot (
    vendor      TEXT NOT NULL,
    endpoint    TEXT NOT NULL,
    params_key  TEXT NOT NULL,
    symbol      TEXT,
    fetched_on  TEXT NOT NULL,
    digest      TEXT NOT NULL,
    payload     BLOB,              -- NULL when identical to the previous snapshot
    same_as     TEXT,              -- fetched_on of the snapshot holding the payload
    PRIMARY KEY (vendor, endpoint, params_key, fetched_on)
);

CREATE TABLE IF NOT EXISTS node (id TEXT PRIMARY KEY, kind TEXT NOT NULL, attrs TEXT);
-- ``ref`` tells apart two facts of one kind on one day (an insider's two Form 4
-- rows): the digest of the row the edge came from.
CREATE TABLE IF NOT EXISTS edge (
    src TEXT NOT NULL, dst TEXT NOT NULL, kind TEXT NOT NULL,
    as_of TEXT NOT NULL, ref TEXT NOT NULL DEFAULT '',
    available_at TEXT, source TEXT, attrs TEXT,
    PRIMARY KEY (src, dst, kind, as_of, ref)
);
CREATE INDEX IF NOT EXISTS edge_dst ON edge (dst, kind, available_at);
CREATE INDEX IF NOT EXISTS edge_src ON edge (src, kind);

CREATE TABLE IF NOT EXISTS request_count (
    day TEXT NOT NULL, vendor TEXT NOT NULL, endpoint TEXT NOT NULL,
    outcome TEXT NOT NULL,          -- hit | fetch
    n INTEGER NOT NULL,
    PRIMARY KEY (day, vendor, endpoint, outcome)
);

CREATE TABLE IF NOT EXISTS throttle (
    name TEXT PRIMARY KEY, tokens REAL NOT NULL, updated REAL NOT NULL
);
"""

# Parameters that do not change the answer and must not split the cache.
_IGNORED_PARAMS = frozenset({"apikey", "source"})


def params_key(params: dict) -> str:
    kept = {k: str(v) for k, v in params.items() if k not in _IGNORED_PARAMS and v is not None}
    return json.dumps(kept, sort_keys=True, separators=(",", ":"))


def _now() -> datetime:
    return datetime.now(UTC)


class DataStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        conn = self._conn()
        self._migrate(conn)
        conn.executescript(SCHEMA)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """The first edge table had no ``ref``; it was never written, so it is
        rebuilt. A populated one would need a real migration and is refused."""
        cols = [r[1] for r in conn.execute("PRAGMA table_info(edge)")]
        if cols and "ref" not in cols:
            if conn.execute("SELECT COUNT(*) FROM edge").fetchone()[0]:
                raise RuntimeError("edge table predates `ref` and holds rows; migrate it by hand")
            conn.execute("DROP TABLE edge")

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    # -- responses ---------------------------------------------------------

    def get(self, vendor: str, endpoint: str, key: str) -> tuple[str, datetime, bool] | None:
        """The stored body, when it was fetched, and whether it is final. A row
        that cannot be decoded is treated as absent, never as an error."""
        row = self._conn().execute(
            "SELECT payload, fetched_at, final FROM response "
            "WHERE vendor=? AND endpoint=? AND params_key=?",
            (vendor, endpoint, key),
        ).fetchone()
        if row is None:
            return None
        try:
            body = gzip.decompress(row[0]).decode("utf-8")
            fetched = datetime.fromisoformat(row[1])
        except (OSError, EOFError, UnicodeDecodeError, ValueError, TypeError):
            return None
        return body, fetched, bool(row[2])

    def put(self, vendor: str, endpoint: str, key: str, body: str, *,
            symbol: str | None, final: bool, fetched_at: datetime | None = None) -> None:
        fetched_at = fetched_at or _now()
        self._conn().execute(
            "INSERT OR REPLACE INTO response VALUES (?,?,?,?,?,?,?)",
            (vendor, endpoint, key, symbol, fetched_at.isoformat(), int(final),
             gzip.compress(body.encode("utf-8"))),
        )

    def stored(self, vendor: str, endpoint: str, key: str) -> tuple[str, datetime] | None:
        """The stored body and when it was fetched, whatever its age -- for
        callers with their own schedule (the harvest's weekly refreshes)."""
        row = self.get(vendor, endpoint, key)
        return None if row is None else (row[0], row[1])

    # -- snapshots ---------------------------------------------------------

    def snapshot(self, vendor: str, endpoint: str, key: str, body: str, *,
                 symbol: str | None, fetched_on: str) -> None:
        """Append today's response; one per day, and an unchanged body is stored
        as a pointer to the snapshot that holds it."""
        conn = self._conn()
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        prev = conn.execute(
            "SELECT fetched_on, digest, same_as FROM snapshot "
            "WHERE vendor=? AND endpoint=? AND params_key=? AND fetched_on<? "
            "ORDER BY fetched_on DESC LIMIT 1",
            (vendor, endpoint, key, fetched_on),
        ).fetchone()
        if prev is not None and prev[1] == digest:
            payload, same_as = None, prev[2] or prev[0]
        else:
            payload, same_as = gzip.compress(body.encode("utf-8")), None
        conn.execute(
            "INSERT OR REPLACE INTO snapshot VALUES (?,?,?,?,?,?,?,?)",
            (vendor, endpoint, key, symbol, fetched_on, digest, payload, same_as),
        )

    def last_snapshot_day(self, vendor: str, endpoint: str, key: str) -> str | None:
        row = self._conn().execute(
            "SELECT MAX(fetched_on) FROM snapshot WHERE vendor=? AND endpoint=? AND params_key=?",
            (vendor, endpoint, key)).fetchone()
        return row[0] if row else None

    def snapshots(self, vendor: str, endpoint: str, key: str) -> list[tuple[str, str]]:
        """Every snapshot as (fetched_on, body), oldest first."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT fetched_on, payload, same_as FROM snapshot "
            "WHERE vendor=? AND endpoint=? AND params_key=? ORDER BY fetched_on",
            (vendor, endpoint, key),
        ).fetchall()
        bodies = {r[0]: r[1] for r in rows if r[1] is not None}
        return [(on, gzip.decompress(payload if payload is not None else bodies[same_as]).decode("utf-8"))
                for on, payload, same_as in rows]

    # -- the graph ---------------------------------------------------------

    def upsert_node(self, node_id: str, kind: str, attrs: dict | None = None) -> None:
        self._conn().execute("INSERT OR REPLACE INTO node VALUES (?,?,?)",
                             (node_id, kind, json.dumps(attrs or {}, sort_keys=True)))

    def upsert_nodes(self, rows: list[tuple[str, str, dict | None]]) -> None:
        """Rows of (id, kind, attrs), in one transaction."""
        conn = self._conn()
        conn.execute("BEGIN")
        try:
            conn.executemany("INSERT OR REPLACE INTO node VALUES (?,?,?)",
                             [(i, k, json.dumps(a or {}, sort_keys=True)) for i, k, a in rows])
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def upsert_edges(self, rows: list[tuple]) -> None:
        """Rows of (src, dst, kind, as_of, ref, available_at, source, attrs dict)."""
        conn = self._conn()
        conn.execute("BEGIN")
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO edge VALUES (?,?,?,?,?,?,?,?)",
                [(s, d, k, a, r, av, src, json.dumps(at or {}, sort_keys=True))
                 for s, d, k, a, r, av, src, at in rows])
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def edges_to(self, dst: str, kind: str, available_by: str | None = None) -> list[dict]:
        """Edges into ``dst`` of ``kind``, only those public by ``available_by``."""
        sql = ("SELECT src, as_of, available_at, attrs FROM edge WHERE dst=? AND kind=?"
               + (" AND available_at <= ?" if available_by else "") + " ORDER BY as_of")
        args = (dst, kind, available_by) if available_by else (dst, kind)
        return [{"src": s, "as_of": a, "available_at": av, **json.loads(at or "{}")}
                for s, a, av, at in self._conn().execute(sql, args)]

    def node(self, node_id: str) -> dict | None:
        row = self._conn().execute("SELECT kind, attrs FROM node WHERE id=?", (node_id,)).fetchone()
        return None if row is None else {"id": node_id, "kind": row[0], **json.loads(row[1] or "{}")}

    # -- counters and stats ------------------------------------------------

    def count(self, vendor: str, endpoint: str, outcome: str, day: str) -> None:
        self._conn().execute(
            "INSERT INTO request_count VALUES (?,?,?,?,1) "
            "ON CONFLICT (day, vendor, endpoint, outcome) DO UPDATE SET n = n + 1",
            (day, vendor, endpoint, outcome),
        )

    def counts(self, day: str) -> dict[tuple[str, str], dict[str, int]]:
        out: dict[tuple[str, str], dict[str, int]] = {}
        for vendor, endpoint, outcome, n in self._conn().execute(
            "SELECT vendor, endpoint, outcome, n FROM request_count WHERE day=? "
            "ORDER BY vendor, endpoint", (day,),
        ):
            out.setdefault((vendor, endpoint), {})[outcome] = n
        return out

    def stats(self) -> dict:
        conn = self._conn()
        responses, final = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(final), 0) FROM response").fetchone()
        snaps = conn.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0]
        size = sum(p.stat().st_size for p in self.path.parent.glob(self.path.name + "*"))
        return {"responses": responses, "final": final, "snapshots": snaps, "bytes": size}
