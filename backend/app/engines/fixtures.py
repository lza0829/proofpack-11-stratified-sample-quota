from __future__ import annotations

import json
from typing import Any

from app.config import FIXTURES_DIR


def load_fixture(name: str) -> tuple[str | None, list[dict[str, Any]]]:
    """
    加载夹具，返回 (分层字段, 数据行)。
    新格式 {"strata_field": ..., "rows": [...]} 在夹具中约定分层字段；
    旧的纯数组格式没有分层约定，返回 (None, rows)。
    """
    path = FIXTURES_DIR / name
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return None, data
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        raise ValueError(f"fixture {name} 须为 {{strata_field, rows}} 对象")
    strata_field = data.get("strata_field")
    return (str(strata_field) if strata_field else None), data["rows"]


def fixture_strata(name: str) -> tuple[str | None, dict[str, int]]:
    """返回 (分层字段, {层值: 源条数})。"""
    strata_field, rows = load_fixture(name)
    counts: dict[str, int] = {}
    if strata_field:
        for r in rows:
            key = str(r.get(strata_field, ""))
            counts[key] = counts.get(key, 0) + 1
    return strata_field, dict(sorted(counts.items()))
