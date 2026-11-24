import re
import uuid
from collections import defaultdict

from darksort.search import search_text
from darksort.settings import MARK_COLORS

TARGET_TYPES = ("group", "tag", "color")
COLOR_NAMES = {color: name for name, color in MARK_COLORS}


def dynamic_target(value: str) -> bool:
    return value.partition(":")[0] in TARGET_TYPES and ":" in value


def valid_color(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"#[0-9a-f]{6}", value) is not None


def validate_target(value: str) -> None:
    kind, _, identifier = value.partition(":")
    if kind == "group" and str(uuid.UUID(identifier)) != identifier:
        raise ValueError("规则的分组标识无效。")
    if kind == "tag" and (not 0 < len(identifier) <= 128 or search_text(identifier) != identifier):
        raise ValueError("规则的标签无效。")
    if kind == "color" and not valid_color(identifier):
        raise ValueError("规则的标记颜色无效。")


def distinct_targets(first, second, first_dynamic, second_dynamic):
    shared = set(first).intersection(second) if first_dynamic or second_dynamic else set()
    if shared:
        if first_dynamic:
            first = [index for index in first if index not in shared]
        if second_dynamic:
            second = [index for index in second if index not in shared]
    return first, second, bool(shared)


def target_labels(profile, tags=(), colors=()):
    labels = {f"group:{group.id}": f"分组 · {group.title}" for group in profile.mod_groups}
    for tag in tags:
        normalized = search_text(tag)
        if 0 < len(normalized) <= 128:
            labels.setdefault(f"tag:{normalized}", f"标签 · {tag}")
    for color in COLOR_NAMES.keys() | set(colors):
        if valid_color(color):
            labels[f"color:{color}"] = f"标记颜色 · {COLOR_NAMES.get(color, color)}"
    return labels


def target_positions(profile, resolved, *, tags=None, colors=None):
    needed = {target for rule in profile.rules if rule.enabled for target in (rule.first, rule.second)}
    groups = defaultdict(list)
    for group in profile.mod_groups:
        key = f"group:{group.id}"
        if key in needed:
            for member in group.members:
                groups[member.reference].append(key)
    positions = defaultdict(list)
    tags, colors = tags or {}, colors or {}
    names = target_labels(profile, (tag for values in tags.values() for tag in values), colors.values())
    for index, row in enumerate(resolved):
        mod = row.mod
        if not mod:
            continue
        reference = row.entry.reference
        positions[reference].append(index)
        for group in groups.get(reference, ()):
            positions[group].append(index)
        if color := colors.get(mod.reference):
            positions[f"color:{color}"].append(index)
        for tag in dict.fromkeys((*mod.tags, *tags.get(mod.reference, ()))):
            key = f"tag:{search_text(tag)}"
            names.setdefault(key, f"标签 · {tag}")
            if key in needed and (not positions[key] or positions[key][-1] != index):
                positions[key].append(index)
    for key in needed:
        if key.startswith("group:"):
            names.setdefault(key, "已移除的分组")
        elif key.startswith("tag:"):
            names.setdefault(key, f"标签 · {key.removeprefix('tag:')}")
    return positions, names
