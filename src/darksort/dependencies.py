from collections import defaultdict, deque
from dataclasses import replace
from graphlib import CycleError, TopologicalSorter

from darksort.models import ProfileEntry
from darksort.profiles import resolve_entries


def required_edges(rules):
    edges = defaultdict(dict)
    for rule in rules:
        if rule.enabled and rule.kind == "requires":
            edges[rule.first][rule.second] = None
    return edges


def required_mods(rules, roots, names):
    edges = required_edges(rules)
    queue = deque(roots)
    graph = {}
    while queue:
        reference = queue.popleft()
        if reference not in graph:
            graph[reference] = edges.get(reference, ())
            queue.extend(graph[reference])
    try:
        TopologicalSorter(graph).prepare()
    except CycleError as exc:
        cycle = " → ".join(names.get(reference, reference) for reference in exc.args[1])
        raise ValueError(f"前置规则存在循环：{cycle}") from exc
    return dict.fromkeys(target for targets in graph.values() for target in targets)


def enable_requirements(profile, roots, installed, unavailable_keys):
    names = {mod.reference: mod.name for mod in installed}
    names.update((entry.reference, entry.name) for entry in profile.entries)
    required = required_mods(profile.rules, roots, names)
    if not required:
        return profile, []
    available = defaultdict(list)
    for mod in installed:
        if mod.key not in unavailable_keys:
            available[mod.reference].append(mod)
    current = defaultdict(list)
    for row in resolve_entries(profile.entries, installed):
        current[row.entry.reference].append(row.mod)
    additions = []
    for reference in required:
        if reference in current:
            if any(mod and mod.key not in unavailable_keys for mod in current[reference]):
                continue
            raise ValueError(f"请先匹配计划中的前置 Mod「{names.get(reference, reference)}」。")
        candidates = available[reference]
        name = names.get(reference, reference)
        if len(candidates) != 1:
            detail = "发现多份安装，请先选择对应安装" if candidates else "请先安装前置或检查扫描路径"
            raise ValueError(f"无法启用前置「{name}」。{detail}。")
        additions.append(ProfileEntry.from_mod(candidates[0], pinned=True))
    return replace(profile, entries=profile.entries + additions), additions


def check_removal(profile, removed_ids):
    removed = {entry.reference: entry.name for entry in profile.entries if entry.id in removed_ids}
    kept = {entry.reference: entry.name for entry in profile.entries if entry.id not in removed_ids}
    blocked = [
        f"「{kept[first]}」仍需要「{removed[second]}」"
        for first, targets in required_edges(profile.rules).items() if first in kept
        for second in targets if second in removed and second not in kept
    ]
    if blocked:
        raise ValueError("，".join(blocked[:3]) + "。请先停用依赖它的 Mod。")


def validate_requirements(rules, resolved):
    names = {row.entry.reference: row.mod.name if row.mod else row.entry.name for row in resolved}
    active = {row.entry.reference for row in resolved if row.mod}
    required = required_mods(rules, names, names)
    missing = [names.get(reference, reference) for reference in required if reference not in active]
    if missing:
        raise ValueError("计划缺少前置 Mod，请匹配并加入计划：" + "、".join(missing[:8]))
