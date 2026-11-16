from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def subject_id(code: str) -> int:
    r = client.get("/api/subjects")
    assert r.status_code == 200
    return next(s["id"] for s in r.json() if s["code"] == code)


def run(code: str, seed: str = "seed-1"):
    return client.post("/api/samples/run", json={"subject_id": subject_id(code), "seed": seed})


def strata_by_name(resp_json) -> dict:
    return {s["stratum"]: s for s in resp_json["strata"]}


# ---------- 分层明细与互证 ----------

def test_stratified_run_writes_strata_details():
    r = run("store_dayend")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ready"
    assert body["quota_met"] == 1
    assert body["quota_version"] == 1

    st = strata_by_name(body)
    assert set(st) == {"城区", "近郊", "远郊"}
    assert st["城区"]["source_count"] == 4 and st["城区"]["target_count"] == 2
    assert st["近郊"]["source_count"] == 4 and st["近郊"]["target_count"] == 2
    assert st["远郊"]["source_count"] == 2 and st["远郊"]["target_count"] == 2
    assert all(s["quota_met"] == 1 for s in st.values())
    assert body["sample_count"] == 6

    # 样本行带层标记，且与分层明细逐层互证
    rows = body["rows"]
    assert len(rows) == 6
    assert all(row["stratum"] for row in rows)
    for name, s in st.items():
        assert sum(1 for row in rows if row["stratum"] == name) == s["sample_count"]

    # 批次详情同样给出分层明细与配额快照
    detail = client.get(f"/api/samples/{body['id']}").json()
    assert len(detail["strata"]) == 3
    assert detail["quota_config"]["quotas"] == {"城区": 2, "近郊": 2, "远郊": 2}
    assert detail["quota_config"]["strata_field"] == "region"


def test_same_seed_same_quota_reproducible():
    a = run("channel_settle", seed="seed-x").json()
    b = run("channel_settle", seed="seed-x").json()
    for name in ("A", "B", "C"):
        keys_a = sorted(r["business_key"] for r in a["rows"] if r["stratum"] == name)
        keys_b = sorted(r["business_key"] for r in b["rows"] if r["stratum"] == name)
        assert keys_a == keys_b, f"层 {name} 同种子同配额重跑不可复现"


# ---------- 不足配额的两种命运（互斥） ----------

def test_strict_shortfall_fails_whole_run_without_batch():
    sid = subject_id("price_change")
    r = client.put(
        f"/api/quota/{sid}",
        json={
            "quota_mode": "count",
            "quotas": {"低价": 3, "中价": 3, "高价": 2},
            "shortfall_policy": "strict",
            "stale_policy": "freeze",
        },
    )
    assert r.status_code == 200, r.text

    before = len(client.get("/api/samples").json())
    r = run("price_change")
    assert r.status_code == 400
    assert "严格策略" in r.json()["detail"] and "高价" in r.json()["detail"]
    # 整次失败：不落批次、无可打包样本
    after = client.get("/api/samples").json()
    assert len(after) == before
    assert all(b["subject_code"] != "price_change" for b in after)


def test_best_effort_shortfall_keeps_batch_but_blocks_release():
    # 种子配置：高价层源 1 条 < 配额 2，best_effort
    r = run("price_change")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ready_shortfall"
    assert body["quota_met"] == 0
    st = strata_by_name(body)
    assert st["高价"]["sample_count"] == 1 and st["高价"]["quota_met"] == 0
    assert st["低价"]["quota_met"] == 1

    # 未满批次允许打包保留样本与摸底清单，但包不得标为可放行
    r = client.post("/api/packs/build", json={"batch_id": body["id"]})
    assert r.status_code == 200, r.text
    pack = r.json()
    assert pack["releasable"] == 0
    assert "高价" in pack["hold_reason"]

    # 清单可跑（互证一致），但放行通过必须硬拒绝
    r = client.post("/api/checklist/run", json={"pack_id": pack["id"]})
    assert r.status_code == 200 and r.json()["all_passed"]
    r = client.post("/api/releases/decide", json={"pack_id": pack["id"], "decision": "pass"})
    assert r.status_code == 400
    assert "未满" in r.json()["detail"] and "高价" in r.json()["detail"]
    # 显式驳回仍然允许
    r = client.post(
        "/api/releases/decide",
        json={"pack_id": pack["id"], "decision": "reject", "reason": "层未满，退回摸底"},
    )
    assert r.status_code == 200


