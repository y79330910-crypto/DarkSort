from __future__ import annotations

import sys
from pathlib import Path

import pytest

from darksort.adapters import saves
from darksort.adapters.dson import UGCS, DsonDocument, DsonField, SavedMod
from darksort.models import ProfileEntry, Source
from darksort.profiles import resolve_entries
from darksort.scanning.metadata import parse_mod

WORKSHOP = SavedMod("1234567890", "Steam")
LOCAL = SavedMod("本地标题", "mod_local_source")


def make_save(*, revision=28385, in_raid=False, mods=(WORKSHOP, LOCAL)):
    header = (0xB101, revision << 16, 64, 0, 0, 0, 64, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    persistent = DsonField(
        "persistent_ugcs",
        children=(DsonField(UGCS, children=(DsonField("3", children=()),)),),
    )
    fields = (
        DsonField("inraid", body_offset=0, raw=bytes([int(in_raid)])),
        DsonField(UGCS, children=()),
        persistent,
        DsonField("counter", raw=b"\x03\x00\x00\x00"),
        DsonField("counter", raw=b"\x12\x00\x00\x00"),
    )
    return DsonDocument(header, (DsonField("base_root", children=fields),)).with_mods(mods).encode()


def make_target(tmp_path, data):
    target = tmp_path / "profile_3" / "persist.game.json"
    target.parent.mkdir()
    target.write_bytes(data)
    return target


@pytest.mark.parametrize("target_mods", [(LOCAL, WORKSHOP), (), (WORKSHOP,)])
def test_dson_rewrite_preserves_other_fields_and_opaque_duplicate_values(tmp_path, target_mods):
    original = make_save()
    target = make_target(tmp_path, original)
    state = saves.read_slot(target)
    plan = saves.prepare_write(state, target_mods)
    before, after = DsonDocument.read(original), DsonDocument.read(plan.payload)
    assert before.mods() == (WORKSHOP, LOCAL)
    assert after.mods() == target_mods
    assert before.outside_mods() == after.outside_mods()
    assert after.encode() == plan.payload
    assert target.read_bytes() == original


@pytest.mark.parametrize("version,active", [(11234, False), (28385, True)])
def test_unknown_revision_or_raid_can_be_read_but_not_prepared(tmp_path, version, active):
    state = saves.read_slot(make_target(tmp_path, make_save(revision=version, in_raid=active)))
    assert state.mods == (WORKSHOP, LOCAL)
    assert state.read_only_reason
    with pytest.raises(ValueError):
        saves.prepare_write(state, ())


def test_decoded_list_uses_numeric_keys_and_imports_unknown_as_placeholder(tmp_path):
    raw = b'{"base_root":{"applied_ugcs_1_0":{"10":{"name":"test","source":"other"},"2":{"name":"123","source":"Steam"}}}}'
    state = saves.read_slot(make_target(tmp_path, raw))
    assert [item.name for item in state.mods] == ["123", "test"]
    assert state.read_only_reason
    entries = saves.entries_from_slot(state, [])
    assert entries[0].workshop_id == "123"
    assert entries[1].name.startswith("test") and not entries[1].path


def test_native_fresh_campaign_json_is_readable_as_an_empty_mod_list(tmp_path):
    raw = b'{"version":2,"data":{"totalelapsed":0,"inraid":true,"raiddungeon":"tutorial","estatename":"New Estate"}}'
    target = make_target(tmp_path, raw)
    state = saves.read_slot(target)
    assert state.mods == () and state.read_only_reason
    assert target.read_bytes() == raw


def test_local_mapping_requires_an_installed_game_mod_and_uses_xml_title(tmp_path):
    game = tmp_path / "Game"
    folder = game / "mods" / "different-directory-name"
    folder.mkdir(parents=True)
    (folder / "project.xml").write_text("<project><Title>本地标题</Title></project>", encoding="utf-8")
    mod = parse_mod(folder, Source.LOCAL)
    resolved = resolve_entries([ProfileEntry.from_mod(mod)], [mod])
    assert saves.mods_for_slot(resolved, [mod], [game]) == (LOCAL,)
    with pytest.raises(ValueError, match="不唯一"):
        saves.mods_for_slot(resolved, [mod], [tmp_path / "Other Game"])


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ReplaceFileW transaction")
def test_native_replace_backup_restore_and_reject_stale_preview(tmp_path, monkeypatch):
    monkeypatch.setattr(saves, "require_game_stopped", lambda: None)
    original = make_save()
    target = make_target(tmp_path, original)
    store = saves.SaveBackups(tmp_path / "data")
    plan = saves.prepare_write(saves.read_slot(target), (LOCAL,))
    backup = saves.commit(plan, store)
    assert backup is not None and (backup.directory / target.name).read_bytes() == original
    assert saves.read_slot(target).mods == (LOCAL,)
    with pytest.raises(OSError, match="已经|已改变"):
        saves.commit(plan, store)
    stored = store.list(target)[0][0]
    restore = store.prepare_restore(saves.read_slot(target), stored)
    assert saves.commit(restore, store) is not None
    assert target.read_bytes() == original
    assert not list(target.parent.glob(".darksort-*"))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows sharing and recovery")
def test_partial_replace_restores_missing_target_and_retains_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(saves, "require_game_stopped", lambda: None)
    original = make_save()
    target = make_target(tmp_path, original)
    store = saves.SaveBackups(tmp_path / "data")
    plan = saves.prepare_write(saves.read_slot(target), ())

    def partial_replace(path: Path, _pending: Path, displaced: Path):
        path.rename(displaced)
        raise OSError("simulated interrupted replacement")

    monkeypatch.setattr(saves, "replace_with_displaced", partial_replace)
    with pytest.raises(OSError, match="原始备份"):
        saves.commit(plan, store)
    assert target.read_bytes() == original
    assert len(store.list(target)[0]) == 1
    assert not list(target.parent.glob(".darksort-*"))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows save dialog and transaction")
def test_save_dialog_applies_preview_to_selected_copy_and_records_history(qtbot, tmp_path, monkeypatch):
    from darksort.history import ProfileHistory
    from darksort.profiles import ProfileStore
    from darksort.ui import save_dialog

    monkeypatch.setattr(saves, "require_game_stopped", lambda: None)
    monkeypatch.setattr(save_dialog, "discover_slots", lambda _: [])
    monkeypatch.setattr(save_dialog, "confirmation_dialog", lambda *_: True)
    monkeypatch.setattr(save_dialog.QMessageBox, "information", lambda *_: None)
    game = tmp_path / "game"
    folder = game / "mods" / "content"
    folder.mkdir(parents=True)
    (folder / "project.xml").write_text("<project><Title>本地标题</Title></project>", encoding="utf-8")
    mod = parse_mod(folder, Source.LOCAL)
    data_dir = tmp_path / "data"
    profile = ProfileStore(data_dir).new("应用测试", [ProfileEntry.from_mod(mod)])
    target = make_target(tmp_path, make_save())
    resolved = resolve_entries(profile.entries, [mod])
    dialog = save_dialog.SaveDialog(data_dir, [], [game], [mod], resolved, profile=profile)
    qtbot.addWidget(dialog)
    dialog.slots.addItem("隔离档位", str(target))
    dialog.slots.setCurrentIndex(1)
    assert dialog.apply_button.isEnabled()
    assert "28385" in dialog.status.text()
    dialog.apply_button.click()
    assert saves.read_slot(target).mods == (LOCAL,)
    assert len(dialog.backups.list(target)[0]) == 1
    snapshots, errors = ProfileHistory(data_dir).list(profile.id)
    assert not errors and snapshots[0].profile.entries == profile.entries


def test_binding_displays_the_right_profile_and_keeps_slot_bytes(qtbot, tmp_path, monkeypatch):
    from darksort.profiles import ProfileStore
    from darksort.save_bindings import SaveBindings
    from darksort.ui import save_dialog

    monkeypatch.setattr(save_dialog, "discover_slots", lambda _: [])
    raw = make_save()
    target = make_target(tmp_path, raw)
    store = ProfileStore(tmp_path)
    current = store.new("当前方案")
    bound = store.new("远征方案", [ProfileEntry("工坊测试", Source.WORKSHOP, "", WORKSHOP.name)])
    dialog = save_dialog.SaveDialog(tmp_path, [], [], [], [], profile=bound, initial_path=target)
    qtbot.addWidget(dialog)
    dialog.bind_button.click()
    assert SaveBindings(tmp_path).get(target) == bound.id
    assert target.read_bytes() == raw

    other = save_dialog.SaveDialog(tmp_path, [], [], [], [], profile=current, initial_path=target)
    qtbot.addWidget(other)
    assert "远征方案" in other.binding_text.text()
    assert "工坊测试" in other.desired_view.toPlainText()
    assert "仅档位 1" in other.difference.text()
    assert not other.apply_button.isEnabled()
    other.switch_button.click()
    assert other.switch_to == bound.id
    assert target.read_bytes() == raw

    dialog.unbind_button.click()
    assert SaveBindings(tmp_path).get(target) is None


def test_refresh_discovers_new_slots_and_keeps_manually_opened_copy(qtbot, tmp_path, monkeypatch):
    from darksort.ui import save_dialog

    slots = []
    monkeypatch.setattr(save_dialog, "discover_slots", lambda _: slots.copy())
    (tmp_path / "manual").mkdir()
    (tmp_path / "steam").mkdir()
    manual = make_target(tmp_path / "manual", make_save())
    dialog = save_dialog.SaveDialog(tmp_path / "data", [], [], [], [], initial_path=manual)
    qtbot.addWidget(dialog)
    new = make_target(tmp_path / "steam", make_save(mods=()))
    slots.append(saves.Slot("新档", new.resolve()))
    dialog.refresh_slots()
    assert dialog.slots.currentData() == str(manual.resolve())
    assert dialog.slots.findData(str(new.resolve())) > 0
    dialog.select_path(new)
    assert not dialog.state.mods
