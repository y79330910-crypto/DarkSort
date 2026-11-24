from collections import defaultdict
from dataclasses import dataclass
from heapq import heapify, heappop, heappush

from darksort.classification import PRIORITY, Category, Classification
from darksort.diagnostics import PlanProblem
from darksort.locks import entry_units, lock_starts, place_units
from darksort.models import Mod, Profile, ProfileEntry, ResolvedEntry
from darksort.rule_targets import distinct_targets, dynamic_target, target_positions


def browse_key(mod: Mod, field: str, display_name: str = ""):
    name = (display_name or mod.name).casefold()
    if field == "source":
        return mod.source.label, name
    if field == "category":
        return mod.category.casefold(), name
    return name


def move_entries(
    entries: list[ProfileEntry], indices: list[int], target: int
) -> list[ProfileEntry]:
    """Move a disjoint selection as a stable block; target uses original row boundaries."""
    selected = sorted(set(indices))
    if any(i < 0 or i >= len(entries) for i in selected) or not 0 <= target <= len(entries):
        raise ValueError("无效的移动位置。")
    selected_set = set(selected)
    block = [entries[i] for i in selected]
    remainder = [entry for i, entry in enumerate(entries) if i not in selected_set]
    insertion = target - sum(i < target for i in selected)
    return remainder[:insertion] + block + remainder[insertion:]


@dataclass(frozen=True)
class SortDecision:
    before: int
    after: int
    name: str
    category: str
    reason: str


@dataclass(frozen=True)
class OrderPreview:
    entries: tuple[ProfileEntry, ...]
    notices: tuple[str, ...]
    blockers: tuple[str, ...]
    decisions: tuple[SortDecision, ...] = ()
    problems: tuple[PlanProblem, ...] = ()


