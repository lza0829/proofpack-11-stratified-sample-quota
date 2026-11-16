from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.db import connect
from app.repositories import checklist as check_repo
from app.repositories import packs as pack_repo
from app.repositories import quota as quota_repo
from app.repositories import releases as release_repo
from app.repositories import samples as sample_repo
from app.repositories import settings as settings_repo


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def list_releases(limit: int = 50) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = release_repo.list_decisions(conn, limit=limit)
        packs = {p["id"]: p for p in pack_repo.list_packs(conn, limit=500)}
        for r in rows:
            p = packs.get(r["pack_id"])
            r["pack_no"] = p["pack_no"] if p else None
            r["pack_status"] = p["status"] if p else None
        return rows
    finally:
        conn.close()


def list_settings() -> dict[str, str]:
    conn = connect()
    try:
        return settings_repo.list_settings(conn)
    finally:
        conn.close()


def _quota_gate(conn, pack: dict[str, Any]) -> None:
    """放行通过的层配额硬门禁：任一不满足即硬拒绝，原因与批次分层明细互证。"""
    if not pack["quota_version_id"]:
        raise HTTPException(
            status_code=400,
            detail="该包无分层明细（旧平采批次产出），不能按层配额放行",
        )
    strata = sample_repo.list_strata(conn, pack["batch_id"])
    if not strata:
        raise HTTPException(
            status_code=400,
            detail="批次无分层明细，不能声称已按层配额，禁止放行",
        )
    unmet = [s for s in strata if not s["quota_met"]]
    if unmet:
        detail = "；".join(
            f"层「{s['stratum']}」源{s['source_count']}/目标{s['target_count']}/实抽{s['sample_count']}"
            for s in unmet
        )
        raise HTTPException(
            status_code=400,
            detail=f"尽力未满批次生成的包不得放行（{detail}），须按配额重新采样",
        )
    current = quota_repo.get_current(conn, pack["subject_id"])
    if not current or current["id"] != pack["quota_version_id"]:
        versions = quota_repo.version_map(conn)
        pv = versions.get(pack["quota_version_id"], {}).get("version", "?")
        cv = current["version"] if current else "无"
        raise HTTPException(
            status_code=400,
            detail=f"配额已变更（包引用 v{pv}，当前 v{cv}），旧配额包一律禁止放行，须按新配额重新采样打包",
        )
    if not pack["releasable"]:
        raise HTTPException(
            status_code=400,
            detail=pack["hold_reason"] or "该包被标记为不可放行",
        )


def decide(pack_id: int, decision: str, reason: str | None = None) -> dict[str, Any]:
    decision = (decision or "").strip().lower()
    if decision not in ("pass", "reject"):
        raise HTTPException(status_code=400, detail="决策须为 pass 或 reject")

    conn = connect()
    try:
        pack = pack_repo.get_pack(conn, pack_id)
        if not pack:
            raise HTTPException(status_code=404, detail="证据包不存在")
        if pack["status"] == "voided":
            raise HTTPException(
                status_code=400,
                detail=pack["hold_reason"] or "包已作废，不能再做放行决策",
            )

        results = check_repo.list_results(conn, pack_id=pack_id)
        if not results:
            raise HTTPException(status_code=400, detail="请先跑校验清单")

        all_ok = check_repo.all_passed(conn, pack_id)
        require = settings_repo.get_setting(conn, "require_checklist_pass", "1") == "1"

        if decision == "pass":
            _quota_gate(conn, pack)
            if require and not all_ok:
                raise HTTPException(
                    status_code=400,
                    detail="清单未全部通过，不能放行",
                )
            if not reason:
                reason = "清单全部通过，层配额互证一致，予以放行"
            status = "released"
        else:
            if not reason or not str(reason).strip():
                raise HTTPException(status_code=400, detail="驳回须填写原因")
            status = "rejected"

        now = _now()
        rid = release_repo.create_decision(
            conn,
            pack_id=pack_id,
            decision=decision,
            reason=reason,
            created_at=now,
        )
        pack_repo.update_status(conn, pack_id, status)
        conn.commit()

        return {
            "id": rid,
            "pack_id": pack_id,
            "pack_no": pack["pack_no"],
            "decision": decision,
            "reason": reason,
            "created_at": now,
            "checklist_all_passed": all_ok,
        }
    finally:
        conn.close()
