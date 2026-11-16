from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.db import connect
from app.engines.fixtures import fixture_strata
from app.repositories import packs as pack_repo
from app.repositories import quota as quota_repo
from app.repositories import subjects as subject_repo

QUOTA_MODES = ("count", "rate")
# 不足配额的两种命运：单值枚举，同一时刻只生效一种，不做并存开关
SHORTFALL_POLICIES = ("strict", "best_effort")
# 配额变更对旧配额未放行包的两种处置：同为单值枚举互斥
STALE_POLICIES = ("invalidate", "freeze")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _subject_or_404(conn: sqlite3.Connection, subject_id: int) -> dict[str, Any]:
    s = subject_repo.get_subject(conn, subject_id)
    if not s:
        raise HTTPException(status_code=404, detail="举证对象不存在")
    return s


def _config_view(conn: sqlite3.Connection, subject: dict[str, Any]) -> dict[str, Any]:
    strata_field, strata_counts = fixture_strata(subject["fixture"])
    current = quota_repo.get_current(conn, subject["id"])
    return {
        "subject_id": subject["id"],
        "subject_code": subject["code"],
        "subject_title": subject["title"],
        "strata_field": strata_field,
        "strata": [
            {"stratum": k, "source_count": v} for k, v in sorted(strata_counts.items())
        ],
        "current": current,
        "versions": quota_repo.list_versions(conn, subject["id"]),
    }


def list_configs() -> list[dict[str, Any]]:
    conn = connect()
    try:
        out = []
        for s in subject_repo.list_subjects(conn):
            out.append(_config_view(conn, s))
        return out
    finally:
        conn.close()


def get_config(subject_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        subject = _subject_or_404(conn, subject_id)
        return _config_view(conn, subject)
    finally:
        conn.close()


def _validate(
    strata_field: str | None,
    strata_counts: dict[str, int],
    quota_mode: str,
    quotas: dict[str, Any],
    shortfall_policy: str,
    stale_policy: str,
) -> dict[str, float]:
    if not strata_field:
        raise HTTPException(status_code=400, detail="夹具未约定分层字段，无法配置分层配额")
    if quota_mode not in QUOTA_MODES:
        raise HTTPException(status_code=400, detail="配额模式须为 count（目标条数）或 rate（层内采样率）")
    if shortfall_policy not in SHORTFALL_POLICIES:
        raise HTTPException(status_code=400, detail="不足配额策略须为 strict 或 best_effort（二选一）")
    if stale_policy not in STALE_POLICIES:
        raise HTTPException(status_code=400, detail="旧配额处置策略须为 invalidate 或 freeze（二选一）")
    if not isinstance(quotas, dict) or not quotas:
        raise HTTPException(status_code=400, detail="须按层配置配额（quotas 非空）")

    source_strata = set(strata_counts)
    configured = set(quotas)
    missing = sorted(source_strata - configured)
    extra = sorted(configured - source_strata)
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"缺少层: {', '.join(missing)}")
        if extra:
            parts.append(f"源数据不存在的层: {', '.join(extra)}")
        raise HTTPException(
            status_code=400,
            detail=f"配额层须与夹具分层字段「{strata_field}」的层集合一一对应（{'；'.join(parts)}）",
        )

    clean: dict[str, float] = {}
    for stratum, value in quotas.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HTTPException(status_code=400, detail=f"层「{stratum}」配额须为数值")
        if quota_mode == "count":
            if int(value) != value or int(value) < 1:
                raise HTTPException(
                    status_code=400, detail=f"层「{stratum}」目标条数须为 ≥1 的整数"
                )
            clean[stratum] = int(value)
        else:
            if not (0 < float(value) <= 1):
                raise HTTPException(
                    status_code=400, detail=f"层「{stratum}」层内采样率须在 (0, 1] 之间"
                )
            clean[stratum] = float(value)
    return clean


def update_quota(
    subject_id: int,
    *,
    quota_mode: str,
    quotas: dict[str, Any],
    shortfall_policy: str,
    stale_policy: str,
) -> dict[str, Any]:
    """
    变更分层配额：产生新版本，并在同一事务内按 stale_policy 处置
    仍引用旧配额的未放行包（invalidate 作废 / freeze 冻结只读）。
    与分层采样互斥同成同败：采样进行中本操作失败，不留半层状态。
    """
    conn = connect()
    try:
        subject = _subject_or_404(conn, subject_id)
        strata_field, strata_counts = fixture_strata(subject["fixture"])
        clean_quotas = _validate(
            strata_field, strata_counts, quota_mode, quotas, shortfall_policy, stale_policy
        )

        current = quota_repo.get_current(conn, subject_id)
        if current and (
            current["quota_mode"] == quota_mode
            and current["quotas"] == clean_quotas
            and current["shortfall_policy"] == shortfall_policy
            and current["stale_policy"] == stale_policy
            and current["strata_field"] == strata_field
        ):
            return {
                "ok": True,
                "changed": False,
                "config": current,
                "affected_packs": 0,
                "detail": "配置未变化，未产生新版本",
            }

        now = _now()
        # 与采样落库互斥：BEGIN IMMEDIATE 拿不到写锁即失败，双方同成同败
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            raise HTTPException(
                status_code=409,
                detail="分层采样落库进行中，配额未变更，请稍后重试（同成同败）",
            )
        try:
            version = quota_repo.max_version(conn, subject_id) + 1
            quota_id = quota_repo.insert_config(
                conn,
                subject_id=subject_id,
                version=version,
                strata_field=strata_field,
                quota_mode=quota_mode,
                shortfall_policy=shortfall_policy,
                stale_policy=stale_policy,
                quotas=clean_quotas,
                created_at=now,
            )
            if stale_policy == "invalidate":
                hold_reason = f"配额已变更至 v{version}：按「改配额作废」策略，本包作废"
            else:
                hold_reason = (
                    f"配额已变更至 v{version}：旧配额包保留只读对照，一律禁止放行，"
                    "须按新配额重新采样打包"
                )
            affected = pack_repo.apply_stale_policy(
                conn,
                subject_id=subject_id,
                current_quota_id=quota_id,
                stale_policy=stale_policy,
                hold_reason=hold_reason,
            )
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

        config = quota_repo.get_by_id(conn, quota_id)
        return {
            "ok": True,
            "changed": True,
            "config": config,
            "affected_packs": affected,
            "detail": (
                f"配额已更新为 v{version}，"
                + (
                    f"作废旧配额未放行包 {affected} 个"
                    if stale_policy == "invalidate"
                    else f"冻结旧配额未放行包 {affected} 个（只读对照，禁止放行）"
                )
            ),
        }
    finally:
        conn.close()