def preview_order(
    profile: Profile,
    resolved: list[ResolvedEntry],
    classifications: dict[str, Classification] | None = None,
    *,
    preferences: dict[int, int] | None = None,
    installed_names: dict[str, str] | None = None,
    tags: dict[str, tuple[str, ...]] | None = None,
    colors: dict[str, str] | None = None,
) -> OrderPreview:
    positions, labels = target_positions(profile, resolved, tags=tags, colors=colors)
    names = (installed_names or {}) | {row.entry.reference: row.entry.name for row in resolved} | labels
    spans = entry_units(profile.entries, profile.locks)
    units = {unit.start: unit for unit in spans}
    owner = {index: unit.start for unit in spans for index in unit}
    locks = lock_starts(profile.entries, profile.locks)
    indegree = {owner[index]: 0 for index, row in enumerate(resolved) if row.mod is not None}
    successors = {index: {} for index in indegree}
    notices, blockers, problems = [], [], []
    manual = defaultdict(list)
    next_boundary = -1

    def connect(start_unit, end_unit, start, end, note):
        if end_unit not in successors[start_unit]:
            successors[start_unit][end_unit] = (start, end, note)
            indegree[end_unit] += 1

    for rule in profile.rules:
        if not rule.enabled:
            continue
        first, second = positions[rule.first], positions[rule.second]
        a, b = names.get(rule.first, rule.first), names.get(rule.second, rule.second)
        if rule.kind == "requires":
            if first and not second:
                notices.append(f"{a} 依赖 {b}，当前方案缺少可定位的依赖。")
                problems.append(PlanProblem(
                    "error", "少了一个需要的 Mod", f"「{a}」需要「{b}」。",
                    "先安装或找到它，再把它加入当前方案。",
                    tuple((profile.entries[i].id, "需要它") for i in first), rule.note,
                ))
            continue
        first_dynamic, second_dynamic = dynamic_target(rule.first), dynamic_target(rule.second)
        first, second, overlapping = distinct_targets(first, second, first_dynamic, second_dynamic)
        if overlapping:
            notices.append(f"规则「{a} 在 {b} 上方」已略过两边重合的成员。")
        if not first or not second:
            notices.append(f"略过规则「{a} 在 {b} 上方」，需要两个可定位的条目。")
            continue
        if (not first_dynamic and len(first) > 1) or (not second_dynamic and len(second) > 1):
            blockers.append(f"规则「{a} 在 {b} 上方」匹配多个条目，请先处理重复项。")
            problems.append(PlanProblem(
                "error", "排序规则找到了重复的 Mod", f"「{a}」或「{b}」在方案里匹配了不止一行。",
                "检查这些 Mod 的来源，留下想用的那一份。",
                tuple((profile.entries[i].id, "相关 Mod") for i in dict.fromkeys(first + second)),
                rule.note,
            ))
            continue
        last_above = {owner[index]: index for index in first}
        first_below = {owner[index]: index for index in reversed(second)}
        shared_units = sorted(last_above.keys() & first_below.keys())
        reversed_unit = next((unit for unit in shared_units if last_above[unit] > first_below[unit]), None)
        if reversed_unit is not None or len(shared_units) > 1:
            if reversed_unit is not None:
                start, end = last_above[reversed_unit], first_below[reversed_unit]
                detail = f"联锁「{locks[reversed_unit].title}」的固定顺序与要求相反。"
            else:
                start, end = last_above[shared_units[1]], first_below[shared_units[0]]
                detail = "两个联锁都同时包含上方和下方的成员。"
            blockers.append(f"规则「{a} 在 {b} 上方」与联锁冲突。{detail}")
            problems.append(PlanProblem(
                "error", "联锁和上下顺序规则撞上了", f"规则要「{a}」在「{b}」上面。{detail}",
                "检查分组成员或解除涉及的联锁，再调整顺序。",
                ((profile.entries[start].id, "应该在上"), (profile.entries[end].id, "应该在下")), rule.note,
            ))
            continue
        if len(last_above) == len(first_below) == 1:
            start_unit, end_unit = owner[first[0]], owner[second[0]]
            if start_unit != end_unit:
                connect(start_unit, end_unit, first[0], second[0], rule.note)
        else:
            # A shared lock can serve as the boundary. Otherwise one virtual node suffices
            # for every pair implied by the rule, even with large groups.
            if shared_units:
                boundary = shared_units[0]
            else:
                boundary = next_boundary
                next_boundary -= 1
                indegree[boundary], successors[boundary] = 0, {}
            for unit, start in last_above.items():
                if unit != boundary:
                    connect(unit, boundary, start, first_below.get(boundary, second[0]), rule.note)
            for unit, end in first_below.items():
                if unit != boundary:
                    connect(boundary, unit, last_above.get(boundary, first[0]), end, rule.note)
        for index in first:
            manual[index].append(f"规则要求在 {b} 上方")
        for index in second:
            manual[index].append(f"规则要求在 {a} 下方")

    categories = {
        index: classifications[row.mod.key]
        for index, row in enumerate(resolved)
        if row.mod is not None and classifications is not None and row.mod.key in classifications
    }
    anchors, skin_reasons = pair_skins(resolved, categories, owner, successors, indegree, notices)
    movable = {
        index for index in indegree if classifications is None or indegree[index]
        or successors[index] or any(
            member in categories and categories[member].category is not None for member in units[index]
        )
    }

    def member_priority(index: int):
        if index in anchors:
            return (PRIORITY[Category.HERO], anchors[index], 0)
        classification = categories.get(index)
        tier = PRIORITY.get(classification.category, len(PRIORITY)) if classification else len(PRIORITY)
        return (tier, index, 1)

    def sort_key(index: int):
        if index < 0:
            return (-1, index)
        priority = min(member_priority(member) for member in units[index])
        if preferences is None:
            return (*priority, index)
        return (min(preferences[member] for member in units[index]), *priority, index)

    ready = [sort_key(index) for index in movable if indegree[index] == 0]
    heapify(ready)
    placed = []
    while ready:
        start = heappop(ready)[-1]
        placed.append(start)
        for end in successors[start]:
            indegree[end] -= 1
            if indegree[end] == 0:
                heappush(ready, sort_key(end))
    if len(placed) != len(movable):
        remaining = {index for index, degree in indegree.items() if degree > 0}
        cycle_entries = {}
        cycle_lines = []
        for start, end in find_cycle(successors, remaining):
            first, second, detail = successors[start][end]
            note = f"（{detail}）" if detail else ""
            blockers.append(f"循环 · {profile.entries[first].name} 在 {profile.entries[second].name} 上方{note}")
            cycle_entries.update({first: None, second: None})
            cycle_lines.append(f"「{profile.entries[first].name}」要在「{profile.entries[second].name}」上面{note}")
        if cycle_entries:
            problems.append(PlanProblem(
                "error", "这些 Mod 都被要求排在前面", "几条规则绕成了圈，这些位置要求无法同时满足。",
                "打开「用户规则」，调整其中一条规则。涉及联锁时，也可以先解除联锁。",
                tuple((profile.entries[i].id, "顺序卡住") for i in sorted(cycle_entries)),
                "\n".join(cycle_lines),
            ))
        if any(index in locks for index in remaining):
            blockers.append("联锁必须整体相邻。检查是否有规则要求把另一项插在联锁内部。")

    if blockers:
        return OrderPreview(tuple(profile.entries), tuple(notices), tuple(blockers), problems=tuple(problems))
    order = place_units(spans, [units[index] for index in placed if index in units])
    shifted = sum(
        owner[before] not in movable and after != before for after, before in enumerate(order)
    )
    if shifted:
        notices.append(f"为保持联锁相邻，{shifted} 个预留条目的序号随整组移动而调整。")
    decisions = []
    for after, before in enumerate(order, 1):
        row = resolved[before]
        category = categories.get(before)
        reason = []
        if before in skin_reasons:
            reason.append(skin_reasons[before])
        if category:
            reason.append(category.reason)
        if lock := locks.get(owner[before]):
            reason.append(f"联锁 · {lock.title}")
        reason.extend(dict.fromkeys(manual.get(before, ())))
        if row.mod is None:
            reason.append(row.problem or "条目未定位")
        elif classifications is not None and owner[before] not in movable and before + 1 == after:
            reason.append("保留原位置")
        decisions.append(SortDecision(
            before + 1, after, row.entry.name,
            category.label if category else ("待定位" if row.mod is None else "—"),
            " · ".join(reason) or "沿用当前相对顺序",
        ))
    ordered = tuple(profile.entries[index] for index in order)
    return OrderPreview(ordered, tuple(notices), (), tuple(decisions), tuple(problems))


