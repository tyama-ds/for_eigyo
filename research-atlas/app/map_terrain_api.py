"""Read-only terrain projection of an existing saved technology landscape."""
from fastapi import APIRouter

from . import storage
from .map_terrain import build_terrain


router = APIRouter()


@router.get("/api/results/{result_id}/terrain")
def get_terrain(result_id: str):
    result = storage.read("results", result_id)
    landscape = result.get("map")
    nodes = landscape.get("nodes", []) if isinstance(landscape, dict) else []
    return build_terrain(nodes)
