from dataclasses import replace
from zipfile import ZipFile

import pytest

from darksort.models import ModGroup, OrderRule, ProfileEntry, ProfileGroup, ProfileLock
from darksort.profiles import ProfileStore, profile_dict, resolve_entries
from darksort.sharing import (
    LIST_FILE,
    PROFILE_FILE,
    group_from_template,
    read_group_template,
    read_share_package,
    write_group_template,
    write_share_package,
)


def test_share_round_trip_keeps_local_identities_locks_groups_and_rules(tmp_path, mods):
    workshop, _, local = (ProfileEntry.from_mod(mod, pinned=True) for mod in mods)
    other = replace(local, path=str(tmp_path / "第二份"), id=ProfileEntry.from_mod(mods[2]).id)
    store = ProfileStore(tmp_path / "data")
    profile = store.new("远征", [local, workshop])
    group = ModGroup("人物和皮肤", "#bd99e8", (other, workshop, local))
    profile.mod_groups = [group]
    profile.staged = [other]
    profile.groups = [ProfileGroup(local.id, "开头")]
    profile.locks = [ProfileLock("配套", (local.id, workshop.id))]
    profile.rules = [
        OrderRule(local.reference, workshop.reference),
        OrderRule(f"group:{group.id}", other.reference),
        OrderRule("tag:人物", f"group:{group.id}"),
        OrderRule(f"local:{tmp_path / '尚未加入计划'}", workshop.reference, enabled=False),
    ]
    store.save(profile)
    path = tmp_path / "远征.darksort.zip"
    write_share_package(profile, path)

    with ZipFile(path) as archive:
        raw = archive.read(PROFILE_FILE).decode("utf-8")
        assert str(tmp_path).replace("\\", "\\\\") not in raw
        assert "尚未加入计划" not in raw
        manifest = archive.read(LIST_FILE).decode("utf-8-sig")
        assert "顶部优先" in manifest
        assert str(tmp_path) not in manifest
        assert "https://steamcommunity.com/sharedfiles/filedetails/?id=1001" in manifest
    imported = store.import_profile(read_share_package(path))
    assert imported.id != profile.id
    assert store.load(profile.id).entries == [local, workshop]
    assert [entry.id for entry in imported.entries] == [local.id, workshop.id]
    assert imported.groups == profile.groups and imported.locks == profile.locks
    waiting, available = imported.entries
    assert not waiting.path and not available.path and not available.pinned_path
    assert waiting.name == imported.staged[0].name and waiting.reference != imported.staged[0].reference
    assert [member.reference for member in imported.mod_groups[0].members] == [
        imported.staged[0].reference, workshop.reference, waiting.reference,
    ]
    assert imported.rules[0].first == waiting.reference
    assert imported.rules[1].second == imported.staged[0].reference
    assert imported.rules[2] == profile.rules[2]
    assert store.load(imported.id).mod_groups == imported.mod_groups
    assert profile_dict(imported)["version"] == 5

    relocated = replace(mods[0], path=str(tmp_path / "新库"))
    rows = resolve_entries(imported.entries, [relocated, mods[2]])
    assert rows[0].mod is None
    assert rows[1].mod == relocated
    assert resolve_entries([available], [relocated, mods[0]])[0].mod is None


def test_group_template_creates_new_named_copy_and_matches_workshop_only(tmp_path, mods):
    entries = tuple(ProfileEntry.from_mod(mod, pinned=True) for mod in mods)
    group = ModGroup("地下城", "#75b9ea", (entries[2], entries[1], entries[0]))
    profile = ProfileStore(tmp_path / "data").new("接受模板")
    profile.mod_groups = [group]
    path = tmp_path / "组合.darksort-group.json"
    write_group_template(group, path)
    template = read_group_template(path)
    imported = group_from_template(profile, template)
    assert imported.title == "地下城 (2)" and imported.id != group.id and imported.color == group.color
    assert [member.name for member in imported.members] == [member.name for member in group.members]
    assert all(not member.path and not member.pinned_path for member in imported.members)
    assert [row.mod for row in resolve_entries(imported.members, mods)] == [None, mods[1], mods[0]]
    assert not profile.entries and profile.mod_groups == [group]


def test_share_rejects_invalid_or_oversize_manifest(tmp_path, monkeypatch):
    path = tmp_path / "broken.zip"
    path.write_bytes(b"test file")
    with pytest.raises(ValueError, match="无法读取"):
        read_share_package(path)

    monkeypatch.setattr("darksort.sharing.MAX_PROFILE_BYTES", 10)
    with ZipFile(path, "w") as archive:
        archive.writestr(PROFILE_FILE, "[]" * 6)
    with pytest.raises(ValueError, match="过大"):
        read_share_package(path)
