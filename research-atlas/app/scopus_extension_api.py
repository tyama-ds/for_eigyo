"""Download the audited Scopus companion files, never arbitrary workspace files."""
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

router = APIRouter()
EXTENSION_ROOT = Path(__file__).resolve().parent.parent / "browser-extension" / "scopus-dates"
EXTENSION_FILES = (
    "manifest.json", "popup.html", "popup.js", "panel.html", "panel.js",
    "panel.css", "core.js", "README.md",
)


@router.get("/api/scopus-browser-extension")
def download_extension():
    buffer = BytesIO()
    try:
        with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
            for filename in EXTENSION_FILES:
                path = EXTENSION_ROOT / filename
                if path.is_symlink():
                    raise FileNotFoundError(filename)
                info = ZipInfo(f"research-atlas-scopus-dates/{filename}")
                info.compress_type = ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())
    except FileNotFoundError:
        raise HTTPException(503, "Scopus補助拡張のファイルが不足しています。アプリ一式を更新してください。") from None
    return Response(buffer.getvalue(), media_type="application/zip", headers={
        "Content-Disposition": 'attachment; filename="research-atlas-scopus-dates.zip"',
    })
