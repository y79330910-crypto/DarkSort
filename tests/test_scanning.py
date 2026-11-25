import json
import sqlite3

import pytest
import vdf

from darksort.models import ProfileEntry, ScanRoot, Source
from darksort.profiles import ProfileStore
from darksort.scanning.discovery import discover
from darksort.scanning.metadata import parse_mod
from darksort.scanning.scanner import scan
from darksort.settings import Settings


def project(folder, text):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "project.xml").write_text(text, encoding="utf-8")


def test_real_xml_shape_and_author_paths_are_not_install_paths(tmp_path):
    folder = tmp_path / "123456"
    project(
        folder,
        """<project><Title>中文英雄</Title><ItemDescription>介绍 [b]文字[/b]</ItemDescription>
      <ModDataPath>C:/AuthorsComputer/missing</ModDataPath>
      <VersionMajor>0</VersionMajor><VersionMinor>0</VersionMinor>
      <PublishedFileId>123456</PublishedFileId><PreviewIconFile>preview_icon.png</PreviewIconFile>
      <Tags><Tags>英雄</Tags><Tags/><Tags>英雄</Tags><Tags>皮肤</Tags></Tags></project>""",
    )
    (folder / "preview_icon.png").write_bytes(b"not decoded by scanner")
    mod = parse_mod(folder, Source.WORKSHOP)
    assert mod.path == str(folder.resolve())
    assert mod.name == "中文英雄"
    assert mod.version == "0.0"
    assert mod.tags == ("英雄", "皮肤")
    assert mod.workshop_id == "123456"
    assert mod.preview_path == str(folder / "preview_icon.png")
    assert mod.warnings == ()


@pytest.mark.parametrize(
    "xml",
    [
        None,
        "<project><broken>",
        '<!DOCTYPE x [<!ENTITY x SYSTEM "file:///secret">]><project><Title>&x;</Title></project>',
    ],
)
def test_old_broken_or_entity_xml_is_visible_and_warned(tmp_path, xml):
    folder = tmp_path / "older_mod"
    folder.mkdir()
    if xml:
        project(folder, xml)
    mod = parse_mod(folder, Source.LOCAL)
    assert mod.name == "older_mod"
    assert mod.warnings


@pytest.mark.parametrize("metadata", ["", "<PublishedFileId/>", "<PublishedFileId>  </PublishedFileId>"])
def test_local_mod_without_workshop_id_can_be_saved(tmp_path, metadata):
    project(tmp_path, f"<project><Title>Local</Title>{metadata}</project>")
    mod = parse_mod(tmp_path, Source.LOCAL)
    assert not mod.warnings
    assert mod.workshop_id is None
    store = ProfileStore(tmp_path / "data")
    profile = store.new("Local", [ProfileEntry.from_mod(mod)])
    assert store.load(profile.id).entries[0].workshop_id is None


def test_preview_cannot_escape_mod_folder(tmp_path):
    folder = tmp_path / "mod"
    project(
        folder,
        "<project><Title>Example</Title><PreviewIconFile>../outside.png</PreviewIconFile></project>",
    )
    (tmp_path / "outside.png").write_bytes(b"image")
    mod = parse_mod(folder, Source.LOCAL)
    assert mod.preview_path is None
    assert any("超出" in warning for warning in mod.warnings)


def test_workshop_install_id_wins_over_stale_xml(tmp_path):
    folder = tmp_path / "555"
    project(folder, "<project><Title>Copy</Title><PublishedFileId>444</PublishedFileId></project>")
    mod = parse_mod(folder, Source.WORKSHOP)
    assert mod.workshop_id == "555"
    assert any("不一致" in warning for warning in mod.warnings)


