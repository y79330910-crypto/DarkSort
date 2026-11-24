import re
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path

from darksort.classification import Category, Classification
from darksort.mod_files import inspect_mod_files
from darksort.models import ResolvedEntry
from darksort.scanning.content import skin_resource
from darksort.search import search_text

# Class IDs verified against the installed DD1 hero definitions. DLC classes are listed separately.
BASE_HEROES = {
    "abomination": "咒缚者", "antiquarian": "古董商人", "arbalest": "弩手",
    "bounty_hunter": "赏金猎人", "crusader": "十字军", "grave_robber": "盗墓贼",
    "hellion": "蛮族战士", "highwayman": "强盗", "houndmaster": "驯犬师", "jester": "小丑",
    "leper": "麻风剑客", "man_at_arms": "老兵", "occultist": "神秘学者",
    "plague_doctor": "瘟疫医生", "vestal": "修女",
}
DLC_HEROES = {"flagellant": "苦修者", "musketeer": "火枪手", "shieldbreaker": "破盾者"}
HERO_NAMES = BASE_HEROES | DLC_HEROES
HERO_ALIASES = {"麻风病人": "leper", "蛮女": "hellion", "古董商": "antiquarian", "憎恶": "abomination"}
TOPICS = {
    "skins": ("皮肤", "皮膚", "换装", "外观", "skin", "outfit"),
    "expansions": ("扩展", "扩充", "扩展包", "拓展", "玩法", "expansion", "overhaul"),
    "monsters": ("怪物", "敌人", "新怪", "首领", "monster", "enemies"),
    "dungeons": ("地区", "地區", "地牢模组", "dungeon", "region", "地图"),
}
FILES = re.compile(r"文件|资源路径|源码|技能|数值|饰品|配置里|改了什么|改了哪些|\.(?:darkest|xml|json|csv|txt|md|lua)(?![a-z0-9_])")
FOLLOW_UP = re.compile(r"它|这个|这些|这一|那个|那些|刚才|上面|上次|上一篇|上一条|them\b|\bit\b")
MISSING = re.compile(r"没(?:有|加|添|装|覆盖|皮肤)|未(?:加|添|安|覆盖|找)|还缺|缺哪些|空缺|missing|without")
ORDINAL = re.compile(r"第\s*([0-9]{1,4}|[一二两三四五六七八九十]{1,3})\s*(?:个|项|条)")
PLAN_NUMBER = re.compile(r"#\s*(\d{1,4})(?!\d)")
INTERPRET = re.compile(r"解释|分析|为什么|是否兼容|有冲突吗|能一起用|怎么用|what does|explain")
CATALOG = re.compile(r"哪些|哪几|几个|多少|当前|现在|已启用|已装|我加|我装|方案|列出|清单|整理|统计|\b(?:which|list|installed)\b")
PLAN_CONTENTS = re.compile(
    r"(?:给我|整理|列出|查看|看看)(?:一下|下)?(?:我(?:的)?|当前|默认|这个)?(?:方案|配置)"
    r"|(?:方案|配置)(?:里|中|的)(?:都)?(?:有|包含|清单|内容)"
    r"|(?:我|当前|现在)(?:都|已经|已)?(?:启用|安装|装|加|用)(?:了)?(?:哪[些几]|什么|啥|多少)"
    r"|(?:我|当前|现在)(?:都)?有(?:哪[些几]|什么|啥|多少)\s*(?:mod|模组)"
)


@dataclass(frozen=True)
class PlanLookup:
    text: str
    matches: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()
    focus: str = ""
    file: str = ""


def in_reply_order(lookup: PlanLookup, reply: str, entry_ids: tuple[str, ...]) -> PlanLookup:
    if len(lookup.matches) < 2:
        return lookup
    matches = set(lookup.matches)
    entries_by_number = {index: entry for index, entry in enumerate(entry_ids, 1) if entry in matches}
    reply_numbers = (int(number) for number in PLAN_NUMBER.findall(reply))
    mentioned = tuple(dict.fromkeys(
        entries_by_number[number] for number in reply_numbers if number in entries_by_number
    ))
    if not mentioned:
        return lookup
    return replace(lookup, matches=mentioned, focus=mentioned[0] if len(mentioned) == 1 else "")


def ordinal_number(value: str) -> int:
    if value.isdecimal():
        return int(value)
    digits = {name: index for index, name in enumerate("一二三四五六七八九", 1)} | {"两": 2}
    if "十" in value:
        tens, _, units = value.partition("十")
        return digits.get(tens, 1) * 10 + digits.get(units, 0)
    return digits.get(value, 0)


