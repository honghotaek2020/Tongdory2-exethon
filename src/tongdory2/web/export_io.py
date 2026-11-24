"""목록·최종 목록표 파일 저장(방식 1: 선택한 폴더 직접 저장 또는 브라우저 다운로드) 공용 유틸."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

from fastapi.responses import JSONResponse, Response

from tongdory2.fsx.paths import first_symlink_component, real, tilde
from tongdory2.fsx.roots import is_denied_path
from tongdory2.fsx.scan import SKIP_DIRS
from tongdory2.messages import msg
from tongdory2.spec.normalize import TOP_IDS, parse
from tongdory2.spec.structure_input import save_file_no_overwrite

MEDIA_TYPES = {
    "json": "application/json; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
}


def validate_save_dir(raw_path: str, t) -> Path:
    value = raw_path.strip()
    if value == "~" or value.startswith("~/"):
        value = str(t.home) + value[1:]
    p = Path(value)
    if not p.is_absolute() or ".." in p.parts:
        raise ValueError(msg("export.err.need_abs_path"))
    if first_symlink_component(p, Path(p.anchor)):
        raise ValueError(msg("export.err.no_symlink"))
    folder = real(p)
    if folder == Path(folder.anchor):
        raise ValueError(msg("export.err.no_drive_root"))
    if is_denied_path(folder, t.home, t.app_data):
        raise ValueError(msg("export.err.denied_dir"))
    if any(part.casefold() in SKIP_DIRS or TOP_IDS.get(parse(part).key) == "src" for part in folder.parts):
        raise ValueError(msg("export.err.skip_or_src_dir"))
    if not folder.is_dir() or not os.access(folder, os.W_OK | os.X_OK):
        raise ValueError(msg("export.err.not_writable"))
    return folder


def build_download_response(content: str, filename: str, ext: str) -> Response:
    clean_ext = ext.lstrip(".").lower()
    media = MEDIA_TYPES.get(clean_ext, "text/plain; charset=utf-8")
    ascii_fallback = f"tongdory_export.{clean_ext}"
    encoded_name = quote(filename, safe="")
    headers = {
        "Content-Disposition": f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{encoded_name}',
        "X-Content-Type-Options": "nosniff",
    }
    return Response(content=content.encode("utf-8"), media_type=media, headers=headers)


def export_text_or_save(content: str, filename: str, ext: str, save_dir: str, t) -> Response:
    if save_dir.strip():
        folder = validate_save_dir(save_dir, t)
        saved = save_file_no_overwrite(folder, filename, content)
        return JSONResponse(
            {
                "ok": True,
                "filename": saved.name,
                "saved_path": tilde(saved, t.home),
            }
        )
    return build_download_response(content, filename, ext)
