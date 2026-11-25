from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from darksort.models import Mod, Source, path_key

CACHE_VERSION = 4


def signature(folder: Path) -> str:
    """Metadata-only signatures: never walk all game assets or hash large mods."""
    pieces = [str(CACHE_VERSION)]
    manifest = folder / "modfiles.txt"
    sources = [folder, folder / "project.xml", manifest]
    if not manifest.is_file():
        sources.extend(folder / name for name in ("fonts", "localization", "heroes", "dungeons", "monsters"))
    for file in sources:
        try:
            stat = file.stat()
            pieces.append(f"{stat.st_mtime_ns}:{stat.st_size}")
        except OSError:
            pieces.append("missing")
    return ":".join(pieces)


class ScanCache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=2)
        try:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS mod_cache "
                "(key TEXT PRIMARY KEY, signature TEXT NOT NULL, payload TEXT NOT NULL)"
            )
        except Exception:
            self.connection.close()
            raise

    def get(self, folder: Path, source: Source, stamp: str) -> Mod | None:
        key = f"{source.value}:{path_key(folder)}"
        row = self.connection.execute(
            "SELECT payload FROM mod_cache WHERE key=? AND signature=?", (key, stamp)
        ).fetchone()
        if not row:
            return None
        try:
            mod = Mod.from_dict(json.loads(row[0]))
            # Older scans could cache a blank PublishedFileId as an empty string.
            if mod.key != key or mod.workshop_id == "":
                return None
            if mod.preview_path and not Path(mod.preview_path).is_file():
                return None
            return mod
        except (ValueError, KeyError, TypeError):
            return None

    def put(self, mod: Mod, stamp: str) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO mod_cache VALUES (?, ?, ?)",
            (mod.key, stamp, json.dumps(mod.to_dict(), ensure_ascii=False)),
        )

    def close(self) -> None:
        try:
            self.connection.commit()
        finally:
            self.connection.close()