def test_multiple_steam_libraries_and_legacy_vdf(tmp_path):
    steam, lib_a, lib_b = [tmp_path / name for name in ("Steam", "库 A", "库 B")]
    (steam / "steamapps").mkdir(parents=True)
    (steam / "steamapps/libraryfolders.vdf").write_text(
        vdf.dumps({"libraryfolders": {"1": {"path": lib_a.as_posix()}, "2": lib_b.as_posix()}}),
        encoding="utf-8",
    )
    game = lib_a / "steamapps/common/DarkestDungeon"
    game.mkdir(parents=True)
    (lib_a / "steamapps/appmanifest_262060.acf").write_text(
        vdf.dumps({"AppState": {"appid": "262060", "installdir": "DarkestDungeon"}}),
        encoding="utf-8",
    )
    workshop = lib_b / "steamapps/workshop/content/262060"
    workshop.mkdir(parents=True)
    discovery = discover(Settings(), steam_roots=[steam])
    assert discovery.games == [game]
    assert ScanRoot(workshop, Source.WORKSHOP) in discovery.roots
    assert ScanRoot(game / "mods", Source.LOCAL) in discovery.roots


def test_manual_roots_work_without_steam_and_missing_roots_warn(tmp_path):
    root = tmp_path / "My mods"
    project(root / "legacy", "<project><Title>Manual</Title></project>")
    settings = Settings(auto_detect=False, local_paths=[str(root), str(tmp_path / "missing")])
    found = discover(settings, steam_roots=[])
    result = scan(found.roots)
    assert [mod.name for mod in result.mods] == ["Manual"]
    assert len(result.issues) == 1


def test_cache_hits_invalidation_refresh_and_deleted_mod(tmp_path):
    root = tmp_path / "mods"
    folder = root / "one"
    project(folder, "<project><Title>Before</Title></project>")
    roots = [ScanRoot(root, Source.LOCAL)]
    cache = tmp_path / "cache.sqlite3"
    assert scan(roots, cache).cache_hits == 0
    assert scan(roots, cache).cache_hits == 1
    project(folder, "<project><Title>After a change</Title></project>")
    result = scan(roots, cache)
    assert result.cache_hits == 0 and result.mods[0].name == "After a change"
    assert scan(roots, cache, refresh=True).cache_hits == 0
    (folder / "project.xml").unlink()
    folder.rmdir()
    assert scan(roots, cache).mods == []


def test_broken_sqlite_does_not_prevent_scan(tmp_path):
    root = tmp_path / "mods"
    project(root / "one", "<project><Title>Recovered scan</Title></project>")
    cache = tmp_path / "broken.sqlite3"
    cache.write_bytes(b"corrupt database")
    result = scan([ScanRoot(root, Source.LOCAL)], cache)
    assert result.mods[0].name == "Recovered scan"
    assert result.issues


def test_cancel_stops_before_metadata_and_preserves_disk(tmp_path):
    root = tmp_path / "mods"
    project(root / "one", "<project><Title>Original</Title></project>")
    before = (root / "one/project.xml").read_bytes()
    result = scan([ScanRoot(root, Source.LOCAL)], cancelled=lambda: True)
    assert result.cancelled
    assert result.mods == []
    assert (root / "one/project.xml").read_bytes() == before


def test_invalid_cached_payload_falls_back_to_metadata(tmp_path):
    root = tmp_path / "mods"
    project(root / "one", "<project><Title>Safe</Title></project>")
    cache = tmp_path / "cache.sqlite3"
    scan([ScanRoot(root, Source.LOCAL)], cache)
    with sqlite3.connect(cache) as db:
        db.execute("UPDATE mod_cache SET payload='[]'")
    result = scan([ScanRoot(root, Source.LOCAL)], cache)
    assert result.mods[0].name == "Safe"
    assert not result.cache_hits


def test_legacy_cached_empty_workshop_id_is_rebuilt(tmp_path):
    root = tmp_path / "mods"
    project(root / "one", "<project><Title>Local</Title><PublishedFileId/></project>")
    roots = [ScanRoot(root, Source.LOCAL)]
    cache = tmp_path / "cache.sqlite3"
    mod = scan(roots, cache).mods[0]
    old_payload = mod.to_dict() | {"workshop_id": ""}
    with sqlite3.connect(cache) as db:
        db.execute("UPDATE mod_cache SET payload=?", (json.dumps(old_payload),))

    result = scan(roots, cache)
    assert result.cache_hits == 0
    assert result.mods[0].workshop_id is None
    cached = scan(roots, cache)
    assert cached.cache_hits == 1
    assert cached.mods[0].workshop_id is None
