from __future__ import annotations

import hashlib
import os
import stat
from collections import defaultdict
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from darksort.models import Mod, ResolvedEntry

MAX_MANIFEST = 16 * 1024 * 1024
MAX_COMPARE_BYTES = 64 * 1024 * 1024
METADATA = {"modfiles.txt", "project.xml", "preview_icon.png", "preview.png", "preview.jpg"}


class FileStatus(StrEnum):
    MISSING = "清单中的文件缺失"
    ADDITIVE = "资源可叠加 · 按定义解析"
    HERO_IDENTICAL = "英雄文件 · 相同内容"
    OVERWRITE = "英雄文件 · 预计覆盖"
    IDENTICAL = "文件内容相同"
    UNKNOWN = "同路径候选"


@dataclass(frozen=True)
class Provider:
    rank: int
    mod: Mod
    relative: str
    size: int | None
    listed: bool | None

    @property
    def path(self) -> Path:
        return Path(self.mod.path) / self.relative


@dataclass(frozen=True)
class Conflict:
    path: str
    providers: tuple[Provider, ...]
    status: FileStatus
    winner: Provider | None = None


@dataclass(frozen=True)
class ConflictReport:
    conflicts: tuple[Conflict, ...] = ()
    issues: tuple[str, ...] = ()
    cancelled: bool = False
    unavailable: tuple[str, ...] = ()
    failed: bool = False


def reorder_conflicts(report: ConflictReport, entries: list[ResolvedEntry]) -> ConflictReport:
    """Update priority from the same set of scanned installations without reading files again."""
    ranks = {}
    for rank, row in enumerate(entries, 1):
        if row.mod:
            ranks.setdefault(row.mod.key, rank)
    conflicts = []
    for hit in report.conflicts:
        providers = tuple(sorted(
            (replace(provider, rank=ranks[provider.mod.key]) for provider in hit.providers),
            key=lambda provider: provider.rank,
        ))
        winner = next(provider for provider in providers if provider.listed) if hit.winner else None
        conflicts.append(replace(hit, providers=providers, winner=winner))
    return replace(report, conflicts=tuple(conflicts))


def safe_relative(text: str) -> str | None:
    parts = text.replace("\\", "/").split("/")
    if (
        any(part in ("", ".", "..") or ":" in part for part in parts)
        or any(ord(char) < 32 for char in text)
    ):
        return None
    return "/".join(parts)


def read_manifest(path: Path) -> set[str]:
    if path.is_symlink() or path.is_junction() or path.stat().st_size > MAX_MANIFEST:
        raise ValueError("清单路径或大小不支持。")
    result = set()
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        parts = line.strip().rsplit(maxsplit=1)
        if len(parts) != 2 or not parts[1].isascii() or not parts[1].isdecimal():
            raise ValueError(f"第 {number} 行应为相对路径和文件大小。")
        relative = safe_relative(parts[0])
        if relative is None:
            raise ValueError(f"第 {number} 行包含不安全的路径。")
        if relative.casefold() not in METADATA:
            result.add(relative)
    return result


