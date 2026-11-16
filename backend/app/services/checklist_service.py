from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.db import connect
from app.repositories import checklist as check_repo
from app.repositories import packs as pack_repo
from app.repositories import samples as sample_repo


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


REQUIRED_FIELDS = ("business_key", "amount", "qty", "event_ts", "revision", "title")


def list_results(pack_id: int | None = None) -> list[dict[str, Any]]:
    conn = connect()
    try:
        return check_repo.list_results(conn, pack_id=pack_id)
    finally:
        conn.close()


def run_checklist(pack_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        pack = pack_repo.get_pack(conn, pack_id)
        if not pack:
            raise HTTPException(status_code=404, detail="证据包不存在")

        items = pack_repo.list_items(conn, pack_id)
        sample_rows = sample_repo.list_rows(conn, pack["batch_id"])
        now = _now()
        check_repo.clear_for_pack(conn, pack_id)

        results: list[dict[str, Any]] = []

        # 1. 条数一致
        count_ok = len(items) == len(sample_rows) and len(items) == pack["item_count"]
        detail_count = (
            f"包条目={len(items)}，样本行={len(sample_rows)}，包记录条数={pack['item_count']}"
        )
        check_repo.insert_result(
            conn,
            pack_id=pack_id,
            check_code="count_match",
            check_title="条数一致",
            passed=count_ok,
            detail=detail_count,
            created_at=now,
        )
        results.append(
            {
                "check_code": "count_match",
                "check_title": "条数一致",
                "passed": count_ok,
                "detail": detail_count,
            }
        )

        # 2. 键唯一
        keys = [it["business_key"] for it in items]
        unique_ok = len(keys) == len(set(keys))
        dup = sorted({k for k in keys if keys.count(k) > 1})
        detail_unique = "全部业务键唯一" if unique_ok else f"重复键: {', '.join(dup)}"
        check_repo.insert_result(
            conn,
            pack_id=pack_id,
            check_code="key_unique",
            check_title="键唯一",
            passed=unique_ok,
            detail=detail_unique,
            created_at=now,
        )
        results.append(
            {
                "check_code": "key_unique",
                "check_title": "键唯一",
                "passed": unique_ok,
                "detail": detail_unique,
            }
        )

        # 3. 必填非空
        missing: list[str] = []
        for it in items:
            summary = it.get("summary") if isinstance(it.get("summary"), dict) else {}
            merged = {
                "business_key": it.get("business_key"),
                "title": summary.get("title"),
                "amount": summary.get("amount"),
                "qty": summary.get("qty"),
                "event_ts": summary.get("event_ts"),
                "revision": summary.get("revision"),
            }
            for field in REQUIRED_FIELDS:
                val = merged.get(field)
                if val is None or (isinstance(val, str) and val.strip() == ""):
                    missing.append(f"{it.get('business_key')}.{field}")
        required_ok = len(missing) == 0
        detail_req = "必填字段齐全" if required_ok else f"缺失: {', '.join(missing[:20])}"
        check_repo.insert_result(
            conn,
            pack_id=pack_id,
            check_code="required_fields",
            check_title="必填非空",
            passed=required_ok,
            detail=detail_req,
            created_at=now,
        )
        results.append(
            {
                "check_code": "required_fields",
                "check_title": "必填非空",
                "passed": required_ok,
                "detail": detail_req,
            }
        )

        # 4. 分层配额互证：层配额 ↔ 批次分层明细 ↔ 样本行 ↔ 包条目
        strata = sample_repo.list_strata(conn, pack["batch_id"])
        batch = sample_repo.get_batch(conn, pack["batch_id"])
        problems: list[str] = []
        if not strata:
            problems.append("批次无分层明细，无法互证层配额")
        else:
            row_strata = [r.get("stratum") for r in sample_rows]
            if any(s is None for s in row_strata):
                problems.append("存在无分层标记的样本行")
            for s in strata:
                actual = sum(1 for x in row_strata if x == s["stratum"])
                if actual != s["sample_count"]:
                    problems.append(
                        f"层「{s['stratum']}」明细样本{s['sample_count']}条≠样本行{actual}条"
                    )
                expect_met = 1 if s["sample_count"] >= s["target_count"] else 0
                if int(s["quota_met"]) != expect_met:
                    problems.append(f"层「{s['stratum']}」达额标记与目标/样本数不符")
            if sum(s["sample_count"] for s in strata) != len(sample_rows):
                problems.append("各层样本数合计与样本行总数不符")
        if batch and batch.get("quota_version_id") != pack.get("quota_version_id"):
            problems.append("包与批次引用的配额版本不一致")
        strata_ok = not problems
        if strata_ok:
            detail_strata = "；".join(
                f"层「{s['stratum']}」源{s['source_count']}/目标{s['target_count']}/样本{s['sample_count']}/"
                + ("达额" if s["quota_met"] else "未满")
                for s in strata
            )
        else:
            detail_strata = "；".join(problems)
        check_repo.insert_result(
            conn,
            pack_id=pack_id,
            check_code="strata_quota",
            check_title="分层配额互证",
            passed=strata_ok,
            detail=detail_strata,
            created_at=now,
        )
        results.append(
            {
                "check_code": "strata_quota",
                "check_title": "分层配额互证",
                "passed": strata_ok,
                "detail": detail_strata,
            }
        )

        all_ok = all(r["passed"] for r in results)
        # 清单结果留痕，但不改写终态（作废/已放行/已驳回），避免作废包被复活
        if pack["status"] not in ("voided", "released", "rejected"):
            pack_repo.update_status(conn, pack_id, "checked_ok" if all_ok else "checked_fail")
        conn.commit()

        return {
            "pack_id": pack_id,
            "all_passed": all_ok,
            "item_count": len(items),
            "results": check_repo.list_results(conn, pack_id=pack_id),
        }
    finally:
        conn.close()