# ---------- 配额变更的两种处置（互斥） ----------

def _bump_store_quota(stale_policy: str):
    sid = subject_id("store_dayend")
    return client.put(
        f"/api/quota/{sid}",
        json={
            "quota_mode": "count",
            "quotas": {"城区": 3, "近郊": 2, "远郊": 2},
            "shortfall_policy": "best_effort",
            "stale_policy": stale_policy,
        },
    )


def test_quota_change_invalidate_voids_unreleased_packs():
    batch = run("store_dayend").json()
    pack = client.post("/api/packs/build", json={"batch_id": batch["id"]}).json()

    r = _bump_store_quota("invalidate")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] and body["config"]["version"] == 2
    assert body["affected_packs"] == 1

    pack_after = client.get(f"/api/packs/{pack['id']}").json()
    assert pack_after["status"] == "voided"
    assert pack_after["releasable"] == 0

    # 作废包一律不得再放行决策
    client.post("/api/checklist/run", json={"pack_id": pack["id"]})
    r = client.post("/api/releases/decide", json={"pack_id": pack["id"], "decision": "pass"})
    assert r.status_code == 400 and "作废" in r.json()["detail"]

    # 旧配额批次禁止再打包
    r = client.post("/api/packs/build", json={"batch_id": batch["id"]})
    assert r.status_code == 400 and "旧配额" in r.json()["detail"]

    # 重新采样走新配额
    new_batch = run("store_dayend", seed="seed-2").json()
    assert new_batch["quota_version"] == 2
    assert strata_by_name(new_batch)["城区"]["target_count"] == 3


def test_quota_change_freeze_keeps_readonly_but_blocks_release():
    batch = run("store_dayend").json()
    pack = client.post("/api/packs/build", json={"batch_id": batch["id"]}).json()
    client.post("/api/checklist/run", json={"pack_id": pack["id"]})

    r = _bump_store_quota("freeze")
    assert r.status_code == 200 and r.json()["affected_packs"] == 1

    # 保留只读对照：状态不动作废，但禁止放行
    pack_after = client.get(f"/api/packs/{pack['id']}").json()
    assert pack_after["status"] == "checked_ok"
    assert pack_after["releasable"] == 0
    assert "禁止放行" in pack_after["hold_reason"]

    r = client.post("/api/releases/decide", json={"pack_id": pack["id"], "decision": "pass"})
    assert r.status_code == 400 and "配额已变更" in r.json()["detail"]

    # 按新配额重新采样打包后可正常放行
    new_batch = run("store_dayend", seed="seed-2").json()
    new_pack = client.post("/api/packs/build", json={"batch_id": new_batch["id"]}).json()
    assert new_pack["releasable"] == 1
    r = client.post("/api/checklist/run", json={"pack_id": new_pack["id"]})
    assert r.json()["all_passed"]
    r = client.post("/api/releases/decide", json={"pack_id": new_pack["id"], "decision": "pass"})
    assert r.status_code == 200, r.text
    assert client.get(f"/api/packs/{new_pack['id']}").json()["status"] == "released"


# ---------- 改配额与采样同成同败 ----------

def test_quota_change_during_sampling_fails_run(monkeypatch):
    from app.services import quota_service, sample_service

    sid = subject_id("store_dayend")
    original = sample_service.load_fixture
    calls = {"n": 0}

    def hook(name):
        data = original(name)
        calls["n"] += 1
        if calls["n"] == 1:
            # 采样规划完成后、落库前，配额被改成 v2
            quota_service.update_quota(
                sid,
                quota_mode="count",
                quotas={"城区": 3, "近郊": 2, "远郊": 2},
                shortfall_policy="best_effort",
                stale_policy="freeze",
            )
        return data

    monkeypatch.setattr(sample_service, "load_fixture", hook)
    r = run("store_dayend")
    assert r.status_code == 409
    assert "配额" in r.json()["detail"]
    # 不留半层样本批次
    assert client.get("/api/samples").json() == []


# ---------- 无分层明细不得声称按层配额 ----------

