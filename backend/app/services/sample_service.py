from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.db import connect
from app.engines.fixtures import load_fixture
from app.engines.sample import stratified_sample
from app.repositories import quota as quota_repo
from app.repositories import samples as sample_repo
from app.repositories import subjects as subject_repo


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def dashboard_stats() -> dict[str, Any]:
    conn = connect()
    try:
        return {
            "subject_count": conn.execute("SELECT COUNT(*) c FROM subjects").fetchone()["c"],
            "enabled_subjects": conn.execute(
                "SELECT COUNT(*) c FROM subjects WHERE enabled=1"
            ).fetchone()["c"],
            "sample_batch_count": conn.execute(
                "SELECT COUNT(*) c FROM sample_batches"
            ).fetchone()["c"],
            "shortfall_batches": conn.execute(
                "SELECT COUNT(*) c FROM sample_batches WHERE quota_met=0"
            ).fetchone()["c"],
            "pack_count": conn.execute("SELECT COUNT(*) c FROM evidence_packs").fetchone()["c"],
            "voided_packs": conn.execute(
                "SELECT COUNT(*) c FROM evidence_packs WHERE status='voided'"
            ).fetchone()["c"],
            "checklist_count": conn.execute(
                "SELECT COUNT(*) c FROM checklist_results"
            ).fetchone()["c"],
            "release_count": conn.execute(
                "SELECT COUNT(*) c FROM release_decisions"
            ).fetchone()["c"],
            "passed_releases": conn.execute(
                "SELECT COUNT(*) c FROM release_decisions WHERE decision='pass'"
            ).fetchone()["c"],
            "rejected_releases": conn.execute(
                "SELECT COUNT(*) c FROM release_decisions WHERE decision='reject'"
            ).fetchone()["c"],
        }
    finally:
        conn.close()


def list_subjects() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return subject_repo.list_subjects(conn)
    finally:
        conn.close()