def topic_matches(query: str) -> tuple[str, ...]:
    return tuple(
        topic for topic, words in TOPICS.items() if any(
            word in query if not word.isascii() else re.search(rf"\b{word}s?\b", query)
            for word in words
        )
    )


def mod_category(row, classifications):
    item = classifications.get(row.mod.key) if row.mod else None
    return item.category if item else None


def appearance_targets(row, classifications) -> dict[str, str]:
    if not row.mod:
        return {}
    category = mod_category(row, classifications)
    result = {}
    for path in row.mod.skin_files:
        if appearance := skin_resource(path):
            hero, is_variant = appearance
            if is_variant or category == Category.SKIN or hero not in row.mod.hero_definitions:
                result.setdefault(hero, path)
    return result


def names_for_heroes(heroes) -> str:
    return "、".join(HERO_NAMES.get(hero, hero) for hero in heroes)


def mod_name(row) -> str:
    return row.mod.name if row.mod else row.entry.name


def line_for(number, row) -> str:
    return f"#{number:02d} {mod_name(row)}"


def find_target(query, entries, previous, selected):
    if number := PLAN_NUMBER.search(query):
        index = int(number[1]) - 1
        return entries[index][1].entry.id if 0 <= index < len(entries) else "-"
    if (number := ORDINAL.search(query)) and previous and previous.matches:
        index = ordinal_number(number[1]) - 1
        return previous.matches[index] if 0 <= index < len(previous.matches) else "-"
    named = []
    generic_names = {word for words in TOPICS.values() for word in words}
    for _, row in entries:
        names = {search_text(mod_name(row)), search_text(row.entry.name)}
        workshop_id = row.entry.workshop_id or (row.mod.workshop_id if row.mod else None)
        if workshop_id and re.search(rf"(?<!\d){re.escape(workshop_id)}(?!\d)", query):
            return row.entry.id
        for name in names:
            if name not in generic_names and len(name) >= (4 if name.isascii() else 3) and name in query:
                named.append((len(name), row.entry.id))
    if named:
        return max(named, key=lambda item: item[0])[1]
    if "选中" in query:
        return selected
    if FOLLOW_UP.search(query):
        if previous and (previous.focus or previous.matches or previous.topics):
            return previous.focus
        return selected
    return ""


def list_inventory(entries, classifications, topics, hero_filter=()):
    matches = []
    sections = []
    for topic in topics:
        lines = []
        for number, row in entries:
            if not row.mod:
                continue
            category = mod_category(row, classifications)
            kinds = row.mod.resource_kinds
            if topic == "skins":
                appearances = appearance_targets(row, classifications)
                if not appearances and category != Category.SKIN:
                    continue
                if hero_filter and not set(hero_filter).intersection(appearances):
                    continue
                suffix = f" → {names_for_heroes(appearances)}" if appearances else "（对应角色待确认）"
            elif topic == "expansions" and category == Category.EXPANSION:
                suffix = ""
            elif topic == "monsters" and "monster" in kinds:
                suffix = "（有怪物定义相关文件）"
            elif topic == "dungeons" and "dungeon" in kinds:
                suffix = "（有地区定义相关文件）"
            else:
                continue
            lines.append(line_for(number, row) + suffix)
            matches.append(row.entry.id)
        label = {"skins": "皮肤与人物外观", "expansions": "扩充与玩法调整",
                 "monsters": "涉及怪物的 Mod", "dungeons": "涉及地区的 Mod"}[topic]
        if topic == "skins" and hero_filter:
            label += f" · {names_for_heroes(hero_filter)}"
        sections.append(f"{label}（{len(lines)} 项）\n" + ("\n".join(lines) or "当前方案没有查到匹配项。"))
    return PlanLookup("\n\n".join(sections), tuple(matches), topics, matches[0] if len(matches) == 1 else "")


def skin_coverage(entries, classifications, hero_filter=()):
    found = defaultdict(list)
    uncertain = 0
    for number, row in entries:
        if not row.mod:
            continue
        appearances = appearance_targets(row, classifications)
        if mod_category(row, classifications) == Category.SKIN and not appearances:
            uncertain += 1
        for hero in appearances:
            found[hero].append(line_for(number, row))
    sections = []
    for label, heroes in (("游戏本体", BASE_HEROES), ("官方 DLC 角色", DLC_HEROES)):
        applicable = [key for key in heroes if not hero_filter or key in hero_filter]
        if not applicable:
            continue
        missing = [key for key in applicable if key not in found]
        covered = [key for key in applicable if key in found]
        lines = [f"{label} · {len(covered)}/{len(applicable)} 个角色查到外观资源"]
        if missing:
            lines.append(f"未查到皮肤（{len(missing)}）  {names_for_heroes(missing)}")
        if covered:
            lines.append("查到的皮肤来源")
            lines.extend(f"{heroes[key]}  " + "、".join(found[key]) for key in covered)
        sections.append("\n".join(lines))
    note = "按当前已定位 Mod 的人物动画和配色资源统计，DLC 角色单独列出。"
    if uncertain:
        note += f" 有 {uncertain} 个皮肤分类的 Mod 暂未确认目标。"
    return PlanLookup("\n\n".join((*sections, note)), topics=("skins",))


