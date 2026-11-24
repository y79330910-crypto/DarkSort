from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from platformdirs import user_data_path


def default_data_dir() -> Path:
    return user_data_path("DarkSort", appauthor=False, roaming=False)


def atomic_json(path: Path, data: dict, *, max_bytes: int | None = None) -> None:
    """Keep an existing config intact when serialization or replacement fails."""
    payload = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    if max_bytes is not None and len(payload) > max_bytes:
        raise OSError("文件太大，无法保存更多设置。")
    atomic_bytes(path, payload)


def atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_json(path: Path, *, max_bytes: int = 8 * 1024 * 1024) -> dict:
    if path.stat().st_size > max_bytes:
        raise ValueError("文件过大，请选择 DarkSort 导出的配置文件。")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("配置的顶层必须是 JSON 对象。")
    return data
