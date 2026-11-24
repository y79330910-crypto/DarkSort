import uuid
from dataclasses import replace

import pytest

from darksort.models import Profile, ProfileEntry
from darksort.profiles import ProfileStore, parse_profile, profile_dict, resolve_entries
from darksort.sorting import move_entries


def test_profile_create_save_export_import_and_switch(tmp_path, mods):
    store = ProfileStore(tmp_path / "数据")
    profile = store.new("中文配置")
    profile.entries = [ProfileEntry.from_mod(mod) for mod in reversed(mods)]
    store.save(profile)
    another = store.new("空配置")
    exported = tmp_path / "分享.json"
    store.export_file(profile, exported)
    imported = store.import_file(exported)
    assert imported.id != profile.id
    assert imported.entries == profile.entries
    assert store.load(another.id).entries == []
    assert store.load(profile.id).entries == profile.entries
    assert len(store.list()[0]) == 3


def test_missing_and_ambiguous_mods_keep_exact_order(tmp_path, mods):
    entries = [ProfileEntry.from_mod(mod) for mod in mods]
    duplicate = replace(mods[0], path=str(tmp_path / "other"))
    result = resolve_entries(entries, [mods[0], duplicate])
    assert [row.entry for row in result] == entries
    assert all(row.mod is None for row in result)
    assert "多个" in result[0].problem


def test_workshop_can_move_library_but_local_does_not_fuzzy_match(tmp_path, mods):
    entries = [ProfileEntry.from_mod(mods[0]), ProfileEntry.from_mod(mods[2])]
    moved = [
        replace(mods[0], path=str(tmp_path / "new_workshop")),
        replace(mods[2], path=str(tmp_path / "new_local")),
    ]
    result = resolve_entries(entries, moved)
    assert result[0].mod == moved[0]
    assert result[1].mod is None


def test_user_pin_resolves_duplicate_workshop_candidate(tmp_path, mods):
    duplicate = replace(mods[0], path=str(tmp_path / "duplicate"))
    entry = ProfileEntry.from_mod(mods[0], pinned=True)
    assert resolve_entries([entry], [mods[0], duplicate])[0].mod == mods[0]


def test_atomic_save_failure_keeps_original(tmp_path, mods, monkeypatch):
    import darksort.storage as storage

    store = ProfileStore(tmp_path)
    profile = store.new("Original")
    original = store.path(profile.id).read_bytes()
    profile.entries = [ProfileEntry.from_mod(mods[0])]

    def fail_replace(*_):
        raise PermissionError("simulated locked destination")

    monkeypatch.setattr(storage.os, "replace", fail_replace)
    with pytest.raises(PermissionError):
        store.save(profile)
    assert store.path(profile.id).read_bytes() == original
    assert not list(store.directory.glob("*.tmp"))


@pytest.mark.parametrize(
    "patch",
    [{"version": 999}, {"id": "../../escape"}, {"entries": "invalid"}, {"name": ""}, {"id": 123}],
)
def test_invalid_import_rejected_without_new_profile(tmp_path, patch):
    from darksort.storage import atomic_json

    data = profile_dict(Profile(str(uuid.uuid4()), "Valid")) | patch
    imported = tmp_path / "invalid.json"
    atomic_json(imported, data)
    store = ProfileStore(tmp_path / "data")
    with pytest.raises(ValueError):
        store.import_file(imported)
    assert store.list()[0] == []


def test_multi_selection_move_is_stable_and_serializable(mods):
    entries = [ProfileEntry.from_mod(mod) for mod in mods]
    moved = move_entries(entries, [0, 2], 3)
    assert moved == [entries[1], entries[0], entries[2]]
    assert move_entries(entries, [1], 1) == entries
    assert parse_profile(profile_dict(Profile(str(uuid.uuid4()), "Moved", moved))).entries == moved
