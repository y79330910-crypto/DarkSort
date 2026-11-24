from __future__ import annotations

import os
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path


class Source(StrEnum):
    WORKSHOP = "workshop"
    LOCAL = "local"

    @property
    def label(self) -> str:
        return "创意工坊" if self == Source.WORKSHOP else "本地"


def path_key(path: str | Path) -> str:
    # Profile references may point to offline drives. Matching must not touch the filesystem.
    # Actual scan paths have already been resolved in the background scanner.
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def mod_reference(source: Source, path: str, workshop_id: str | None, name: str) -> str:
    if source == Source.WORKSHOP and workshop_id:
        return f"workshop:{workshop_id}"
    if path:
        return f"{source.value}:{path_key(path)}"
    return f"unresolved:{source.value}:{name.casefold()}"


@dataclass(frozen=True)
class ScanRoot:
    path: Path
    source: Source


@dataclass(frozen=True)
class Mod:
    path: str
    source: Source
    name: str
    description: str = ""
    tags: tuple[str, ...] = ()
    version: str = "未知"
    workshop_id: str | None = None
    preview_path: str | None = None
    warnings: tuple[str, ...] = ()
    target_build: str = ""
    project_title: str = ""
    resource_kinds: tuple[str, ...] = ()
    hero_definitions: tuple[str, ...] = ()
    skin_targets: tuple[str, ...] = ()
    skin_files: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.source.value}:{path_key(self.path)}"

    @property
    def reference(self) -> str:
        return mod_reference(self.source, self.path, self.workshop_id, self.name)

    @property
    def category(self) -> str:
        return " / ".join(self.tags) or "未分类"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Mod:
        data = dict(data)
        data["source"] = Source(data["source"])
        for field_name in ("tags", "warnings", "resource_kinds", "hero_definitions", "skin_targets", "skin_files"):
            data[field_name] = tuple(data.get(field_name, ()))
        return cls(**data)


@dataclass(frozen=True)
class ScanIssue:
    path: str
    message: str


@dataclass
class ScanResult:
    mods: list[Mod] = field(default_factory=list)
    issues: list[ScanIssue] = field(default_factory=list)
    cache_hits: int = 0
    cancelled: bool = False


@dataclass(frozen=True)
class ProfileEntry:
    name: str
    source: Source
    path: str
    workshop_id: str | None = None
    pinned_path: bool = False
    id: str = field(default_factory=lambda: str(uuid.uuid4()), compare=False)
    portable_id: str | None = None

    @classmethod
    def from_mod(cls, mod: Mod, *, pinned: bool = False) -> ProfileEntry:
        return cls(mod.name, mod.source, mod.path, mod.workshop_id, pinned)

    def to_dict(self) -> dict:
        data = asdict(self)
        if self.portable_id is None:
            del data["portable_id"]
        return data

    @property
    def reference(self) -> str:
        if self.portable_id:
            return f"portable:{self.portable_id}"
        return mod_reference(self.source, self.path, self.workshop_id, self.name)


@dataclass(frozen=True)
class ProfileGroup:
    anchor: str
    title: str


@dataclass(frozen=True)
class ModGroup:
    title: str
    color: str
    members: tuple[ProfileEntry, ...] = ()
    id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass(frozen=True)
class ProfileLock:
    title: str
    members: tuple[str, ...]


@dataclass(frozen=True)
class OrderRule:
    first: str
    second: str
    kind: str = "above"
    note: str = ""
    enabled: bool = True


@dataclass
class Profile:
    id: str
    name: str
    entries: list[ProfileEntry] = field(default_factory=list)
    groups: list[ProfileGroup] = field(default_factory=list)
    rules: list[OrderRule] = field(default_factory=list)
    staged: list[ProfileEntry] = field(default_factory=list)
    locks: list[ProfileLock] = field(default_factory=list)
    mod_groups: list[ModGroup] = field(default_factory=list)


@dataclass(frozen=True)
class ResolvedEntry:
    entry: ProfileEntry
    mod: Mod | None
    problem: str = ""
