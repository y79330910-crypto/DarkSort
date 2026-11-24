from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from darksort.models import ScanIssue, ScanResult, ScanRoot, Source
from darksort.scanning.cache import ScanCache, signature
from darksort.scanning.content import inspect_resources
from darksort.scanning.metadata import parse_mod


def scan(
    roots: list[ScanRoot],
    cache_path: Path | None = None,
    *,
    progress: Callable[[int, int, str], None] = lambda *_: None,
    cancelled: Callable[[], bool] = lambda: False,
    refresh: bool = False,
) -> ScanResult:
    result = ScanResult()
    cache = None
    if cache_path:
        try:
            cache = ScanCache(cache_path)
        except (OSError, sqlite3.Error) as exc:
            result.issues.append(ScanIssue(str(cache_path), f"缓存不可用，改用直接扫描：{exc}"))
    candidates = []
    seen = set()
    try:
        for root in roots:
            if cancelled():
                result.cancelled = True
                return result
            try:
                if (root.path / "project.xml").is_file():
                    result.issues.append(ScanIssue(str(root.path), "请选择包含多个 Mod 的父目录。"))
                    continue
                for folder in sorted(root.path.iterdir(), key=lambda p: p.name.casefold()):
                    if cancelled():
                        result.cancelled = True
                        return result
                    if not folder.is_dir() or folder.name.startswith("."):
                        continue
                    if folder.name.casefold() in {"downloads", "temp", "downloading"}:
                        continue
                    key = (str(folder.resolve()).casefold(), root.source)
                    if key not in seen:
                        candidates.append((folder, root.source))
                        seen.add(key)
            except OSError as exc:
                result.issues.append(ScanIssue(str(root.path), f"无法读取 Mod 目录：{exc}"))
        total = len(candidates)
        for index, (folder, source) in enumerate(candidates):
            if cancelled():
                result.cancelled = True
                break
            progress(index, total, folder.name)
            try:
                stamp = signature(folder)
                mod = cache.get(folder, source, stamp) if cache and not refresh else None
                if mod is not None:
                    result.cache_hits += 1
                else:
                    mod = inspect_resources(parse_mod(folder, source), cancelled)
                    if cancelled():
                        result.cancelled = True
                        break
                    if cache:
                        cache.put(mod, stamp)
                result.mods.append(mod)
            except sqlite3.Error as exc:
                result.issues.append(ScanIssue(str(folder), f"缓存读写失败：{exc}"))
                if cache:
                    try:
                        cache.close()
                    except sqlite3.Error:
                        pass
                    cache = None
                result.mods.append(inspect_resources(parse_mod(folder, source), cancelled))
            except OSError as exc:
                result.issues.append(ScanIssue(str(folder), f"读取失败：{exc}"))
        progress(total, total, "完成")
        duplicates: dict[str, list] = {}
        for mod in result.mods:
            if mod.source == Source.WORKSHOP and mod.workshop_id:
                duplicates.setdefault(mod.workshop_id, []).append(mod)
        for workshop_id, mods in duplicates.items():
            if len(mods) > 1:
                result.issues.append(
                    ScanIssue(
                        workshop_id, "发现多个相同 Workshop ID 的目录，配置匹配时需要手动定位。"
                    )
                )
    finally:
        if cache:
            try:
                cache.close()
            except sqlite3.Error as exc:
                result.issues.append(ScanIssue(str(cache_path), f"缓存保存失败：{exc}"))
    return result
