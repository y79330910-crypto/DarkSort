from dataclasses import replace

from darksort.conflicts import analyze_conflicts
from darksort.history import ProfileHistory, compare_entries, describe_change
from darksort.models import Mod, OrderRule, ProfileEntry, ProfileGroup, Source
from darksort.profiles import ProfileStore, profile_dict, resolve_entries
from darksort.sorting import preview_order
from darksort.storage import atomic_json
from darksort.tags import TagStore


def test_v1_profile_migrates_only_on_save_and_keeps_identity(tmp_path, mods):
    store = ProfileStore(tmp_path)
    original = store.new("旧方案")
    old_data = profile_dict(original) | {"version": 1}
    old_data["entries"] = [
        {key: value for key, value in ProfileEntry.from_mod(mod).to_dict().items() if key != "id"}
        for mod in mods
    ]
    old_data.pop("groups")
    old_data.pop("rules")
    path = store.path(original.id)
    atomic_json(path, old_data)
    before = path.read_bytes()
    profile = store.load(original.id)
    assert [entry.id for entry in store.load(original.id).entries] == [
        entry.id for entry in profile.entries
    ]
    assert path.read_bytes() == before
    profile.groups.append(ProfileGroup(profile.entries[0].id, "界面"))
    profile.rules.append(OrderRule(profile.entries[0].reference, profile.entries[1].reference))
    store.save(profile)
    assert profile_dict(store.load(original.id)) == profile_dict(profile)
    assert store.load(original.id).id == original.id


def test_named_history_and_duplicate_are_isolated(tmp_path, mods):
    store = ProfileStore(tmp_path)
    profile = store.new("计划", [ProfileEntry.from_mod(mod) for mod in mods])
    history = ProfileHistory(tmp_path)
    snapshot = history.capture(profile, "排序前", name="基准")
    profile.entries.reverse()
    duplicate = store.duplicate(profile, "副本")
    assert duplicate.id != profile.id
    snapshots, errors = history.list(profile.id)
    assert not errors
    assert snapshots[0].id == snapshot.id
    assert snapshots[0].name == "基准"
    assert snapshots[0].profile.entries[0].name == mods[0].name
    assert "移位" in describe_change(profile, snapshots[0].profile)
    assert history.list(duplicate.id)[0] == []


def test_compare_entries_prefers_stable_ids_for_duplicates(tmp_path, mods):
    a, b, c = [ProfileEntry.from_mod(mod) for mod in mods]
    duplicate = replace(a, id=ProfileEntry.from_mod(mods[0]).id)
    before = ProfileStore(tmp_path).new("比对", [a, b, duplicate, c])
    after = replace(before, entries=[duplicate, a, b])
    changes = compare_entries(before, after)
    assert [(row.before, row.after, row.status) for row in changes] == [
        (3, 1, "移位"), (1, 2, "顺延"), (2, 3, "顺延"), (4, None, "移除")
    ]


def test_stable_topological_sort_keeps_unresolved_slots(tmp_path, mods):
    a, b, c = [ProfileEntry.from_mod(mod) for mod in mods]
    missing = ProfileEntry("尚未安装", Source.LOCAL, str(tmp_path / "missing"))
    profile = ProfileStore(tmp_path).new("排序", [a, missing, b, c])
    profile.rules = [
        OrderRule(c.reference, a.reference),
        OrderRule(b.reference, missing.reference, "requires"),
    ]
    preview = preview_order(profile, resolve_entries(profile.entries, mods))
    assert preview.entries == (b, missing, c, a)
    assert "缺少" in preview.notices[0]
    assert not preview.blockers
    profile.entries = list(preview.entries)
    assert preview_order(profile, resolve_entries(profile.entries, mods)).entries == preview.entries


def test_cycle_reports_edges_without_applying_and_a_disabled_rule_resolves_it(tmp_path, mods):
    a, b, c = [ProfileEntry.from_mod(mod) for mod in mods]
    profile = ProfileStore(tmp_path).new("循环", [a, b, c])
    profile.rules = [
        OrderRule(a.reference, b.reference),
        OrderRule(b.reference, c.reference),
        OrderRule(c.reference, a.reference, note="来自兼容补丁"),
    ]
    resolved = resolve_entries(profile.entries, mods)
    result = preview_order(profile, resolved)
    assert result.entries == (a, b, c)
    assert len(result.blockers) == 3
    assert "来自兼容补丁" in "\n".join(result.blockers)
    profile.rules[2] = replace(profile.rules[2], enabled=False)
    assert not preview_order(profile, resolved).blockers


