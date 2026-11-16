from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.db import connect
from app.engines.checksum import item_fingerprint, pack_fingerprint
from app.repositories import packs as pack_repo
from app.repositories import quota as quota_repo
from app.repositories import samples as sample_repo
from app.repositories import subjects as subject_repo


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _attach_quota(conn, rows: list[dict[str, Any]]) -> None:
    versions = quota_repo.version_map(conn)
    for r in rows:
        qid = r.get("quota_version_id")
        r["quota_version"] = versions.get(qid, {}).get("version") if qid else None


def list_packs(limit: int = 50) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = pack_repo.list_packs(conn, limit=limit)
        subjects = {s["id"]: s for s in subject_repo.list_subjects(conn)}
        _attach_quota(conn, rows)
        for r in rows:
            s = subjects.get(r["subject_id"])
            r["subject_code"] = s["code"] if s else None
            r["subject_title"] = s["title"] if s else None
        return rows
    finally:
        conn.close()


def get_pack(pack_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        p = pack_repo.get_pack(conn, pack_id)
        if not p:
            raise HTTPException(status_code=404, detail="证据包不存在")
        s = subject_repo.get_subject(conn, p["subject_id"])
        p["subject_code"] = s["code"] if s else None
        p["subject_title"] = s["title"] if s else None
        _attach_quota(conn, [p])
        p["items"] = pack_repo.list_items(conn, pack_id)
        return p
    finally:
        conn.close()


def build_pack(batch_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        batch = sample_repo.get_batch(conn, batch_id)
        if not batch:
            raise HTTPException(status_code=404, detail="采样批次不存在")
        if not batch["quota_version_id"]:
            raise HTTPException(
                status_code=400,
                detail="该批次为旧平采批次，无分层明细，不能按层配额打包",
            )
        strata = sample_repo.list_strata(conn, batch_id)
        if not strata:
            raise HTTPException(
                status_code=400,
                detail="采样批次无分层明细，不能声称已按层配额，禁止打包",
            )
        current = quota_repo.get_current(conn, batch["subject_id"])
        if not current or current["id"] != batch["quota_version_id"]:
            versions = quota_repo.version_map(conn)
            bv = versions.get(batch["quota_version_id"], {}).get("version", "?")
            cv = current["version"] if current else "无"
            raise HTTPException(
                status_code=400,
                detail=(
                    f"批次引用旧配额 v{bv}（当前 v{cv}），请按新配额重新采样后再打包"
                ),
            )
        rows = sample_repo.list_rows(conn, batch_id)
        if not rows:
            raise HTTPException(status_code=400, detail="采样批次无样本行")

        # 尽力未满批次：允许打包保留样本与摸底清单，但不得标为可放行
        unmet = [s for s in strata if not s["quota_met"]]
        if unmet:
            releasable = 0
            hold_reason = "尽力策略未满批次，不得放行：" + "；".join(
                f"层「{s['stratum']}」源{s['source_count']}/目标{s['target_count']}/实抽{s['sample_count']}"
                for s in unmet
            )
        else:
            releasable = 1
            hold_reason = None

        items_src = []
        for r in rows:
            payload = r.get("payload") if isinstance(r.get("payload"), dict) else {}
            items_src.append(
                {
                    "business_key": r["business_key"],
                    "title": payload.get("title"),
                    "amount": payload.get("amount"),
                    "qty": payload.get("qty"),
                    "event_ts": payload.get("event_ts") or r.get("event_ts"),
                    "revision": payload.get("revision") if payload.get("revision") is not None else r.get("revision"),
                }
            )

        fp = pack_fingerprint(items_src)
        now = _now()
        seq = conn.execute("SELECT COUNT(*) c FROM evidence_packs").fetchone()["c"] + 1
        pack_no = f"EP-{now[:10].replace('-', '')}-{seq:04d}"

        pack_items = []
        for it in items_src:
            summary = {
                "title": it.get("title"),
                "amount": it.get("amount"),
                "qty": it.get("qty"),
                "event_ts": it.get("event_ts"),
                "revision": it.get("revision"),
            }
            pack_items.append(
                {
                    "business_key": it["business_key"],
                    "summary": summary,
                    "item_fingerprint": item_fingerprint(it),
                }
            )

        pack_id = pack_repo.create_pack(
            conn,
            pack_no=pack_no,
            batch_id=batch_id,
            subject_id=batch["subject_id"],
            item_count=len(pack_items),
            fingerprint=fp,
            status="built",
            created_at=now,
            quota_version_id=batch["quota_version_id"],
            releasable=releasable,
            hold_reason=hold_reason,
        )
        pack_repo.insert_items(conn, pack_id, pack_items)
        conn.commit()

        subject = subject_repo.get_subject(conn, batch["subject_id"])
        return {
            "id": pack_id,
            "pack_no": pack_no,
            "batch_id": batch_id,
            "subject_id": batch["subject_id"],
            "subject_code": subject["code"] if subject else None,
            "subject_title": subject["title"] if subject else None,
            "item_count": len(pack_items),
            "fingerprint": fp,
            "status": "built",
            "created_at": now,
            "quota_version_id": batch["quota_version_id"],
            "quota_version": current["version"],
            "releasable": releasable,
            "hold_reason": hold_reason,
            "items": pack_repo.list_items(conn, pack_id),
        }
    finally:
        conn.close()
