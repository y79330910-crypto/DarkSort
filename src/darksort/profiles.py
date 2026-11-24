from __future__ import annotations

import uuid
from dataclasses import asdict, replace
from itertools import chain
from pathlib import Path

from darksort.mod_groups import MAX_GROUP_MEMBERS, MAX_MOD_GROUPS
from darksort.models import (
    Mod,
    ModGroup,
    OrderRule,
    Profile,
    ProfileEntry,
    ProfileGroup,
    ProfileLock,
    ResolvedEntry,
    Source,
    path_key,
)
from darksort.rule_targets import dynamic_target, valid_color, validate_target
from darksort.search import search_text
from darksort.storage import atomic_json, read_json

FORMAT = "darksort.profile"
GROUPS_VERSION = 4
VERSION = 5
MAX_ENTRIES = 20000
MAX_PROFILE_BYTES = 8 * 1024 * 1024


def profile_dict(profile: Profile) -> dict:
    version = 3 if profile.staged or profile.locks else 2
    if profile.mod_groups or any(dynamic_target(key) for rule in profile.rules for key in (rule.first, rule.second)):
        version = GROUPS_VERSION
    member_lists = (group.members for group in profile.mod_groups)
    if any(entry.portable_id for entry in chain(profile.entries, profile.staged, *member_lists)):
        version = VERSION
    payload = {
        "format": FORMAT,
        "version": version,
        "id": profile.id,
        "name": profile.name,
        "entries": [entry.to_dict() for entry in profile.entries],
        "groups": [asdict(group) for group in profile.groups],
        "rules": [asdict(rule) for rule in profile.rules],
    }
    if profile.staged or profile.locks:
        payload["staged"] = [entry.to_dict() for entry in profile.staged]
        payload["locks"] = [
            {"title": lock.title, "members": list(lock.members)} for lock in profile.locks
        ]
    if profile.mod_groups:
        payload["mod_groups"] = [mod_group_dict(group) for group in profile.mod_groups]
    return payload


def mod_group_dict(group: ModGroup) -> dict:
    return {"id": group.id, "title": group.title, "color": group.color,
            "members": [member.to_dict() for member in group.members]}


def parse_profile(data: dict) -> Profile:
    version = data.get("version")
    if data.get("format") != FORMAT or type(version) is not int or not 1 <= version <= VERSION:
        raise ValueError("不是受支持的 DarkSort 配置文件。")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise ValueError("配置名称必须为 1–120 个字符。")
    raw_id = data.get("id")
    if not isinstance(raw_id, str):
        raise ValueError("配置 ID 必须是 UUID 字符串。")
    profile_id = str(uuid.UUID(raw_id))
    entry_ids = set()
    entries = parse_entries(data.get("entries"), entry_ids, profile_id if version == 1 else None,
                            allow_portable=version == VERSION)
    if version == 1:
        return Profile(profile_id, name.strip(), entries)

    groups = []
    anchors = set()
    for item in profile_list(data, "groups", len(entries)):
        anchor, title = item.get("anchor"), item.get("title")
        if not isinstance(anchor, str) or anchor not in entry_ids or anchor in anchors:
            raise ValueError("分组必须绑定到不同的现有条目。")
        if not isinstance(title, str) or not title.strip() or len(title) > 120:
            raise ValueError("分组名称必须为 1–120 个字符。")
        anchors.add(anchor)
        groups.append(ProfileGroup(anchor, title.strip()))

    rules = []
    for item in profile_list(data, "rules", 20000):
        first, second = item.get("first"), item.get("second")
        if not all(isinstance(key, str) and 0 < len(key) <= 32780 for key in (first, second)):
            raise ValueError("排序规则的 Mod 标识无效。")
        kind, note, enabled = item.get("kind", "above"), item.get("note", ""), item.get("enabled", True)
        if first == second or kind not in ("above", "requires"):
            raise ValueError("排序规则必须关联两个不同的 Mod 并使用已支持的关系。")
        if dynamic_target(first) or dynamic_target(second):
            if version < GROUPS_VERSION or kind != "above":
                raise ValueError("分组、标签和颜色只能用于「在上方」规则。")
            validate_target(first)
            validate_target(second)
        if not isinstance(note, str) or len(note) > 500 or not isinstance(enabled, bool):
            raise ValueError("排序规则的说明或启用状态无效。")
        rules.append(OrderRule(first, second, kind, note, enabled))
    staged = parse_entries(data.get("staged", []), entry_ids, allow_portable=version == VERSION) if version >= 3 else []
    locks = parse_locks(data, entries, staged) if version >= 3 else []
    mod_groups = parse_mod_groups(data, allow_portable=version == VERSION) if version >= GROUPS_VERSION else []
    return Profile(profile_id, name.strip(), entries, groups, rules, staged, locks, mod_groups)


