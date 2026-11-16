from __future__ import annotations

import json
import sqlite3
from typing import Any

_PACK_COLS = "id, pack_no, batch_id, subject_id, item_count, fingerprint, status, created_at, quota_version_id, releasable, hold_reason"


def create_pack(
    conn: sqlite3.Connection,
    *,
    pack_no: str,
    batch_id: int,
    subject_id: int,
    item_count: int,
    fingerprint: str,
    status: str,
    created_at: str,
    quota_version_id: int | None = None,
    releasable: int = 1,
    hold_reason: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO evidence_packs(
            pack_no, batch_id, subject_id, item_count, fingerprint, status,
            created_at, quota_version_id, releasable, hold_reason
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            pack_no,
            batch_id,
            subject_id,
            item_count,
            fingerprint,
            status,
            created_at,
            quota_version_id,
            releasable,
            hold_reason,
        ),
    )
    return int(cur.lastrowid)


def insert_items(
    conn: sqlite3.Connection,
    pack_id: int,
    items: list[dict[str, Any]],
) -> None:
    for it in items:
        conn.execute(
            """
            INSERT INTO pack_items(pack_id, business_key, summary_json, item_fingerprint)
            VALUES (?,?,?,?)
            """,
            (
                pack_id,
                str(it["business_key"]),
                json.dumps(it["summary"], ensure_ascii=False),
                it["item_fingerprint"],
            ),
        )


def list_packs(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT {_PACK_COLS}
        FROM evidence_packs
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_pack(conn: sqlite3.Connection, pack_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        f"""
        SELECT {_PACK_COLS}
        FROM evidence_packs WHERE id=?
        """,
        (pack_id,),
    ).fetchone()
    return dict(row) if row else None


def list_items(conn: sqlite3.Connection, pack_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, pack_id, business_key, summary_json, item_fingerprint
        FROM pack_items WHERE pack_id=? ORDER BY business_key
        """,
        (pack_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["summary"] = json.loads(d.pop("summary_json"))
        except json.JSONDecodeError:
            d["summary"] = d.pop("summary_json")
        out.append(d)
    return out


def update_status(conn: sqlite3.Connection, pack_id: int, status: str) -> None:
    conn.execute("UPDATE evidence_packs SET status=? WHERE id=?", (status, pack_id))


def apply_stale_policy(
    conn: sqlite3.Connection,
    *,
    subject_id: int,
    current_quota_id: int,
    stale_policy: str,
    hold_reason: str,
) -> int:
    """
    配额变更时处置仍引用旧配额的未放行包（同事务调用，同成同败）：
    invalidate —— 作废；freeze —— 保留只读对照但禁止放行。返回受影响包数。
    已放行/已驳回/已作废的包是历史留痕，不动。
    """
    if stale_policy == "invalidate":
        cur = conn.execute(
            """
            UPDATE evidence_packs
            SET status='voided', releasable=0, hold_reason=?
            WHERE subject_id=?
              AND quota_version_id IS NOT NULL AND quota_version_id != ?
              AND status IN ('built','checked_ok','checked_fail')
            """,
            (hold_reason, subject_id, current_quota_id),
        )
    else:
        cur = conn.execute(
            """
            UPDATE evidence_packs
            SET releasable=0, hold_reason=?
            WHERE subject_id=?
              AND quota_version_id IS NOT NULL AND quota_version_id != ?
              AND status IN ('built','checked_ok','checked_fail')
            """,
            (hold_reason, subject_id, current_quota_id),
        )
    return int(cur.rowcount)