def pair_skins(resolved, categories, owner, successors, indegree, notices):
    heroes = defaultdict(list)
    for index, classification in categories.items():
        if classification.category is Category.HERO:
            for hero in resolved[index].mod.hero_definitions:
                heroes[hero].append(index)
    anchors, reasons = {}, {}
    for index, classification in categories.items():
        if classification.category is not Category.SKIN:
            continue
        row = resolved[index]
        targets = set()
        for hero in row.mod.skin_targets:
            matches = heroes.get(hero, [])
            if len(matches) == 1:
                targets.add(matches[0])
            elif len(matches) > 1:
                notices.append(f"{row.entry.name} 的人物标识 {hero} 对应多个 Mod，请用规则指定。")
        linked = []
        skin_unit = owner[index]
        for target in sorted(targets):
            title = resolved[target].entry.name
            hero_unit = owner[target]
            if hero_unit == skin_unit:
                if index < target:
                    linked.append(target)
                else:
                    notices.append(f"{row.entry.name} 与 {title} 的皮肤顺序由联锁固定。")
                continue
            if reachable(successors, hero_unit, skin_unit):
                notices.append(f"{row.entry.name} 与 {title} 的皮肤顺序受到自定义规则约束。")
                continue
            if hero_unit not in successors[skin_unit]:
                successors[skin_unit][hero_unit] = (index, target, "皮肤资源匹配人物")
                indegree[hero_unit] += 1
            linked.append(target)
        if linked:
            external = [target for target in linked if owner[target] != skin_unit]
            if external:
                anchors[index] = min(external)
            names = "、".join(resolved[target].entry.name for target in linked)
            reasons[index] = f"皮肤放在对应人物 {names} 上方"
    return anchors, reasons


def reachable(successors, start: int, end: int) -> bool:
    pending = [start]
    visited = {start}
    while pending:
        index = pending.pop()
        if index == end:
            return True
        for neighbor in successors[index]:
            if neighbor not in visited:
                pending.append(neighbor)
                visited.add(neighbor)
    return False


def find_cycle(successors: dict[int, dict[int, tuple[int, int, str]]], remaining: set[int]):
    """Find one cycle deterministically without recursion on large imported lists."""
    visited = set()
    for start in sorted(remaining):
        if start in visited:
            continue
        path = [start]
        active = {start: 0}
        visited.add(start)
        stack = [iter(sorted(successors[start]))]
        while stack:
            end = next(stack[-1], None)
            if end is None:
                active.pop(path.pop())
                stack.pop()
            elif end in active:
                cycle = path[active[end] :] + [end]
                return list(zip(cycle, cycle[1:]))
            elif end in remaining and end not in visited:
                visited.add(end)
                active[end] = len(path)
                path.append(end)
                stack.append(iter(sorted(successors[end])))
    return []