def parse_mod_groups(data, *, allow_portable=False):
    groups, used_ids, used_titles = [], set(), set()
    total = 0
    for item in profile_list(data, "mod_groups", MAX_MOD_GROUPS):
        identity, title, color = item.get("id"), item.get("title"), item.get("color")
        if not isinstance(identity, str) or str(uuid.UUID(identity)) != identity or identity in used_ids:
            raise ValueError("分组的标识无效或重复。")
        if not isinstance(title, str) or not 0 < len(title.strip()) <= 120 or search_text(title) in used_titles:
            raise ValueError("分组需要 1–120 个字符的不同名称。")
        if not valid_color(color):
            raise ValueError("分组颜色无效。")
        members = parse_entries(item.get("members"), set(), allow_portable=allow_portable)
        total += len(members)
        if total > MAX_GROUP_MEMBERS or len({entry.reference for entry in members}) != len(members):
            raise ValueError("分组成员重复或总数超出上限。")
        used_ids.add(identity)
        used_titles.add(search_text(title))
        groups.append(ModGroup(title.strip(), color, tuple(members), identity))
    return groups


def parse_entries(items, seen, legacy_profile=None, *, allow_portable=False):
    if not isinstance(items, list) or len(items) + len(seen) > MAX_ENTRIES:
        raise ValueError(f"计划和暂存合计最多 {MAX_ENTRIES} 项。")
    entries = []
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {i} 项不是有效条目。")
        source = Source(item.get("source"))
        if not isinstance(item.get("name"), str) or not isinstance(item.get("path"), str):
            raise ValueError(f"第 {i} 项缺少名称或路径。")
        if "\x00" in item["path"] or len(item["path"]) > 32767:
            raise ValueError(f"第 {i} 项路径无效。")
        workshop_id = item.get("workshop_id")
        if workshop_id is not None and (
            not isinstance(workshop_id, str) or not workshop_id.isdecimal()
        ):
            raise ValueError(f"第 {i} 项 Workshop ID 无效。")
        pinned = item.get("pinned_path", False)
        if not isinstance(pinned, bool):
            raise ValueError(f"第 {i} 项定位标志无效。")
        portable_id = item.get("portable_id")
        if portable_id is not None and (
            not allow_portable or item["path"] or pinned or (source == Source.WORKSHOP and workshop_id)
            or not isinstance(portable_id, str) or str(uuid.UUID(portable_id)) != portable_id
        ):
            raise ValueError(f"第 {i} 项便携标识无效。")
        entry_id = item.get("id") if legacy_profile is None else str(
            uuid.uuid5(uuid.UUID(legacy_profile), str(i))
        )
        if not isinstance(entry_id, str):
            raise ValueError(f"第 {i} 项标识无效。")
        entry_id = str(uuid.UUID(entry_id))
        if entry_id in seen:
            raise ValueError(f"第 {i} 项标识重复。")
        seen.add(entry_id)
        entries.append(ProfileEntry(item["name"], source, item["path"], workshop_id, pinned, entry_id, portable_id))
    return entries


