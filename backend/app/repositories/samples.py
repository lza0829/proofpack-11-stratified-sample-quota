from __future__ import annotations

import json
import sqlite3
from typing import Any

_BATCH_COLS = "id, batch_no, subject_id, seed, rate, source_count, sample_count, status, created_at, quota_version_id, quota_met"


def create_batch(
    conn: sqlite3.Connection,
    *,
    batch_no: str,
    subject_id: int,
    seed: str,
    rate: float,
    source_count: int,
    sample_count: int,
    status: str,
    created_at: str,
    quota_version_id: int | None = None,
    quota_met: int | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO sample_batches(
            batch_no, subject_id, seed, rate, source_count, sample_count, status,
            created_at, quota_version_id, quota_met
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            batch_no,
            subject_id,
            seed,
            rate,
            source_count,
            sample_count,
            status,
            created_at,
            quota_version_id,
            quota_met,
        ),
    )
    return int(cur.lastrowid)


def insert_rows(
    conn: sqlite3.Connection,
    batch_id: int,
    rows: list[tuple[dict[str, Any], str | None]],
) -> None:
    """rows 为 (样本数据, 所属层) 对；层值落库，供分层明细与样本行互证。"""
    for r, stratum in rows:
        conn.execute(
            """
            INSERT INTO sample_rows(batch_id, business_key, payload_json, event_ts, revision, stratum)
            VALUES (?,?,?,?,?,?)
            """,
            (
                batch_id,
                str(r.get("business_key", "")),
                json.dumps(r, ensure_ascii=False),
                r.get("event_ts"),
                r.get("revision"),
                stratum,
            ),
        )


def insert_strata(
    conn: sqlite3.Connection,
    batch_id: int,
    strata: list[dict[str, Any]],
) -> None:
    """批次分层明细：每层源条数、目标条数、样本数、是否触达配额。"""
    for s in strata:
        conn.execute(
            """
            INSERT INTO sample_batch_strata(
                batch_id, stratum, source_count, target_count, sample_count, quota_met
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                batch_id,
                s["stratum"],
                s["source_count"],
                s["target_count"],
                s["sample_count"],
                1 if s["quota_met"] else 0,
            ),
        )


def list_strata(conn: sqlite3.Connection, batch_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, batch_id, stratum, source_count, target_count, sample_count, quota_met
        FROM sample_batch_strata
        WHERE batch_id=?
        ORDER BY stratum
        """,
        (batch_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_batches(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT {_BATCH_COLS}
        FROM sample_batches
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_batch(conn: sqlite3.Connection, batch_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        f"""
        SELECT {_BATCH_COLS}
        FROM sample_batches WHERE id=?
        """,
        (batch_id,),
    ).fetchone()
    return dict(row) if row else None


def list_rows(conn: sqlite3.Connection, batch_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, batch_id, business_key, payload_json, event_ts, revision, stratum
        FROM sample_rows WHERE batch_id=? ORDER BY business_key
        """,
        (batch_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["payload"] = json.loads(d.pop("payload_json"))
        except json.JSONDecodeError:
            d["payload"] = d.pop("payload_json")
        out.append(d)
    return out
