from __future__ import annotations

import io
import json
import re
import uuid
from dataclasses import replace
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile
from zlib import error as CompressionError

from darksort.models import ModGroup, Profile, ProfileEntry, Source
from darksort.profiles import (
    MAX_PROFILE_BYTES,
    mod_group_dict,
    parse_mod_groups,
    parse_profile,
    profile_dict,
)
from darksort.rule_targets import dynamic_target
from darksort.search import search_text
from darksort.storage import atomic_bytes, atomic_json, read_json
from darksort.workshop import workshop_id, workshop_page

PROFILE_FILE = "darksort-profile.json"
LIST_FILE = "Mod 清单.txt"
TEMPLATE_FORMAT = "darksort.group-template"
MAX_SHARE_BYTES = 24 * 1024 * 1024


def export_filename(title: str, suffix: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")
    return f"DarkSort - {safe or '方案'}{suffix}"


def portable_entry(entry: ProfileEntry, local_ids: dict[str, str]) -> ProfileEntry:
    identifier = entry.workshop_id if workshop_id(entry.workshop_id) else None
    if entry.source == Source.WORKSHOP and identifier:
        return replace(entry, path="", pinned_path=False)
    reference = entry.reference
    if reference not in local_ids:
        local_ids[reference] = entry.portable_id or str(uuid.uuid4())
    return replace(entry, path="", workshop_id=identifier, pinned_path=False,
                   portable_id=local_ids[reference])


def portable_group(group: ModGroup, local_ids: dict[str, str]) -> ModGroup:
    return replace(group, members=tuple(portable_entry(entry, local_ids) for entry in group.members))


def portable_rule_target(reference: str, local_ids: dict[str, str]) -> str:
    kind, _, identifier = reference.partition(":")
    if reference in local_ids:
        return f"portable:{local_ids[reference]}"
    if dynamic_target(reference) or (kind == "workshop" and workshop_id(identifier)):
        return reference
    if kind == "portable":
        try:
            if str(uuid.UUID(identifier)) == identifier:
                return reference
        except ValueError:
            pass
    local_ids[reference] = str(uuid.uuid4())
    return f"portable:{local_ids[reference]}"


def portable_profile(profile: Profile) -> Profile:
    # Keep one identity for the same local Mod across plan, staging, groups and rules.
    local_ids: dict[str, str] = {}
    entries = [portable_entry(entry, local_ids) for entry in profile.entries]
    staged = [portable_entry(entry, local_ids) for entry in profile.staged]
    groups = [portable_group(group, local_ids) for group in profile.mod_groups]
    rules = [replace(rule, first=portable_rule_target(rule.first, local_ids),
                     second=portable_rule_target(rule.second, local_ids)) for rule in profile.rules]
    return replace(profile, entries=entries, staged=staged, mod_groups=groups, rules=rules)


def mod_checklist(profile: Profile) -> str:
    sections = [("计划顺序（顶部优先）", profile.entries)]
    if profile.staged:
        sections.append(("暂存篮", profile.staged))
    sections.extend((f"分组 · {group.title}", group.members) for group in profile.mod_groups)
    lines = [f"DarkSort · {profile.name}", "《暗黑地牢 1》Mod 清单", "",
             "可在 DarkSort 的「文件 → 导入分享包」中载入此包。",
             "创意工坊项目可按链接订阅。本地 Mod 在扫描后可以手动匹配。"]
    for title, entries in sections:
        lines.extend(("", f"{title} · {len(entries)} 项"))
        for index, entry in enumerate(entries, 1):
            name = " ".join(entry.name.split()) or "未命名 Mod"
            url = workshop_page(entry.workshop_id) if entry.source == Source.WORKSHOP else ""
            lines.append(f"{index:02d}. {name} · {url or '手动匹配本机 Mod'}")
    return "\n".join(lines) + "\n"


def write_share_package(profile: Profile, path: Path) -> None:
    profile = portable_profile(profile)
    payload = profile_dict(profile)
    parse_profile(payload)
    data = (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    checklist = mod_checklist(profile).encode("utf-8-sig")
    if len(data) > MAX_PROFILE_BYTES or len(checklist) > MAX_PROFILE_BYTES:
        raise ValueError("方案清单过大，无法生成分享包。")
    output = io.BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(PROFILE_FILE, data)
        archive.writestr(LIST_FILE, checklist)
    atomic_bytes(path, output.getvalue())


def read_share_package(path: Path) -> Profile:
    if path.stat().st_size > MAX_SHARE_BYTES:
        raise ValueError("分享包文件过大。")
    try:
        with ZipFile(path) as archive:
            manifests = [item for item in archive.infolist() if item.filename == PROFILE_FILE]
            if len(manifests) != 1 or manifests[0].file_size > MAX_PROFILE_BYTES:
                raise ValueError("分享包中的方案清单缺失、重复或过大。")
            if manifests[0].compress_type not in (ZIP_STORED, ZIP_DEFLATED):
                raise ValueError("方案分享包的压缩格式无效。")
            with archive.open(manifests[0]) as manifest:
                raw = manifest.read(MAX_PROFILE_BYTES + 1)
            if len(raw) > MAX_PROFILE_BYTES:
                raise ValueError("分享包中的方案清单过大。")
            data = json.loads(raw.decode("utf-8-sig"))
    except (BadZipFile, RuntimeError, CompressionError) as exc:
        raise ValueError("无法读取 DarkSort 分享包。") from exc
    if not isinstance(data, dict):
        raise ValueError("分享包中的方案清单无效。")
    return portable_profile(parse_profile(data))


def write_group_template(group: ModGroup, path: Path) -> None:
    data = mod_group_dict(portable_group(group, {}))
    parse_mod_groups({"mod_groups": [data]}, allow_portable=True)
    atomic_json(path, {"format": TEMPLATE_FORMAT, "version": 1, "group": data},
                max_bytes=MAX_PROFILE_BYTES)


def read_group_template(path: Path) -> ModGroup:
    data = read_json(path, max_bytes=MAX_PROFILE_BYTES)
    if data.get("format") != TEMPLATE_FORMAT or type(data.get("version")) is not int or data["version"] != 1:
        raise ValueError("请选择 DarkSort 导出的分组模板。")
    group, = parse_mod_groups({"mod_groups": [data.get("group")]}, allow_portable=True)
    return portable_group(group, {})


def group_from_template(profile: Profile, template: ModGroup) -> ModGroup:
    title = unique_title(template.title, (group.title for group in profile.mod_groups))
    return ModGroup(title, template.color, template.members)


def unique_title(original: str, existing) -> str:
    taken = {search_text(title) for title in existing}
    title = original
    number = 2
    while search_text(title) in taken:
        suffix = f" ({number})"
        title = original[:120 - len(suffix)].rstrip() + suffix
        number += 1
    return title
