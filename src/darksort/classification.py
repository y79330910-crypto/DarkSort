from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from darksort.models import Mod


class Category(StrEnum):
    FONT = "font"
    TRANSLATION = "translation"
    PATCH = "patch"
    EXPANSION = "expansion"
    REGION = "region"
    HERO = "hero"
    SKIN = "skin"
    TEXTURE = "texture"

    @property
    def label(self) -> str:
        return LABELS[self]


LABELS = {
    Category.FONT: "字体",
    Category.TRANSLATION: "汉化",
    Category.PATCH: "补丁",
    Category.EXPANSION: "扩充与调整",
    Category.REGION: "地区与怪物",
    Category.HERO: "人物",
    Category.SKIN: "皮肤",
    Category.TEXTURE: "贴图美化",
}
PRIORITY = {category: index for index, category in enumerate(Category)}

HINTS = {
    Category.FONT: re.compile(r"字体|字型|\bfonts?\b|\btypeface\b"),
    Category.TRANSLATION: re.compile(
        r"汉化|漢化|中文化|中文翻译|中文翻譯|简体中文|繁體中文|"
        r"\b(?:locali[sz]ation|translations?|chinese patch|chinese translation)\b"
    ),
    Category.PATCH: re.compile(
        r"补丁|補丁|兼容|相容|修复|修復|\b(?:patch|fix|compatibility|hotfix)\b"
    ),
    Category.EXPANSION: re.compile(
        r"扩充|扩展|拓展|玩法调整|\b(?:expansion|overhaul|gameplay tweaks?|qol)\b"
    ),
    Category.REGION: re.compile(
        r"地区|地區|新地牢|新怪|怪物|首领|首領|\b(?:monsters?|enemies|new dungeon|new region|bosses)\b"
    ),
    Category.HERO: re.compile(
        r"新人物|新角色|新英雄|新职业|新職業|\b(?:hero class|character class|class mod|new hero)\b"
    ),
    Category.SKIN: re.compile(r"皮肤|皮膚|换装|換裝|\b(?:skins?|reskin|outfits?)\b"),
    Category.TEXTURE: re.compile(
        r"贴图|貼圖|美化|\b(?:retexture|textures?|graphics|visual overhaul)\b"
    ),
}


@dataclass(frozen=True)
class Classification:
    category: Category | None
    reason: str

    @property
    def label(self) -> str:
        return self.category.label if self.category else "待归类"


def classify_mod(mod: Mod, override: str = "") -> Classification:
    if override in PRIORITY:
        return Classification(Category(override), "手动指定的分类")
    text = " · ".join((mod.name, *mod.tags)).casefold()
    hints = {category for category, hint in HINTS.items() if hint.search(text)}
    resources = set(mod.resource_kinds)
    gameplay = "gameplay" in resources or bool(mod.hero_definitions) or "region" in resources

    if "font" in resources or Category.FONT in hints:
        return Classification(
            Category.FONT, "字体资源" if "font" in resources else "标题或标签标明字体"
        )
    if (
        Category.TRANSLATION in hints and not mod.hero_definitions and "region" not in resources
    ) or (
        "translation" in resources
        and not gameplay
        and not mod.skin_targets
        and not hints.intersection({Category.PATCH, Category.SKIN})
    ):
        return Classification(Category.TRANSLATION, "本地化资源或翻译标记")
    if Category.PATCH in hints:
        return Classification(Category.PATCH, "标题或标签标明修复、补丁或兼容")
    if Category.SKIN in hints or (mod.skin_targets and not gameplay):
        return Classification(Category.SKIN, "人物皮肤资源或换装标记")
    if (
        mod.hero_definitions
        or Category.HERO in hints
        or any(
            tag.casefold() in {"hero", "heroes", "class", "人物", "英雄", "职业", "角色"}
            for tag in mod.tags
        )
    ):
        return Classification(
            Category.HERO, "发现人物定义" if mod.hero_definitions else "标题或标签标明人物"
        )
    if Category.TEXTURE in hints and not gameplay:
        return Classification(Category.TEXTURE, "标题或标签标明贴图美化")
    if "region" in resources or Category.REGION in hints:
        return Classification(Category.REGION, "地区或怪物定义及其标记")
    if (
        gameplay
        or Category.EXPANSION in hints
        or any(
            tag.casefold() in {"tools", "hints", "level limits", "balance", "trinkets"}
            for tag in mod.tags
        )
    ):
        return Classification(Category.EXPANSION, "玩法、资源规则或扩充标记")
    if "texture" in resources or Category.TEXTURE in hints:
        return Classification(Category.TEXTURE, "贴图或美化资源及其标记")
    return Classification(None, "缺少明确的资源、标题或标签")
