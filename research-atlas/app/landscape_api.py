"""Read-only shared-coordinate temporal landscape endpoints."""
from typing import Literal

from fastapi import APIRouter

from .landscape import build_landscape

router = APIRouter()


@router.get("/api/results/{result_id}/landscape")
def get_landscape(result_id: str, projection: Literal["auto", "tsne", "pca", "umap"] = "auto",
                  interval: Literal["year", "quarter", "month"] = "year"):
    return build_landscape(result_id, projection=projection, interval=interval)