def test_rules_block_ambiguous_logical_id(tmp_path, mods):
    a, b = [ProfileEntry.from_mod(mod) for mod in mods[:2]]
    profile = ProfileStore(tmp_path).new("重复", [a, replace(a, id=ProfileEntry.from_mod(mods[0]).id), b])
    profile.rules = [OrderRule(a.reference, b.reference)]
    assert "多个" in preview_order(profile, resolve_entries(profile.entries, mods)).blockers[0]


def test_custom_tag_identity_survives_workshop_move(tmp_path, mods):
    tags = TagStore(tmp_path)
    tags.set(mods[0].reference, "补丁，UI, ui")
    moved = replace(mods[0], path=str(tmp_path / "moved"))
    assert TagStore(tmp_path).tags[moved.reference] == ("补丁", "UI")


def write_mod(tmp_path, name, files, manifest):
    root = tmp_path / name
    root.mkdir()
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    if manifest is not None:
        (root / "modfiles.txt").write_text(
            "\n".join(f"{relative} {len(files.get(relative, b''))}" for relative in manifest),
            encoding="utf-8",
        )
    return Mod(str(root), Source.LOCAL, name)


def test_manifest_backed_hero_overlap_uses_top_priority_and_reacts_to_reorder(tmp_path):
    hero = "heroes/crusader/crusader.info.darkest"
    other = "overlap with space.json"
    first = write_mod(tmp_path, "top", {hero: b"A", other: b"same"}, [hero, other])
    second = write_mod(tmp_path, "bottom", {hero: b"B", other: b"same"}, [hero, other])
    entries = [ProfileEntry.from_mod(mod) for mod in (first, second)]
    arranged = resolve_entries(entries, [first, second])
    result = {conflict.path: conflict for conflict in analyze_conflicts(arranged).conflicts}
    assert result[hero].winner.mod == first
    assert result[other].winner is None and result[other].status == "文件内容相同"
    result = {conflict.path: conflict for conflict in analyze_conflicts(arranged[::-1]).conflicts}
    assert result[hero].winner.mod == second


def test_incomplete_manifest_provider_never_gets_a_definitive_winner(tmp_path):
    hero = "heroes/crusader/crusader.info.darkest"
    first = write_mod(tmp_path, "missing", {}, [hero])
    second = write_mod(tmp_path, "bottom", {hero: b"B"}, [hero])
    entries = [ProfileEntry.from_mod(mod) for mod in (first, second)]
    result = analyze_conflicts(resolve_entries(entries, [first, second]))
    assert result.conflicts[0].status == "清单中的文件缺失"
    assert result.conflicts[0].winner is None


def test_manifest_rejects_escaping_paths_without_counting_them(tmp_path):
    first = write_mod(tmp_path, "unsafe", {"visible.json": b"A"}, ["../outside.json"])
    second = write_mod(tmp_path, "valid", {"visible.json": b"B"}, None)
    entries = [ProfileEntry.from_mod(mod) for mod in (first, second)]
    rows = resolve_entries(entries, [first, second])
    result = analyze_conflicts(rows)
    assert [item.path for item in result.conflicts] == ["visible.json"]
    assert any("不安全" in issue for issue in result.issues)
    assert analyze_conflicts(rows, cancelled=lambda: True).cancelled


def test_additive_resource_does_not_claim_one_file_wins(tmp_path):
    resource = "campaign/town/districts/stock.districts.json"
    mods = [
        write_mod(tmp_path, str(index), {resource: bytes([index])}, [resource])
        for index in range(2)
    ]
    rows = resolve_entries([ProfileEntry.from_mod(mod) for mod in mods], mods)
    overlap = analyze_conflicts(rows).conflicts[0]
    assert overlap.winner is None
    assert "叠加" in overlap.status