def get_subject(subject_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        s = subject_repo.get_subject(conn, subject_id)
        if not s:
            raise HTTPException(status_code=404, detail="举证对象不存在")
        return s
    finally:
        conn.close()


def _attach_quota(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    versions = quota_repo.version_map(conn)
    for r in rows:
        qid = r.get("quota_version_id")
        r["quota_version"] = versions.get(qid, {}).get("version") if qid else None


def list_batches(limit: int = 50) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = sample_repo.list_batches(conn, limit=limit)
        subjects = {s["id"]: s for s in subject_repo.list_subjects(conn)}
        _attach_quota(conn, rows)
        for r in rows:
            s = subjects.get(r["subject_id"])
            r["subject_code"] = s["code"] if s else None
            r["subject_title"] = s["title"] if s else None
            r["strata"] = sample_repo.list_strata(conn, r["id"])
        return rows
    finally:
        conn.close()


def get_batch(batch_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        b = sample_repo.get_batch(conn, batch_id)
        if not b:
            raise HTTPException(status_code=404, detail="采样批次不存在")
        s = subject_repo.get_subject(conn, b["subject_id"])
        b["subject_code"] = s["code"] if s else None
        b["subject_title"] = s["title"] if s else None
        _attach_quota(conn, [b])
        b["strata"] = sample_repo.list_strata(conn, batch_id)
        b["quota_config"] = (
            quota_repo.get_by_id(conn, b["quota_version_id"]) if b["quota_version_id"] else None
        )
        b["rows"] = sample_repo.list_rows(conn, batch_id)
        return b
    finally:
        conn.close()


def run_sample(subject_id: int, seed: str = "proofpack-seed") -> dict[str, Any]:
    """
    分层配额采样：按夹具约定的分层字段与当前层配额逐层抽样，总样本为各层合并。
    严格策略下层不足配额 => 整次失败，不落批次；尽力策略 => 落批次并标记未满层。
    与配额变更互斥同成同败：落库前配额版本被改 => 本次失败，不留半层批次。
    """
    conn = connect()
    try:
        subject = subject_repo.get_subject(conn, subject_id)
        if not subject:
            raise HTTPException(status_code=404, detail="举证对象不存在")
        if not subject["enabled"]:
            raise HTTPException(status_code=400, detail="举证对象未启用")

        # —— 规划阶段（不落库）——
        quota = quota_repo.get_current(conn, subject_id)
        if not quota:
            raise HTTPException(status_code=400, detail="尚未配置分层配额，请先在设置中配置")
        strata_field, source = load_fixture(subject["fixture"])
        if not strata_field:
            raise HTTPException(status_code=400, detail="夹具未约定分层字段，无法按层配额采样")
        if quota["strata_field"] != strata_field:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"配额分层字段「{quota['strata_field']}」与夹具约定「{strata_field}」"
                    "不一致，请先更新配额配置"
                ),
            )
        source_strata = sorted({str(r.get(strata_field, "")) for r in source})
        missing = [s for s in source_strata if s not in quota["quotas"]]
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"源数据层 {', '.join(missing)} 缺少配额配置，请先更新配额",
            )

        plan = stratified_sample(source, strata_field, quota["quotas"], quota["quota_mode"], str(seed))
        strata_detail = [
            {
                "stratum": stratum,
                "source_count": p["source_count"],
                "target_count": p["target_count"],
                "sample_count": len(p["selected"]),
                "quota_met": len(p["selected"]) >= p["target_count"],
            }
            for stratum, p in sorted(plan.items())
        ]
        shortfalls = [s for s in strata_detail if not s["quota_met"]]
        if shortfalls and quota["shortfall_policy"] == "strict":
            detail = "；".join(
                f"层「{s['stratum']}」源{s['source_count']}条不足配额{s['target_count']}"
                for s in shortfalls
            )
            raise HTTPException(
                status_code=400,
                detail=f"严格策略：{detail}，整次采样失败，未落批次、无可打包样本",
            )

        quota_met_all = 0 if shortfalls else 1
        status = "ready" if quota_met_all else "ready_shortfall"
        selected_all = [
            (item, stratum)
            for stratum, p in sorted(plan.items())
            for item in p["selected"]
        ]
        sample_count = len(selected_all)
        effective_rate = round(sample_count / len(source), 6) if source else 0.0

        # —— 落库阶段：与配额变更互斥，同成同败 ——
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            raise HTTPException(
                status_code=409,
                detail="配额变更进行中，本次采样失败，未留下任何批次（同成同败）",
            )
        try:
            fresh = quota_repo.get_current(conn, subject_id)
            if not fresh or fresh["id"] != quota["id"]:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"采样期间配额已由 v{quota['version']} 变更，本次采样失败，"
                        "未留下半层样本批次或可放行包"
                    ),
                )
            now = _now()
            seq = conn.execute("SELECT COUNT(*) c FROM sample_batches").fetchone()["c"] + 1
            batch_no = f"SB-{now[:10].replace('-', '')}-{seq:04d}"
            batch_id = sample_repo.create_batch(
                conn,
                batch_no=batch_no,
                subject_id=subject_id,
                seed=str(seed),
                rate=effective_rate,
                source_count=len(source),
                sample_count=sample_count,
                status=status,
                created_at=now,
                quota_version_id=quota["id"],
                quota_met=quota_met_all,
            )
            sample_repo.insert_strata(conn, batch_id, strata_detail)
            sample_repo.insert_rows(conn, batch_id, selected_all)
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

        return {
            "id": batch_id,
            "batch_no": batch_no,
            "subject_id": subject_id,
            "subject_code": subject["code"],
            "subject_title": subject["title"],
            "seed": str(seed),
            "rate": effective_rate,
            "source_count": len(source),
            "sample_count": sample_count,
            "status": status,
            "created_at": now,
            "quota_version_id": quota["id"],
            "quota_version": quota["version"],
            "quota_met": quota_met_all,
            "strata": sample_repo.list_strata(conn, batch_id),
            "rows": sample_repo.list_rows(conn, batch_id),
        }
    finally:
        conn.close()