def lookup_plan(
    question: str, rows: list[ResolvedEntry], classifications: dict[str, Classification],
    *, previous: PlanLookup | None = None, selected: str = "",
) -> PlanLookup | None:
    query = search_text(question)
    entries = list(enumerate(rows, 1))
    by_id = {row.entry.id: (number, row) for number, row in entries}
    topics = topic_matches(query)
    hero_filter = tuple(dict.fromkeys(
        hero for name, hero in ({label: hero for hero, label in HERO_NAMES.items()} | HERO_ALIASES).items()
        if name in query or hero in query
    ))
    target = find_target(query, entries, previous, selected)
    missing = MISSING.search(query) and ("skins" in topics or (previous and "skins" in previous.topics))
    if entries and (missing or topics) and not target and not any(row.mod for _, row in entries):
        return PlanLookup("当前方案的 Mod 还没有匹配到本机安装。先扫描 Mod，再查皮肤或分类。", topics=topics)
    if missing:
        return skin_coverage(entries, classifications, hero_filter)
    files = FILES.search(query)
    if files and not target and not topics:
        if previous and len(previous.matches) > 1:
            return PlanLookup("刚才列出了多个 Mod。可以说「第二个的文件」或输入当前列表里的 #序号。")
        target = (previous.focus if previous else "") or selected
    if target and target not in by_id:
        return PlanLookup("这个 Mod 已不在本次可查询的方案里。可以输入当前列表中的 #序号。")
    if files and (target or not topics):
        if not target:
            return PlanLookup("要查哪个 Mod 的文件？可输入「查 #12 的技能文件」，或先选中一个计划条目。")
        number, row = by_id[target]
        if not row.mod:
            return PlanLookup(f"{line_for(number, row)} 还未定位，匹配本机安装后就能查文件。", (target,), focus=target)
        previous_file = previous.file if previous and previous.focus == target else ""
        text, path = inspect_mod_files(
            Path(row.mod.path), question, previous_file=previous_file, subject_name=mod_name(row),
        )
        return PlanLookup(f"{line_for(number, row)}\n{text}", (target,), focus=target, file=path)
    if topics and not target and (hero_filter or CATALOG.search(query)):
        return list_inventory(entries, classifications, topics, hero_filter)
    if target:
        number, row = by_id[target]
        if not row.mod:
            return PlanLookup(f"{line_for(number, row)} 在当前方案里，安装目录还未定位。", (target,), focus=target)
        category = classifications.get(row.mod.key)
        lines = [line_for(number, row), f"已在当前方案启用 · {category.label if category else '待归类'}"]
        if appearances := appearance_targets(row, classifications):
            lines.append(f"查到外观资源  {names_for_heroes(appearances)}")
            lines.extend(appearances.values())
        if "monster" in row.mod.resource_kinds:
            lines.append("查到怪物相关定义。")
        elif "monsters" in topics:
            lines.append("当前扫描未查到这个 Mod 的怪物定义。")
        if "dungeon" in row.mod.resource_kinds:
            lines.append("查到地区相关定义。")
        if row.mod.description.strip():
            lines.append("作者简介  " + row.mod.description.strip()[:420])
        return PlanLookup("\n".join(lines), (target,), focus=target)
    if previous and previous.topics and FOLLOW_UP.search(query):
        if re.search(r"哪些|几个|名单|列|名字", query) or INTERPRET.search(query):
            return list_inventory(entries, classifications, previous.topics, hero_filter)
    if PLAN_CONTENTS.search(query):
        lines = []
        for number, row in entries:
            category = classifications.get(row.mod.key) if row.mod else None
            label = category.label if category else ("待分类" if row.mod else "未定位")
            lines.append(f"{line_for(number, row)}（{label}）")
        return PlanLookup(
            "当前方案的 Mod 清单\n" + "\n".join(lines) if lines else "当前方案还没有加入 Mod。",
            tuple(row.entry.id for _, row in entries),
            focus=entries[0][1].entry.id if len(entries) == 1 else "",
        )
    return None
