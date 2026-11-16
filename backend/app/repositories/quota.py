from __future__ import annotations

import json
import sqlite3
from typing import Any

_COLS = "id, subject_id, version, strata_field, quota_mode, shortfall_policy, stale_policy, quotas_json, created_at"


def _parse(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    d["quotas"] = json.loads(d.pop("quotas_json"))
    return d


def insert_config(
    conn: sqlite3.Connection,
    *,
    subject_id: int,
    version: int,
    strata_field: str,
    quota_mode: str,
    shortfall_policy: str,
    stale_policy: str,
    quotas: dict[str, Any],
    created_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO quota_configs(
            subject_id, version, strata_field, quota_mode,
            shortfall_policy, stale_policy, quotas_json, created_at
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            subject_id,
            version,
            strata_field,
            quota_mode,
            shortfall_policy,
            stale_policy,
            json.dumps(quotas, ensure_ascii=False),
            created_at,
        ),
    )
    return int(cur.lastrowid)


def get_current(conn: sqlite3.Connection, subject_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        f"""
        SELECT {_COLS} FROM quota_configs
        WHERE subject_id=?
        ORDER BY version DESC
        LIMIT 1
        """,
        (subject_id,),
    ).fetchone()
    return _parse(row)


def get_by_id(conn: sqlite3.Connection, quota_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {_COLS} FROM quota_configs WHERE id=?",
        (quota_id,),
    ).fetchone()
    return _parse(row)


def list_current(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT {_COLS} FROM quota_configs q
        WHERE version = (SELECT MAX(version) FROM quota_configs WHERE subject_id=q.subject_id)
        ORDER BY subject_id
        """
    ).fetchall()
    return [_parse(r) for r in rows]


def list_versions(conn: sqlite3.Connection, subject_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT {_COLS} FROM quota_configs
        WHERE subject_id=?
        ORDER BY version DESC
        """,
        (subject_id,),
    ).fetchall()
    return [_parse(r) for r in rows]


def max_version(conn: sqlite3.Connection, subject_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) v FROM quota_configs WHERE subject_id=?",
        (subject_id,),
    ).fetchone()
    return int(row["v"])


def version_map(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    """quota_config id -> {version, subject_id}，供批次/包反查版本号。"""
    rows = conn.execute("SELECT id, subject_id, version FROM quota_configs").fetchall()
    return {r["id"]: {"subject_id": r["subject_id"], "version": r["version"]} for r in rows}
