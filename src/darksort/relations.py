from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from darksort.classification import Category, Classification
from darksort.models import Mod, Profile, ProfileEntry, ResolvedEntry
from darksort.search import ModLocation


@dataclass(frozen=True)
class RelationDestination:
    target: ModLocation
    name: str
    caption: str
    path: str


@dataclass(frozen=True)
class RelatedMod:
    name: str
    kind: str
    evidence: str
    location: str
    target: ModLocation | None = None
    edge_kind: str = ""
    choices: tuple[RelationDestination, ...] = ()


def describe_relation(name, kind, evidence, edge_kind, destinations):
    if len(destinations) == 1:
        destination = destinations[0]
        return RelatedMod(destination.name, kind, evidence, destination.caption, destination.target, edge_kind)
    if destinations:
        return RelatedMod(name, kind, evidence, "匹配多项", choices=tuple(destinations))
    return RelatedMod(name, kind, evidence, "未定位")


def related_mods(
    mod: Mod | None,
    entry: ProfileEntry | None,
    profile: Profile,
    installed: list[Mod],
    resolved: list[ResolvedEntry],
    group_entries: dict[str, tuple[ResolvedEntry, str]],
    classifications: dict[str, Classification],
) -> list[RelatedMod]:
    if mod is None and entry is None:
        return []
    reference = entry.reference if entry else mod.reference
    by_reference = defaultdict(list)
    by_install = defaultdict(list)
    group_targets = defaultdict(list)
    for index, row in enumerate(resolved):
        caption = f"计划 #{index + 1}" + (" · 待匹配" if row.mod is None else "")
        destination = RelationDestination(
            ModLocation("profile", row.entry.id), row.mod.name if row.mod else row.entry.name,
            caption, row.mod.path if row.mod else row.entry.path,
        )
        by_reference[row.entry.reference].append(destination)
        if row.mod:
            by_install[row.mod.key].append(destination)
    for identity, (row, title) in group_entries.items():
        group_targets[row.entry.reference].append(RelationDestination(
            ModLocation("group", identity), row.entry.name, f"分组 · {title}", row.entry.path,
        ))

    def installed_destinations(candidate):
        return by_install.get(candidate.key) or [
            RelationDestination(ModLocation("library", candidate.key), candidate.name, "可用库", candidate.path)
        ]

    rules = []
    for rule in profile.rules:
        if not rule.enabled or reference not in (rule.first, rule.second):
            continue
        first = reference == rule.first
        target = rule.second if first else rule.first
        if rule.kind == "requires":
            kind = "本 Mod 需要" if first else "需要本 Mod"
        else:
            kind = "本 Mod 应在其上" if first else "本 Mod 应在其下"
        destinations = by_reference.get(target)
        if not destinations:
            destinations = [
                destination for candidate in installed if candidate.reference == target
                for destination in installed_destinations(candidate)
            ] or group_targets.get(target, ())
        rules.append(describe_relation(target, kind, f"用户规则 · {rule.note or kind}", rule.kind, destinations))

    automatic = []
    if mod:
        for identifier, kind, candidates in skin_partners(mod, installed, by_install, classifications):
            choices = [destination for candidate in candidates for destination in installed_destinations(candidate)]
            automatic.append(describe_relation(
                identifier, kind, f"人物资源标识一致 · {identifier}", "above", choices,
            ))
    return list(dict.fromkeys((*rules, *automatic)))


def skin_partners(mod, installed, active, classifications):
    category = classifications[mod.key].category
    if category not in (Category.HERO, Category.SKIN):
        return
    heroes = defaultdict(list)
    for candidate in installed:
        if classifications[candidate.key].category == Category.HERO:
            for hero in candidate.hero_definitions:
                heroes[hero].append(candidate)

    def eligible(hero):
        candidates = heroes.get(hero, [])
        return [candidate for candidate in candidates if candidate.key in active] or candidates

    if category == Category.SKIN:
        seen = set()
        for hero in mod.skin_targets:
            candidates = eligible(hero)
            keys = tuple(candidate.key for candidate in candidates)
            if keys and keys not in seen:
                seen.add(keys)
                yield hero, "对应人物 · 本皮肤建议在上", candidates
    else:
        unique = {hero for hero in mod.hero_definitions if eligible(hero) == [mod]}
        for candidate in installed:
            if classifications[candidate.key].category == Category.SKIN:
                target = next((hero for hero in candidate.skin_targets if hero in unique), None)
                if target:
                    yield target, "配套皮肤 · 皮肤建议在上", [candidate]