def test_legacy_batch_without_strata_cannot_pack_or_release():
    from app.db import connect
    from app.repositories import packs as pack_repo
    from app.repositories import samples as sample_repo

    sid = subject_id("store_dayend")
    conn = connect()
    try:
        bid = sample_repo.create_batch(
            conn,
            batch_no="SB-LEGACY-1",
            subject_id=sid,
            seed="s",
            rate=0.3,
            source_count=10,
            sample_count=1,
            status="ready",
            created_at="2026-01-01T00:00:00Z",
        )
        sample_repo.insert_rows(
            conn,
            bid,
            [
                (
                    {
                        "business_key": "SD-1001",
                        "title": "城东店日结",
                        "amount": 12850.5,
                        "qty": 312,
                        "event_ts": "2026-03-01T18:00:00Z",
                        "revision": 1,
                    },
                    None,
                )
            ],
        )
        pid = pack_repo.create_pack(
            conn,
            pack_no="EP-LEGACY-1",
            batch_id=bid,
            subject_id=sid,
            item_count=1,
            fingerprint="f" * 64,
            status="built",
            created_at="2026-01-01T00:00:00Z",
        )
        conn.commit()
    finally:
        conn.close()

    # 旧平采批次禁止打包
    r = client.post("/api/packs/build", json={"batch_id": bid})
    assert r.status_code == 400 and "无分层明细" in r.json()["detail"]

    # 存量无明细包：互证校验失败，放行硬拒绝
    r = client.post("/api/checklist/run", json={"pack_id": pid})
    checks = {c["check_code"]: c for c in r.json()["results"]}
    assert checks["strata_quota"]["passed"] == 0
    assert "无分层明细" in checks["strata_quota"]["detail"]
    r = client.post("/api/releases/decide", json={"pack_id": pid, "decision": "pass"})
    assert r.status_code == 400 and "无分层明细" in r.json()["detail"]


# ---------- 配额配置校验 ----------

def test_quota_update_validation():
    sid = subject_id("store_dayend")
    base = {
        "quota_mode": "count",
        "quotas": {"城区": 2, "近郊": 2, "远郊": 2},
        "shortfall_policy": "best_effort",
        "stale_policy": "freeze",
    }
    # 层集合必须与夹具一一对应
    r = client.put(f"/api/quota/{sid}", json={**base, "quotas": {"城区": 2, "近郊": 2}})
    assert r.status_code == 400 and "远郊" in r.json()["detail"]
    r = client.put(
        f"/api/quota/{sid}", json={**base, "quotas": {**base["quotas"], "月球": 1}}
    )
    assert r.status_code == 400 and "月球" in r.json()["detail"]
    # 目标条数须为 ≥1 整数
    r = client.put(
        f"/api/quota/{sid}", json={**base, "quotas": {"城区": 0, "近郊": 2, "远郊": 2}}
    )
    assert r.status_code == 400
    # 两种命运是单值枚举，非法值直接 422
    r = client.put(f"/api/quota/{sid}", json={**base, "shortfall_policy": "both"})
    assert r.status_code == 422
    r = client.put(f"/api/quota/{sid}", json={**base, "stale_policy": "both"})
    assert r.status_code == 422
    # 配置未变化不产生新版本
    r = client.put(f"/api/quota/{sid}", json=base)
    assert r.status_code == 200 and r.json()["changed"] is False


def test_rate_mode_quota():
    sid = subject_id("price_change")
    r = client.put(
        f"/api/quota/{sid}",
        json={
            "quota_mode": "rate",
            "quotas": {"低价": 0.5, "中价": 0.5, "高价": 1.0},
            "shortfall_policy": "best_effort",
            "stale_policy": "freeze",
        },
    )
    assert r.status_code == 200, r.text
    body = run("price_change").json()
    st = strata_by_name(body)
    assert st["低价"]["target_count"] == 3  # ceil(0.5*6)
    assert st["中价"]["target_count"] == 3  # ceil(0.5*5)
    assert st["高价"]["target_count"] == 1  # ceil(1.0*1)
    assert body["quota_met"] == 1


# ---------- 互证：明细被篡改时清单能发现 ----------

def test_checklist_detects_strata_tampering():
    batch = run("store_dayend").json()
    pack = client.post("/api/packs/build", json={"batch_id": batch["id"]}).json()

    from app.db import connect

    conn = connect()
    try:
        conn.execute("DELETE FROM sample_rows WHERE batch_id=? LIMIT 1", (batch["id"],))
        conn.commit()
    finally:
        conn.close()

    r = client.post("/api/checklist/run", json={"pack_id": pack["id"]})
    checks = {c["check_code"]: c for c in r.json()["results"]}
    assert checks["strata_quota"]["passed"] == 0
    assert r.json()["all_passed"] is False
    r = client.post("/api/releases/decide", json={"pack_id": pack["id"], "decision": "pass"})
    assert r.status_code == 400
