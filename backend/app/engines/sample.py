from __future__ import annotations

import hashlib
import math
from typing import Any


def _bucket(seed: str, business_key: str) -> float:
    """Deterministic 0..1 bucket from seed + key."""
    digest = hashlib.sha256(f"{seed}|{business_key}".encode("utf-8")).hexdigest()
    # use first 12 hex chars -> int -> [0, 1)
    n = int(digest[:12], 16)
    return n / float(16**12)


def target_for(quota_mode: str, quota_value: float, source_count: int) -> int:
    """层目标条数：count 模式取配额条数；rate 模式按层内采样率向上取整。"""
    if quota_mode == "count":
        return int(quota_value)
    if source_count <= 0:
        return 0
    return min(source_count, max(1, math.ceil(float(quota_value) * source_count)))


def stratified_sample(
    rows: list[dict[str, Any]],
    strata_field: str,
    quotas: dict[str, float],
    quota_mode: str,
    seed: str,
) -> dict[str, dict[str, Any]]:
    """
    按层确定性抽样：同一种子 + 同一配额 => 各层样本集合可复现。

    每层内按 (bucket(seed|stratum, key), key) 排序取前 target 条；
    源不足配额时取尽源数据，由调用方按 shortfall_policy 处置。

    返回 {stratum: {"source_count", "target_count", "selected"}}，
    selected 按 business_key 排序，总样本为各层 selected 合并。
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(str(r.get(strata_field, "")), []).append(r)

    plan: dict[str, dict[str, Any]] = {}
    for stratum in sorted(groups):
        members = sorted(groups[stratum], key=lambda x: str(x.get("business_key", "")))
        n = len(members)
        target = target_for(quota_mode, quotas[stratum], n)
        ranked = sorted(
            members,
            key=lambda x: (
                _bucket(f"{seed}|{stratum}", str(x.get("business_key", ""))),
                str(x.get("business_key", "")),
            ),
        )
        selected = sorted(ranked[: min(target, n)], key=lambda x: str(x.get("business_key", "")))
        plan[stratum] = {
            "source_count": n,
            "target_count": target,
            "selected": selected,
        }
    return plan
