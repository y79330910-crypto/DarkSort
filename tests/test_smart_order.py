from dataclasses import replace
from pathlib import Path

from darksort.classification import Category, classify_mod
from darksort.models import Mod, OrderRule, ProfileEntry, ScanRoot, Source
from darksort.profiles import ProfileStore, resolve_entries
from darksort.scanning.scanner import scan
from darksort.sorting import preview_order


def test_smart_order_uses_eight_tiers_and_places_only_paired_skins_above_heroes(tmp_path):
    fixtures = [
        ("地城贴图美化", ("texture",), (), ()),
        ("Crusader Skin", ("texture",), (), ("crusader",)),
        ("巫师", ("gameplay",), ("witch",), ()),
        ("未分类样本", (), (), ()),
        ("法师皮肤", ("texture",), (), ("witch",)),
        ("怪物与新地区", ("region",), (), ()),
        ("玩法扩展", ("gameplay",), (), ()),
        ("兼容补丁", ("gameplay",), (), ()),
        ("中文汉化补丁", ("translation",), (), ()),
        ("中文字体重做", ("font",), (), ()),
    ]
    mods = [
        Mod(
            str(tmp_path / str(index)),
            Source.LOCAL,
            name,
            resource_kinds=kinds,
            hero_definitions=heroes,
            skin_targets=skins,
        )
        for index, (name, kinds, heroes, skins) in enumerate(fixtures)
    ]
    entries = [ProfileEntry.from_mod(mod) for mod in mods]
    profile = ProfileStore(tmp_path).new("八类示例", entries)
    classifications = {mod.key: classify_mod(mod) for mod in mods}
    preview = preview_order(profile, resolve_entries(entries, mods), classifications)
    assert not preview.blockers
    expected = [9, 8, 7, 3, 6, 5, 4, 2, 1, 0]
    assert list(preview.entries) == [entries[i] for i in expected]
    assert "保留原位置" in preview.decisions[3].reason
    assert "巫师" in next(row.reason for row in preview.decisions if row.name == "法师皮肤")
    current = replace(profile, entries=list(preview.entries))
    again = preview_order(current, resolve_entries(current.entries, mods), classifications)
    assert again.entries == preview.entries


def test_user_rule_can_override_skin_hint_without_creating_a_cycle(tmp_path):
    hero = Mod(str(tmp_path / "hero"), Source.LOCAL, "旧神", hero_definitions=("elder",))
    skin = Mod(str(tmp_path / "skin"), Source.LOCAL, "旧神皮肤", skin_targets=("elder",))
    entries = [ProfileEntry.from_mod(skin), ProfileEntry.from_mod(hero)]
    profile = ProfileStore(tmp_path).new("有自定义规则", entries)
    profile.rules = [OrderRule(hero.reference, skin.reference)]
    mods = [hero, skin]
    result = preview_order(
        profile, resolve_entries(entries, mods), {mod.key: classify_mod(mod) for mod in mods}
    )
    assert not result.blockers
    assert result.entries == tuple(reversed(entries))
    assert "自定义规则" in result.notices[0]


def test_resource_evidence_ignores_translation_files_bundled_with_a_hero_and_refreshes_cache(
    tmp_path,
):
    parent = tmp_path / "Mods"
    folder = parent / "my-mod"
    folder.mkdir(parents=True)
    (folder / "project.xml").write_text("<project><Title>Mixed</Title></project>", encoding="utf-8")
    manifest = folder / "modfiles.txt"
    manifest.write_text(
        "heroes/custom/custom.info.darkest 1\nlocalization/main.loc2 1", encoding="utf-8"
    )
    cache = tmp_path / "cache.db"
    roots = [ScanRoot(parent, Source.LOCAL)]
    first = scan(roots, cache)
    assert classify_mod(first.mods[0]).category is Category.HERO
    assert scan(roots, cache).cache_hits == 1
    manifest.write_text("localization/main.loc2 10", encoding="utf-8")
    refreshed = scan(roots, cache)
    assert refreshed.cache_hits == 0
    assert classify_mod(refreshed.mods[0]).category is Category.TRANSLATION
    assert classify_mod(refreshed.mods[0], "patch").reason == "手动指定的分类"


def test_legacy_mod_skin_target_comes_from_physical_character_assets(tmp_path):
    root = tmp_path / "Mods"
    skin = root / "Skin" / "heroes" / "shieldbreaker" / "shieldbreaker_A"
    skin.mkdir(parents=True)
    Path(skin, "shieldbreaker.walk.png").write_bytes(b"fixture")
    result = scan([ScanRoot(root, Source.LOCAL)])
    assert result.mods[0].skin_targets == ("shieldbreaker",)
    assert classify_mod(result.mods[0]).category is Category.SKIN