def file_inventory(root: Path, cancelled, issues: list[str]) -> dict[str, int]:
    result = {}
    for directory, folders, files in os.walk(
        root, followlinks=False, onerror=lambda exc: issues.append(str(exc))
    ):
        if cancelled():
            break
        parent = Path(directory)
        folders[:] = [
            name for name in folders
            if not name.startswith(".") and name.casefold() != "_template"
            and not (parent / name).is_symlink() and not (parent / name).is_junction()
        ]
        for name in files:
            if cancelled():
                break
            relative = (parent / name).relative_to(root).as_posix()
            if relative.casefold() in METADATA or name.startswith("."):
                continue
            path = parent / name
            try:
                info = path.lstat()
                if (
                    stat.S_ISREG(info.st_mode)
                    and not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    result[relative] = info.st_size
            except OSError as exc:
                issues.append(f"{relative} · {exc}")
    return result


def proven_hero_path(relative: str) -> bool:
    parts = relative.split("/")
    return len(parts) == 3 and parts[0] == "heroes" and parts[2] in {
        f"{parts[1]}.info.darkest", f"{parts[1]}.art.darkest", f"{parts[1]}.override.darkest",
    }


def additive_resource(relative: str) -> bool:
    return (
        relative.startswith("curios/") and relative.endswith("curio_type_library.csv")
    ) or (
        relative.startswith("campaign/town/districts/") and relative.endswith("districts.json")
    )


def same_content(providers: tuple[Provider, ...], cancelled) -> bool:
    sizes = {provider.size for provider in providers}
    if len(sizes) != 1 or None in sizes or sum(p.size for p in providers) > MAX_COMPARE_BYTES:
        return False
    hashes = set()
    for provider in providers:
        if cancelled():
            return False
        try:
            digest = hashlib.sha256()
            read = 0
            with provider.path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    read += len(chunk)
                    if cancelled() or read > provider.size:
                        return False
                    digest.update(chunk)
            if read != provider.size:
                return False
            hashes.add(digest.digest())
        except OSError:
            return False
        if len(hashes) > 1:
            return False
    return len(hashes) == 1


def classify(providers: tuple[Provider, ...], cancelled) -> tuple[FileStatus, Provider | None]:
    if any(p.size is None and p.listed for p in providers):
        return FileStatus.MISSING, None
    admitted = tuple(p for p in providers if p.listed)
    verified = (
        len(admitted) >= 2 and all(p.listed is not None for p in providers)
        and len({p.relative for p in providers}) == 1
    )
    if verified and additive_resource(admitted[0].relative):
        return FileStatus.ADDITIVE, None
    if verified and proven_hero_path(admitted[0].relative):
        label = FileStatus.HERO_IDENTICAL if same_content(admitted, cancelled) else FileStatus.OVERWRITE
        return label, admitted[0]
    if same_content(providers, cancelled):
        return FileStatus.IDENTICAL, None
    return FileStatus.UNKNOWN, None


def analyze_conflicts(
    entries: list[ResolvedEntry],
    *,
    progress=lambda *_: None,
    cancelled=lambda: False,
) -> ConflictReport:
    groups: dict[str, list[Provider]] = defaultdict(list)
    issues = []
    unavailable = []
    seen = set()
    for index, row in enumerate(entries, 1):
        if cancelled():
            return ConflictReport(cancelled=True)
        mod = row.mod
        if mod is None:
            issues.append(f"{row.entry.name} · {row.problem}")
            continue
        if mod.key in seen:
            issues.append(f"{mod.name} · 同一安装重复出现在方案，按顶部条目分析。")
            continue
        seen.add(mod.key)
        progress(index, len(entries), mod.name)
        root = Path(mod.path)
        if not root.is_dir() or root.is_symlink() or root.is_junction():
            issues.append(f"{mod.name} · 安装目录不可用。")
            unavailable.append(mod.key)
            continue
        manifest = None
        try:
            manifest = read_manifest(root / "modfiles.txt")
        except (OSError, ValueError) as exc:
            detail = "未找到清单" if isinstance(exc, FileNotFoundError) else str(exc)
            issues.append(f"{mod.name} · {detail}，仅显示物理路径候选。")
        inventory = file_inventory(root, cancelled, issues)
        paths = inventory.keys() if manifest is None else inventory.keys() | manifest
        for relative in sorted(paths):
            listed = None if manifest is None else relative in manifest
            groups[relative.casefold()].append(
                Provider(index, mod, relative, inventory.get(relative), listed)
            )

    conflicts = []
    for key in sorted(groups):
        if cancelled():
            return ConflictReport(cancelled=True)
        providers = tuple(groups[key])
        if len({p.rank for p in providers}) < 2 and not any(
            p.listed and p.size is None for p in providers
        ):
            continue
        status, winner = classify(providers, cancelled)
        conflicts.append(Conflict(providers[0].relative, providers, status, winner))
    return ConflictReport(tuple(conflicts), tuple(dict.fromkeys(issues)), unavailable=tuple(unavailable))
