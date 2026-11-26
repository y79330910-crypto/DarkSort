from pathlib import Path
from zipfile import ZipFile

import pytest

from darksort import file_replacements as replacements
from darksort.adapters.dson import SavedMod
from darksort.adapters.saves import mods_for_slot
from darksort.file_replacements import FilePackStore, ModOrigin
from darksort.models import ProfileEntry, ResolvedEntry, Source
from darksort.resource_import import inspect_import
from darksort.scanning.metadata import parse_mod

TITLE = "fe_flow/title_bg.png"
DLC = "dlc/580100_crimson_court/localization/zh.loc2"


def write(root, relative, data):
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


@pytest.fixture
def installation(tmp_path, monkeypatch):
    monkeypatch.setattr(replacements, "require_game_stopped", lambda: None)
    game = tmp_path / "暗黑地牢"
    write(game, "_windows/win64/Darkest.exe", b"test only")
    write(game, TITLE, b"original title")
    return game


def test_zip_layout_preview_enable_and_restore(installation, tmp_path):
    archive = tmp_path / "patch.zip"
    with ZipFile(archive, "w") as zip_file:
        zip_file.writestr(f"DarkestDungeon/{TITLE}", b"alternate title")
        zip_file.writestr(f"DarkestDungeon/{DLC}", b"translated DLC")
        zip_file.writestr(f"Optional/{TITLE}", b"optional title")
        zip_file.writestr("DarkestDungeon/project.xml", b"metadata")
        zip_file.writestr("DarkestDungeon/loader.dll", b"not a resource")
        zip_file.writestr(f"__MACOSX/DarkestDungeon/{TITLE}", b"metadata")
    source = inspect_import((archive,))
    assert [layout.prefix for layout in source.layouts] == ["DarkestDungeon", "Optional"]
    layout = source.layouts[0]
    assert replacements.import_preview(installation, (path for path, _ in layout.targets())) == (
        (DLC, "新增"), (TITLE, "替换"),
    )

    store = FilePackStore(tmp_path / "app")
    pack = store.import_pack("标题及汉化", installation, source, layout, "")
    assert store.list() == ([pack], [])
    assert (installation / TITLE).read_bytes() == b"original title"
    assert not (installation / DLC).exists()
    store.enable(pack, store.preview(pack))
    assert store.state(pack).phase == "enabled"
    assert (installation / TITLE).read_bytes() == b"alternate title"
    assert (installation / DLC).read_bytes() == b"translated DLC"
    assert not (installation / "loader.dll").exists()

    store.disable(pack)
    assert store.state(pack) is None
    assert (installation / TITLE).read_bytes() == b"original title"
    assert not (installation / DLC).exists()
    store.remove(pack)
    assert store.list() == ([], []) and archive.exists()


def test_restore_detects_external_changes_and_rejects_overlapping_deployments(installation, tmp_path):
    first = FilePackStore(tmp_path / "first")
    second = FilePackStore(tmp_path / "second")
    title = write(tmp_path / "loose", "title_bg.png", b"replacement")
    source = inspect_import((title,))
    layout = source.layouts[0]
    assert layout.manual
    with pytest.raises(ValueError, match="请选择"):
        layout.targets()
    pack = first.import_pack("first", installation, source, layout, "fe_flow")
    overlap = second.import_pack("overlap", installation, source, layout, "fe_flow")
    preview = first.preview(pack)
    write(installation, TITLE, b"changed after preview")
    with pytest.raises(ValueError, match="与预览不同"):
        first.enable(pack, preview)

    first.enable(pack, first.preview(pack))
    with pytest.raises(ValueError, match="已有启用"):
        second.enable(overlap, second.preview(overlap))
    write(installation, TITLE, b"external change")
    with pytest.raises(ValueError, match="外部修改"):
        first.disable(pack)
    assert (installation / TITLE).read_bytes() == b"external change"
    assert first.state(pack).phase == "enabled"
    write(installation, TITLE, b"replacement")
    first.disable(pack)
    assert (installation / TITLE).read_bytes() == b"changed after preview"


def test_partial_enable_rolls_back(installation, tmp_path, monkeypatch):
    folder = tmp_path / "patch"
    write(folder, DLC, b"translated DLC")
    write(folder, TITLE, b"alternate title")
    source = inspect_import((folder,))
    store = FilePackStore(tmp_path / "app")
    pack = store.import_pack("patch", installation, source, source.layouts[0], "")
    switch = replacements.switch_file

    def fail_on_title(target, before, content, after):
        if target == installation / TITLE:
            raise OSError("simulated failure")
        return switch(target, before, content, after)

    monkeypatch.setattr(replacements, "switch_file", fail_on_title)
    with pytest.raises(OSError, match="simulated failure"):
        store.enable(pack, store.preview(pack))
    assert (installation / TITLE).read_bytes() == b"original title"
    assert not (installation / DLC).exists()
    assert store.state(pack) is None


def test_converted_local_mod_cannot_apply_twice_and_source_update_is_separate(installation, tmp_path):
    folder = installation / "mods/local_copy"
    write(folder, "project.xml", b"<project><Title>Local copy</Title></project>")
    title = write(folder, TITLE, b"first snapshot")
    mod = parse_mod(folder, Source.LOCAL)
    origin = ModOrigin(mod.path, mod.key, mod.reference, "123456")
    plan = [ResolvedEntry(ProfileEntry.from_mod(mod), mod)]
    source = inspect_import((folder,))
    store = FilePackStore(tmp_path / "app")
    pack = store.import_pack("copy", installation, source, source.layouts[0], "", origin=origin)
    assert store.load(pack.id) == pack
    assert pack.origin.workshop_id == "123456"
    assert mods_for_slot(plan, [mod], [installation]) == (SavedMod("Local copy", "mod_local_source"),)
    assert not store.has_update(pack)
    title.write_bytes(b"second and longer snapshot")
    assert store.has_update(pack)

    store.enable(pack, store.preview(pack))
    assert (installation / TITLE).read_bytes() == b"first snapshot"
    with pytest.raises(ValueError, match="游戏文件替换"):
        mods_for_slot(plan, [mod], [installation])
    store.disable(pack)
    source = inspect_import((Path(pack.origin.path),))
    updated = store.import_pack(pack.name, installation, source, source.layouts[0], "", origin=origin, previous=pack)
    assert updated.id == pack.id and updated.revision != pack.revision
    assert not store.has_update(updated)
    store.enable(updated, store.preview(updated))
    assert (installation / TITLE).read_bytes() == b"second and longer snapshot"
    store.disable(updated)
    assert title.read_bytes() == b"second and longer snapshot"


@pytest.mark.parametrize("relative", ["../fe_flow/title_bg.png", "fe_flow/CON.png", "fe_flow/title_bg.png:stream"])
def test_archive_paths_outside_game_resources_are_rejected(tmp_path, relative):
    archive = tmp_path / "patch.zip"
    with ZipFile(archive, "w") as zip_file:
        zip_file.writestr(relative, b"example")
    with pytest.raises(ValueError, match="路径无效"):
        inspect_import((archive,))