def parse_locks(data, entries, staged):
    lists = (entries, staged)
    positions = {
        entry.id: (side, index) for side, items in enumerate(lists) for index, entry in enumerate(items)
    }
    used = set()
    locks = []
    for item in profile_list(data, "locks", len(positions) // 2):
        title, members = item.get("title"), item.get("members")
        if not isinstance(title, str) or not title.strip() or len(title) > 120:
            raise ValueError("联锁名称必须为 1–120 个字符。")
        if (
            not isinstance(members, list) or not 2 <= len(members) <= len(positions)
            or not all(isinstance(member, str) and member in positions for member in members)
        ):
            raise ValueError("联锁必须包含至少两个已有条目。")
        side, start = positions[members[0]]
        expected = [entry.id for entry in lists[side][start:start + len(members)]]
        if members != expected or used.intersection(members):
            raise ValueError("联锁条目必须在同一列表内连续排列，每项只能属于一个联锁。")
        used.update(members)
        locks.append(ProfileLock(title.strip(), tuple(members)))
    return locks


def profile_list(data: dict, key: str, limit: int) -> list[dict]:
    value = data.get(key, [])
    if not isinstance(value, list) or len(value) > limit or any(
        not isinstance(item, dict) for item in value
    ):
        raise ValueError(f"配置字段 {key} 无效。")
    return value


class ProfileStore:
    def __init__(self, data_dir: Path):
        self.directory = data_dir / "profiles"
        self.directory.mkdir(parents=True, exist_ok=True)

    def path(self, profile_id: str) -> Path:
        return self.directory / f"{uuid.UUID(profile_id)}.json"

    def list(self) -> tuple[list[Profile], list[str]]:
        profiles, errors = [], []
        for path in self.directory.glob("*.json"):
            try:
                profile = parse_profile(read_json(path))
                if path.stem != profile.id:
                    raise ValueError("文件名与配置 ID 不一致")
                profiles.append(profile)
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                errors.append(f"{path.name}：{exc}")
        profiles.sort(key=lambda profile: profile.name.casefold())
        return profiles, errors

    def new(self, name: str, entries: list[ProfileEntry] | None = None) -> Profile:
        profile = Profile(str(uuid.uuid4()), name.strip(), list(entries or ()))
        self.save(profile)
        return profile

    def save(self, profile: Profile) -> None:
        # Validate before touching a previously saved profile.
        payload = profile_dict(profile)
        parse_profile(payload)
        atomic_json(self.path(profile.id), payload, max_bytes=MAX_PROFILE_BYTES)

    def load(self, profile_id: str) -> Profile:
        path = self.path(profile_id)
        profile = parse_profile(read_json(path))
        if profile.id != path.stem:
            raise ValueError("文件名与配置 ID 不一致。")
        return profile

    def import_file(self, path: Path) -> Profile:
        return self.import_profile(parse_profile(read_json(path)))

    def import_profile(self, profile: Profile) -> Profile:
        # Import always creates a new local profile; never overwrites an existing one.
        profile = replace(profile, id=str(uuid.uuid4()))
        self.save(profile)
        return profile

    def export_file(self, profile: Profile, path: Path) -> None:
        parse_profile(profile_dict(profile))
        atomic_json(path, profile_dict(profile))

    def duplicate(self, profile: Profile, name: str) -> Profile:
        duplicate = replace(profile, id=str(uuid.uuid4()), name=name.strip())
        self.save(duplicate)
        return self.load(duplicate.id)


def resolve_entries(entries: list[ProfileEntry], mods: list[Mod]) -> list[ResolvedEntry]:
    by_path = {(mod.source, path_key(mod.path)): mod for mod in mods}
    by_workshop: dict[str, list[Mod]] = {}
    for mod in mods:
        if mod.source == Source.WORKSHOP and mod.workshop_id:
            by_workshop.setdefault(mod.workshop_id, []).append(mod)
    result = []
    for entry in entries:
        if entry.source == Source.WORKSHOP and entry.workshop_id:
            candidates = by_workshop.get(entry.workshop_id, [])
            if len(candidates) == 1:
                result.append(ResolvedEntry(entry, candidates[0]))
            elif len(candidates) > 1:
                pinned = [mod for mod in candidates if path_key(mod.path) == path_key(entry.path)]
                if entry.pinned_path and len(pinned) == 1:
                    result.append(ResolvedEntry(entry, pinned[0]))
                else:
                    result.append(ResolvedEntry(entry, None, "多个同 ID 目录，请手动定位"))
            else:
                result.append(ResolvedEntry(entry, None, "未扫描到此 Workshop Mod"))
        else:
            mod = by_path.get((entry.source, path_key(entry.path))) if entry.path else None
            problem = "请手动匹配本机 Mod" if entry.portable_id else "原路径不可用，请手动定位"
            result.append(ResolvedEntry(entry, mod, "" if mod else problem))
    return result
