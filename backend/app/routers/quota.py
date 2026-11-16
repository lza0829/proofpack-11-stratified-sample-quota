from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel

from app.services import quota_service as svc

router = APIRouter(tags=["quota"])


class QuotaUpdateBody(BaseModel):
    quota_mode: Literal["count", "rate"]
    quotas: dict[str, float]
    shortfall_policy: Literal["strict", "best_effort"]
    stale_policy: Literal["invalidate", "freeze"]


@router.get("/quota")
def list_quota():
    return svc.list_configs()


@router.get("/quota/{subject_id}")
def get_quota(subject_id: int):
    return svc.get_config(subject_id)


@router.put("/quota/{subject_id}")
def update_quota(subject_id: int, body: QuotaUpdateBody) -> dict[str, Any]:
    return svc.update_quota(
        subject_id,
        quota_mode=body.quota_mode,
        quotas=body.quotas,
        shortfall_policy=body.shortfall_policy,
        stale_policy=body.stale_policy,
    )
