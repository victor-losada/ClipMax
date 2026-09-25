"""Base de datos SQLite: una sola conexión compartida protegida por un lock.

Todas las marcas de tiempo son epoch UTC (float). Los hilos de grabación, chat,
transcripción y la web escriben aquí; SQLite en modo WAL + un RLock es más que
suficiente para el volumen de un PC doméstico (lotes de chat cada 2 s).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY,
    fecha         TEXT UNIQUE NOT NULL,          -- día local del evento (YYYY-MM-DD)
    started_at    REAL,
    ended_at      REAL,
    estado        TEXT NOT NULL DEFAULT 'nueva', -- nueva|grabando|grabada|procesando|esperando_claude|lista|error
    pipeline_json TEXT NOT NULL DEFAULT '{}',    -- estado de cada paso del post-proceso
    x_contexto    TEXT NOT NULL DEFAULT ''       -- texto pegado manualmente desde X
);

CREATE TABLE IF NOT EXISTS recordings (
    id              INTEGER PRIMARY KEY,
    session_id      INTEGER NOT NULL,
    slug            TEXT NOT NULL,
    part_index      INTEGER NOT NULL,
    path            TEXT NOT NULL,
    started_at      REAL NOT NULL,   -- reloj de pared cuando ffmpeg empezó a escribir
    ended_at        REAL,
    duration        REAL,            -- duración real medida con ffprobe al finalizar
    bytes           INTEGER,
    final_path      TEXT,            -- MP4 del día al que se concatenó esta parte
    offset_in_final REAL             -- segundo del MP4 final donde empieza esta parte
);
CREATE INDEX IF NOT EXISTS idx_rec ON recordings(session_id, slug, part_index);

CREATE TABLE IF NOT EXISTS chat_buckets (
    session_id INTEGER NOT NULL,
    slug       TEXT NOT NULL,
    bucket_ts  REAL NOT NULL,
    n_msgs     INTEGER NOT NULL,
    n_users    INTEGER NOT NULL,
    n_hype     INTEGER NOT NULL,
    PRIMARY KEY (session_id, slug, bucket_ts)
);

CREATE TABLE IF NOT EXISTS chat_mentions (
    session_id INTEGER NOT NULL,
    slug       TEXT NOT NULL,     -- chat donde se escribió
    bucket_ts  REAL NOT NULL,
    target     TEXT NOT NULL,     -- streamer mencionado
    n          INTEGER NOT NULL,
    PRIMARY KEY (session_id, slug, bucket_ts, target)
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    slug       TEXT NOT NULL,
    ts         REAL NOT NULL,
    usuario    TEXT,
    texto      TEXT
);
CREATE INDEX IF NOT EXISTS idx_chat ON chat_messages(session_id, slug, ts);

CREATE TABLE IF NOT EXISTS transcript_segments (
    id         INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    slug       TEXT NOT NULL,
    start_ts   REAL NOT NULL,
    end_ts     REAL NOT NULL,
    texto      TEXT NOT NULL,
    fuente     TEXT NOT NULL    -- 'vivo' o 'candidato'
);
CREATE INDEX IF NOT EXISTS idx_tr ON transcript_segments(session_id, slug, start_ts);

CREATE TABLE IF NOT EXISTS signals (
    id           INTEGER PRIMARY KEY,
    session_id   INTEGER NOT NULL,
    slug         TEXT NOT NULL,
    ts           REAL NOT NULL,
    tipo         TEXT NOT NULL,  -- pico_chat|mencion_voz|mencion_chat|sincronia|tema_x
    score        REAL NOT NULL,
    detalle_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_sig ON signals(session_id, tipo, ts);

CREATE TABLE IF NOT EXISTS moments (
    id               INTEGER PRIMARY KEY,
    session_id       INTEGER NOT NULL,
    slug             TEXT NOT NULL,
    start_ts         REAL NOT NULL,
    end_ts           REAL NOT NULL,
    score            REAL NOT NULL,
    rank             INTEGER,
    componentes_json TEXT NOT NULL DEFAULT '{}',
    transcrito       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mom ON moments(session_id, rank);

CREATE TABLE IF NOT EXISTS x_posts (
    id         INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    ext_id     TEXT NOT NULL,
    autor      TEXT,
    texto      TEXT NOT NULL,
    url        TEXT,
    created_at REAL,
    likes      INTEGER DEFAULT 0,
    fuente     TEXT NOT NULL,     -- manual|api|claude_web|archivo
    UNIQUE (session_id, ext_id)
);

CREATE TABLE IF NOT EXISTS claude_runs (
    id            INTEGER PRIMARY KEY,
    session_id    INTEGER,
    created_at    REAL NOT NULL,
    mes           TEXT NOT NULL,   -- YYYY-MM para el control de presupuesto
    tipo          TEXT NOT NULL,   -- decision|investigacion_x
    modelo        TEXT,
    input_tokens  INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read    INTEGER DEFAULT 0,
    cache_write   INTEGER DEFAULT 0,
    web_searches  INTEGER DEFAULT 0,
    costo_usd     REAL DEFAULT 0,
    ok            INTEGER DEFAULT 1,
    nota          TEXT
);

CREATE TABLE IF NOT EXISTS outputs (
    id         INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    tipo       TEXT NOT NULL,     -- video|reporte_md|reporte_html|clip_tiktok|paquete|decision
    path       TEXT NOT NULL,
    created_at REAL NOT NULL,
    meta_json  TEXT NOT NULL DEFAULT '{}'
);

-- Clips para TikTok que se arman mientras se graba (clipmax/liveclips.py)
CREATE TABLE IF NOT EXISTS live_clips (
    id            INTEGER PRIMARY KEY,
    session_id    INTEGER NOT NULL,
    slug          TEXT NOT NULL,
    start_ts      REAL NOT NULL,     -- tramo candidato (hora de pared)
    end_ts        REAL NOT NULL,
    score         REAL NOT NULL DEFAULT 0,
    estado        TEXT NOT NULL DEFAULT 'procesando',   -- procesando|listo|descartado|error
    origen        TEXT NOT NULL DEFAULT '',             -- claude|auto
    titulo        TEXT NOT NULL DEFAULT '',
    caption       TEXT NOT NULL DEFAULT '',
    hashtags_json TEXT NOT NULL DEFAULT '[]',
    path          TEXT,
    thumb         TEXT,
    duracion      REAL,
    nota          TEXT NOT NULL DEFAULT '',
    subido        INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_live_clips ON live_clips(session_id, slug, start_ts);
"""


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Columnas agregadas en versiones posteriores (bases de datos creadas antes)."""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(transcript_segments)")}
        if "palabras_json" not in cols:  # tiempos por palabra para los subtítulos dinámicos
            self._conn.execute("ALTER TABLE transcript_segments ADD COLUMN palabras_json TEXT")

    # -- primitivas ----------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, rows)
            self._conn.commit()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        with self._lock:
            return _row_to_dict(self._conn.execute(sql, tuple(params)).fetchone())

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- sesiones ------------------------------------------------------------
    def get_or_create_session(self, fecha: str) -> dict:
        with self._lock:
            row = self.query_one("SELECT * FROM sessions WHERE fecha=?", (fecha,))
            if row:
                return row
            self.execute("INSERT INTO sessions (fecha) VALUES (?)", (fecha,))
            return self.query_one("SELECT * FROM sessions WHERE fecha=?", (fecha,))

    def get_session(self, session_id: int) -> dict | None:
        return self.query_one("SELECT * FROM sessions WHERE id=?", (session_id,))

    def get_session_by_date(self, fecha: str) -> dict | None:
        return self.query_one("SELECT * FROM sessions WHERE fecha=?", (fecha,))

    def list_sessions(self, limit: int = 60) -> list[dict]:
        return self.query("SELECT * FROM sessions ORDER BY fecha DESC LIMIT ?", (limit,))

    def update_session(self, session_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE sessions SET {cols} WHERE id=?", (*fields.values(), session_id))

    def pipeline_state(self, session_id: int) -> dict:
        row = self.get_session(session_id)
        return json.loads(row["pipeline_json"] or "{}") if row else {}

    def set_pipeline_step(self, session_id: int, step: str, estado: str, detalle: str = "") -> None:
        with self._lock:
            state = self.pipeline_state(session_id)
            state[step] = {"estado": estado, "detalle": detalle, "ts": time.time()}
            self.update_session(session_id, pipeline_json=json.dumps(state, ensure_ascii=False))

    def previous_sessions(self, fecha: str, limit: int = 5) -> list[dict]:
        return self.query(
            "SELECT * FROM sessions WHERE fecha < ? ORDER BY fecha DESC LIMIT ?", (fecha, limit)
        )

    # -- grabaciones ---------------------------------------------------------
    def add_part(self, session_id: int, slug: str, path: str, started_at: float) -> dict:
        with self._lock:
            row = self.query_one(
                "SELECT COALESCE(MAX(part_index), 0) AS m FROM recordings WHERE session_id=? AND slug=?",
                (session_id, slug),
            )
            idx = int(row["m"]) + 1
            cur = self.execute(
                "INSERT INTO recordings (session_id, slug, part_index, path, started_at) VALUES (?,?,?,?,?)",
                (session_id, slug, idx, path, started_at),
            )
            return self.query_one("SELECT * FROM recordings WHERE id=?", (cur.lastrowid,))

    def next_part_index(self, session_id: int, slug: str) -> int:
        row = self.query_one(
            "SELECT COALESCE(MAX(part_index), 0) AS m FROM recordings WHERE session_id=? AND slug=?",
            (session_id, slug),
        )
        return int(row["m"]) + 1

    def close_part(self, part_id: int, ended_at: float, nbytes: int) -> None:
        self.execute("UPDATE recordings SET ended_at=?, bytes=? WHERE id=?", (ended_at, nbytes, part_id))

    def update_part(self, part_id: int, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE recordings SET {cols} WHERE id=?", (*fields.values(), part_id))

    def list_parts(self, session_id: int, slug: str | None = None) -> list[dict]:
        if slug:
            return self.query(
                "SELECT * FROM recordings WHERE session_id=? AND slug=? ORDER BY part_index",
                (session_id, slug),
            )
        return self.query(
            "SELECT * FROM recordings WHERE session_id=? ORDER BY slug, part_index", (session_id,)
        )

    def delete_part(self, part_id: int) -> None:
        self.execute("DELETE FROM recordings WHERE id=?", (part_id,))

    # -- chat ----------------------------------------------------------------
    def add_chat_buckets(self, rows: list[tuple]) -> None:
        """rows: (session_id, slug, bucket_ts, n_msgs, n_users, n_hype). Suma si ya existe."""
        self.executemany(
            """INSERT INTO chat_buckets (session_id, slug, bucket_ts, n_msgs, n_users, n_hype)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(session_id, slug, bucket_ts) DO UPDATE SET
                 n_msgs = n_msgs + excluded.n_msgs,
                 n_users = n_users + excluded.n_users,
                 n_hype = n_hype + excluded.n_hype""",
            rows,
        )

    def add_chat_mentions(self, rows: list[tuple]) -> None:
        """rows: (session_id, slug, bucket_ts, target, n)."""
        self.executemany(
            """INSERT INTO chat_mentions (session_id, slug, bucket_ts, target, n) VALUES (?,?,?,?,?)
               ON CONFLICT(session_id, slug, bucket_ts, target) DO UPDATE SET n = n + excluded.n""",
            rows,
        )

    def add_chat_messages(self, rows: list[tuple]) -> None:
        """rows: (session_id, slug, ts, usuario, texto)."""
        self.executemany(
            "INSERT INTO chat_messages (session_id, slug, ts, usuario, texto) VALUES (?,?,?,?,?)", rows
        )

    def chat_buckets(self, session_id: int, slug: str) -> list[dict]:
        return self.query(
            "SELECT * FROM chat_buckets WHERE session_id=? AND slug=? ORDER BY bucket_ts",
            (session_id, slug),
        )

    def chat_mentions(self, session_id: int, slug: str | None = None) -> list[dict]:
        if slug:
            return self.query(
                "SELECT * FROM chat_mentions WHERE session_id=? AND slug=? ORDER BY bucket_ts",
                (session_id, slug),
            )
        return self.query(
            "SELECT * FROM chat_mentions WHERE session_id=? ORDER BY bucket_ts", (session_id,)
        )

    def chat_sample(self, session_id: int, slug: str, t0: float, t1: float, limit: int = 12) -> list[dict]:
        """Muestra de mensajes del intervalo, priorizando los más repetidos (el 'sentir' del chat)."""
        return self.query(
            """SELECT texto, COUNT(*) AS n FROM chat_messages
               WHERE session_id=? AND slug=? AND ts BETWEEN ? AND ? AND LENGTH(texto) > 1
               GROUP BY LOWER(texto) ORDER BY n DESC, MIN(ts) LIMIT ?""",
            (session_id, slug, t0, t1, limit),
        )

    def chat_count(self, session_id: int, slug: str, t0: float, t1: float) -> int:
        row = self.query_one(
            "SELECT COALESCE(SUM(n_msgs),0) AS n FROM chat_buckets WHERE session_id=? AND slug=? AND bucket_ts BETWEEN ? AND ?",
            (session_id, slug, t0, t1),
        )
        return int(row["n"])

    # -- transcripciones -----------------------------------------------------
    def add_segments(self, session_id: int, slug: str, segs: list[tuple], fuente: str) -> None:
        """segs: (inicio, fin, texto) o (inicio, fin, texto, palabras[[ini, fin, palabra], ...])."""
        rows = []
        for seg in segs:
            a, b, t = seg[0], seg[1], seg[2]
            words = seg[3] if len(seg) > 3 and seg[3] else None
            rows.append((session_id, slug, a, b, t, fuente,
                         json.dumps(words, ensure_ascii=False) if words else None))
        self.executemany(
            "INSERT INTO transcript_segments (session_id, slug, start_ts, end_ts, texto, fuente, palabras_json) "
            "VALUES (?,?,?,?,?,?,?)", rows)

    def segments(self, session_id: int, slug: str, t0: float, t1: float, fuente: str | None = None) -> list[dict]:
        sql = """SELECT * FROM transcript_segments WHERE session_id=? AND slug=?
                 AND end_ts >= ? AND start_ts <= ?"""
        params: list[Any] = [session_id, slug, t0, t1]
        if fuente:
            sql += " AND fuente=?"
            params.append(fuente)
        rows = self.query(sql + " ORDER BY start_ts", params)
        for r in rows:
            r["palabras"] = json.loads(r.pop("palabras_json") or "null") or []
        return rows

    def covered_until(self, session_id: int, slug: str, fuente: str) -> float:
        row = self.query_one(
            "SELECT COALESCE(MAX(end_ts), 0) AS m FROM transcript_segments WHERE session_id=? AND slug=? AND fuente=?",
            (session_id, slug, fuente),
        )
        return float(row["m"])

    def delete_segments(self, session_id: int, slug: str, t0: float, t1: float, fuente: str) -> None:
        self.execute(
            "DELETE FROM transcript_segments WHERE session_id=? AND slug=? AND fuente=? AND start_ts>=? AND start_ts<=?",
            (session_id, slug, fuente, t0, t1),
        )

    # -- señales y momentos ----------------------------------------------------
    def add_signal(self, session_id: int, slug: str, ts: float, tipo: str, score: float, detalle: dict) -> None:
        self.execute(
            "INSERT INTO signals (session_id, slug, ts, tipo, score, detalle_json) VALUES (?,?,?,?,?,?)",
            (session_id, slug, ts, tipo, score, json.dumps(detalle, ensure_ascii=False)),
        )

    def signals(self, session_id: int, tipos: Iterable[str] | None = None) -> list[dict]:
        rows = self.query("SELECT * FROM signals WHERE session_id=? ORDER BY ts", (session_id,))
        tipos = set(tipos) if tipos else None
        out = []
        for r in rows:
            if tipos and r["tipo"] not in tipos:
                continue
            r["detalle"] = json.loads(r.pop("detalle_json") or "{}")
            out.append(r)
        return out

    def delete_signals(self, session_id: int, tipos: Iterable[str]) -> None:
        tipos = list(tipos)
        marks = ",".join("?" * len(tipos))
        self.execute(f"DELETE FROM signals WHERE session_id=? AND tipo IN ({marks})", (session_id, *tipos))

    def replace_moments(self, session_id: int, moments: list[dict]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM moments WHERE session_id=?", (session_id,))
            self._conn.executemany(
                """INSERT INTO moments (session_id, slug, start_ts, end_ts, score, rank, componentes_json)
                   VALUES (?,?,?,?,?,?,?)""",
                [
                    (session_id, m["slug"], m["start_ts"], m["end_ts"], m["score"], m.get("rank"),
                     json.dumps(m.get("componentes", {}), ensure_ascii=False))
                    for m in moments
                ],
            )
            self._conn.commit()

    def moments(self, session_id: int, limit: int | None = None) -> list[dict]:
        sql = "SELECT * FROM moments WHERE session_id=? ORDER BY rank IS NULL, rank, score DESC"
        params: list[Any] = [session_id]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self.query(sql, params)
        for r in rows:
            r["componentes"] = json.loads(r.pop("componentes_json") or "{}")
        return rows

    def update_moment(self, moment_id: int, **fields: Any) -> None:
        if "componentes" in fields:
            fields["componentes_json"] = json.dumps(fields.pop("componentes"), ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE moments SET {cols} WHERE id=?", (*fields.values(), moment_id))

    # -- X ---------------------------------------------------------------------
    def add_x_posts(self, session_id: int, posts: list[dict], fuente: str) -> int:
        before = self.query_one("SELECT COUNT(*) AS n FROM x_posts WHERE session_id=?", (session_id,))["n"]
        self.executemany(
            """INSERT OR IGNORE INTO x_posts (session_id, ext_id, autor, texto, url, created_at, likes, fuente)
               VALUES (?,?,?,?,?,?,?,?)""",
            [
                (session_id, p["ext_id"], p.get("autor"), p["texto"], p.get("url"),
                 p.get("created_at"), int(p.get("likes") or 0), fuente)
                for p in posts
            ],
        )
        after = self.query_one("SELECT COUNT(*) AS n FROM x_posts WHERE session_id=?", (session_id,))["n"]
        return after - before

    def x_posts(self, session_id: int) -> list[dict]:
        return self.query(
            "SELECT * FROM x_posts WHERE session_id=? ORDER BY likes DESC, created_at", (session_id,)
        )

    def delete_x_posts(self, session_id: int, fuente: str | None = None) -> None:
        if fuente:
            self.execute("DELETE FROM x_posts WHERE session_id=? AND fuente=?", (session_id, fuente))
        else:
            self.execute("DELETE FROM x_posts WHERE session_id=?", (session_id,))

    # -- Claude: costos --------------------------------------------------------
    def add_claude_run(self, **fields: Any) -> None:
        fields.setdefault("created_at", time.time())
        fields.setdefault("mes", time.strftime("%Y-%m", time.gmtime(fields["created_at"])))
        cols = ", ".join(fields)
        marks = ",".join("?" * len(fields))
        self.execute(f"INSERT INTO claude_runs ({cols}) VALUES ({marks})", fields.values())

    def month_spend(self, mes: str | None = None) -> float:
        mes = mes or time.strftime("%Y-%m", time.gmtime())
        row = self.query_one("SELECT COALESCE(SUM(costo_usd),0) AS s FROM claude_runs WHERE mes=?", (mes,))
        return float(row["s"])

    def claude_runs(self, limit: int = 50) -> list[dict]:
        return self.query("SELECT * FROM claude_runs ORDER BY created_at DESC LIMIT ?", (limit,))

    # -- clips en vivo -----------------------------------------------------------
    def add_live_clip(self, **fields: Any) -> int:
        fields.setdefault("created_at", time.time())
        if "hashtags" in fields:
            fields["hashtags_json"] = json.dumps(fields.pop("hashtags"), ensure_ascii=False)
        cols = ", ".join(fields)
        cur = self.execute(f"INSERT INTO live_clips ({cols}) VALUES ({','.join('?' * len(fields))})",
                           fields.values())
        return int(cur.lastrowid)

    def update_live_clip(self, clip_id: int, **fields: Any) -> None:
        if "hashtags" in fields:
            fields["hashtags_json"] = json.dumps(fields.pop("hashtags"), ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE live_clips SET {cols} WHERE id=?", (*fields.values(), clip_id))

    def live_clips(self, session_id: int, estados: Iterable[str] | None = None,
                   limit: int | None = None) -> list[dict]:
        sql = "SELECT * FROM live_clips WHERE session_id=?"
        params: list[Any] = [session_id]
        if estados:
            estados = list(estados)
            sql += f" AND estado IN ({','.join('?' * len(estados))})"
            params += estados
        sql += " ORDER BY created_at DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self.query(sql, params)
        for r in rows:
            r["hashtags"] = json.loads(r.pop("hashtags_json") or "[]")
        return rows

    def live_clip(self, clip_id: int) -> dict | None:
        row = self.query_one("SELECT * FROM live_clips WHERE id=?", (clip_id,))
        if row:
            row["hashtags"] = json.loads(row.pop("hashtags_json") or "[]")
        return row

    # -- salidas -----------------------------------------------------------------
    def add_output(self, session_id: int, tipo: str, path: str, meta: dict | None = None) -> None:
        self.execute(
            "INSERT INTO outputs (session_id, tipo, path, created_at, meta_json) VALUES (?,?,?,?,?)",
            (session_id, tipo, path, time.time(), json.dumps(meta or {}, ensure_ascii=False)),
        )

    def outputs(self, session_id: int) -> list[dict]:
        rows = self.query("SELECT * FROM outputs WHERE session_id=? ORDER BY created_at DESC", (session_id,))
        for r in rows:
            r["meta"] = json.loads(r.pop("meta_json") or "{}")
        return rows
