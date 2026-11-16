from __future__ import annotations

import json
from datetime import datetime, timezone

from app.db import connect
from app.engines.fixtures import fixture_strata

# 举证对象种子
SUBJECT_SEEDS = [
    {
        "code": "store_dayend",
        "title": "门店日结抽检",
        "domain": "store",
        "fixture": "store_dayend.json",
        "sample_rate_default": 0.3,
        "enabled": 1,
    },
    {
        "code": "channel_settle",
        "title": "渠道结算抽检",
        "domain": "channel",
        "fixture": "channel_settle.json",
        "sample_rate_default": 0.3,
        "enabled": 1,
    },
    {
        "code": "price_change",
        "title": "价目变更核证",
        "domain": "price",
        "fixture": "price_change.json",
        "sample_rate_default": 0.3,
        "enabled": 1,
    },
]

# 分层配额种子（version 1）：按夹具约定的分层字段配置每层目标条数。
# 不足配额策略与旧配额处置策略均为单值枚举，同一时刻只生效一种。
QUOTA_SEEDS = {
    "store_dayend": {
        "quota_mode": "count",
        "quotas": {"城区": 2, "近郊": 2, "远郊": 2},
        "shortfall_policy": "best_effort",
        "stale_policy": "freeze",
    },
    "channel_settle": {
        "quota_mode": "count",
        "quotas": {"A": 2, "B": 2, "C": 2},
        "shortfall_policy": "best_effort",
        "stale_policy": "freeze",
    },
    "price_change": {
        "quota_mode": "count",
        "quotas": {"低价": 3, "中价": 3, "高价": 2},
        "shortfall_policy": "best_effort",
        "stale_policy": "freeze",
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(
            """
            -- 举证对象
            CREATE TABLE IF NOT EXISTS subjects(
                id INTEGER PRIMARY KEY,
                code TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                domain TEXT NOT NULL,
                fixture TEXT NOT NULL,
                sample_rate_default REAL NOT NULL DEFAULT 0.3,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            -- 采样批次
            CREATE TABLE IF NOT EXISTS sample_batches(
                id INTEGER PRIMARY KEY,
                batch_no TEXT NOT NULL UNIQUE,
                subject_id INTEGER NOT NULL,
                seed TEXT NOT NULL,
                rate REAL NOT NULL,
                source_count INTEGER NOT NULL,
                sample_count INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(subject_id) REFERENCES subjects(id)
            );
            -- 样本行
            CREATE TABLE IF NOT EXISTS sample_rows(
                id INTEGER PRIMARY KEY,
                batch_id INTEGER NOT NULL,
                business_key TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                event_ts TEXT,
                revision INTEGER,
                stratum TEXT,
                FOREIGN KEY(batch_id) REFERENCES sample_batches(id)
            );
            -- 分层配额配置（按对象版本化，采样批次引用版本 id）
            CREATE TABLE IF NOT EXISTS quota_configs(
                id INTEGER PRIMARY KEY,
                subject_id INTEGER NOT NULL,
                version INTEGER NOT NULL,
                strata_field TEXT NOT NULL,
                quota_mode TEXT NOT NULL CHECK(quota_mode IN ('count','rate')),
                shortfall_policy TEXT NOT NULL CHECK(shortfall_policy IN ('strict','best_effort')),
                stale_policy TEXT NOT NULL CHECK(stale_policy IN ('invalidate','freeze')),
                quotas_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(subject_id, version),
                FOREIGN KEY(subject_id) REFERENCES subjects(id)
            );
            -- 批次分层明细：每层源条数、目标条数、样本数、是否触达配额
            CREATE TABLE IF NOT EXISTS sample_batch_strata(
                id INTEGER PRIMARY KEY,
                batch_id INTEGER NOT NULL,
                stratum TEXT NOT NULL,
                source_count INTEGER NOT NULL,
                target_count INTEGER NOT NULL,
                sample_count INTEGER NOT NULL,
                quota_met INTEGER NOT NULL,
                UNIQUE(batch_id, stratum),
                FOREIGN KEY(batch_id) REFERENCES sample_batches(id)
            );
            -- 证据包
            CREATE TABLE IF NOT EXISTS evidence_packs(
                id INTEGER PRIMARY KEY,
                pack_no TEXT NOT NULL UNIQUE,
                batch_id INTEGER NOT NULL,
                subject_id INTEGER NOT NULL,
                item_count INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                quota_version_id INTEGER,
                releasable INTEGER NOT NULL DEFAULT 1,
                hold_reason TEXT,
                FOREIGN KEY(batch_id) REFERENCES sample_batches(id),
                FOREIGN KEY(subject_id) REFERENCES subjects(id)
            );
            -- 证据包条目
            CREATE TABLE IF NOT EXISTS pack_items(
                id INTEGER PRIMARY KEY,
                pack_id INTEGER NOT NULL,
                business_key TEXT NOT NULL,
                summary_json TEXT NOT NULL,
                item_fingerprint TEXT NOT NULL,
                FOREIGN KEY(pack_id) REFERENCES evidence_packs(id)
            );
            -- 校验清单结果
            CREATE TABLE IF NOT EXISTS checklist_results(
                id INTEGER PRIMARY KEY,
                pack_id INTEGER NOT NULL,
                check_code TEXT NOT NULL,
                check_title TEXT NOT NULL,
                passed INTEGER NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(pack_id) REFERENCES evidence_packs(id)
            );
            -- 放行决策
            CREATE TABLE IF NOT EXISTS release_decisions(
                id INTEGER PRIMARY KEY,
                pack_id INTEGER NOT NULL,
                decision TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(pack_id) REFERENCES evidence_packs(id)
            );
            -- 设置
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            -- 影子试跑（空挂，后题用）
            CREATE TABLE IF NOT EXISTS shadow_runs(
                id INTEGER PRIMARY KEY,
                pack_id INTEGER,
                rule_version_id INTEGER,
                status TEXT,
                note TEXT,
                created_at TEXT
            );
            -- 规则版本（空挂，后题用）
            CREATE TABLE IF NOT EXISTS rule_versions(
                id INTEGER PRIMARY KEY,
                code TEXT,
                title TEXT,
                payload_json TEXT,
                active INTEGER DEFAULT 0,
                created_at TEXT
            );
            """
        )
        # 旧库迁移：补齐分层配额相关列
        _ensure_column(conn, "sample_batches", "quota_version_id", "quota_version_id INTEGER")
        _ensure_column(conn, "sample_batches", "quota_met", "quota_met INTEGER")
        _ensure_column(conn, "sample_rows", "stratum", "stratum TEXT")
        _ensure_column(conn, "evidence_packs", "quota_version_id", "quota_version_id INTEGER")
        _ensure_column(
            conn, "evidence_packs", "releasable", "releasable INTEGER NOT NULL DEFAULT 1"
        )
        _ensure_column(conn, "evidence_packs", "hold_reason", "hold_reason TEXT")
        # 旧平采批次产出的未放行包：无分层明细，不得标为可放行
        conn.execute(
            """
            UPDATE evidence_packs
            SET releasable=0, hold_reason='旧平采批次产出，无分层明细，不能按层配额放行'
            WHERE quota_version_id IS NULL
              AND status IN ('built','checked_ok','checked_fail')
            """
        )
        if conn.execute("SELECT COUNT(*) c FROM subjects").fetchone()["c"] == 0:
            now = _now()
            for s in SUBJECT_SEEDS:
                conn.execute(
                    """
                    INSERT INTO subjects(
                        code, title, domain, fixture, sample_rate_default, enabled, created_at
                    ) VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        s["code"],
                        s["title"],
                        s["domain"],
                        s["fixture"],
                        s["sample_rate_default"],
                        s["enabled"],
                        now,
                    ),
                )
        if conn.execute("SELECT COUNT(*) c FROM settings").fetchone()["c"] == 0:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?)",
                ("default_sample_rate", "0.3"),
            )
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?)",
                ("require_checklist_pass", "1"),
            )
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?)",
                ("sample_mode", "stratified"),
            )
        if conn.execute("SELECT COUNT(*) c FROM quota_configs").fetchone()["c"] == 0:
            now = _now()
            subjects = {
                r["code"]: r["id"]
                for r in conn.execute("SELECT id, code FROM subjects").fetchall()
            }
            for code, q in QUOTA_SEEDS.items():
                subject_id = subjects.get(code)
                if not subject_id:
                    continue
                fixture = next(s["fixture"] for s in SUBJECT_SEEDS if s["code"] == code)
                strata_field, _counts = fixture_strata(fixture)
                if not strata_field:
                    continue
                conn.execute(
                    """
                    INSERT INTO quota_configs(
                        subject_id, version, strata_field, quota_mode,
                        shortfall_policy, stale_policy, quotas_json, created_at
                    ) VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        subject_id,
                        1,
                        strata_field,
                        q["quota_mode"],
                        q["shortfall_policy"],
                        q["stale_policy"],
                        json.dumps(q["quotas"], ensure_ascii=False),
                        now,
                    ),
                )
        conn.commit()
    finally:
        conn.close()
