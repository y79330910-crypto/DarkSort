import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QDialog

from darksort.adapters import campaigns
from darksort.models import ProfileEntry, Source
from darksort.profiles import resolve_entries
from darksort.scanning.metadata import parse_mod
from darksort.ui import campaign_dialog


def install_game(game, *, steam=False):
    relative = "_windows" if steam else "_windowsnosteam"
    executable = game / relative / "win64/Darkest.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"MZ" + b"\x00".join(campaigns.LOADOUT_MARKERS))
    if steam:
        (game / "steam_appid.txt").write_text("262060\n", encoding="ascii")
    return executable


def install_local(game, title):
    directory = game / "mods" / title
    directory.mkdir(parents=True)
    (directory / "project.xml").write_text(f"<project><Title>{title}</Title></project>", encoding="utf-8")
    return parse_mod(directory, Source.LOCAL)


def test_uses_supported_native_executable_and_order_in_unicode_paths(tmp_path, monkeypatch):
    game = tmp_path / "游戏"
    executable = install_game(game, steam=True)
    a, b = [install_local(game, title) for title in ("测试甲", "测试乙")]
    entries = [ProfileEntry.from_mod(mod) for mod in (b, a)]
    rows = resolve_entries(entries, [a, b])
    [target] = campaigns.discover_launch_targets([game, game])
    checks, calls = [], []
    process = SimpleNamespace(pid=2222)
    monkeypatch.setattr(campaigns, "require_game_stopped", lambda **kw: checks.append(kw))

    def spawn(args, *, cwd):
        calls.append((args, cwd))
        return process

    monkeypatch.setattr(campaigns.subprocess, "Popen", spawn)
    result = campaigns.launch_campaign(target, tmp_path / "用户目录", rows, [a, b])
    assert result is process
    assert checks == [{"require_steam": True}, {"require_steam": True}]
    [(args, cwd)] = calls
    assert args[:2] == [str(executable), "-mod_profile"]
    assert cwd == game
    argument = Path(args[2])
    assert args[2].isascii() and not argument.is_absolute()
    profile = json.loads((game / argument).read_text(encoding="utf-8"))
    assert profile == {
        "version": 1,
        "mods": [{"name": title, "source": "mod_local_source"} for title in ("测试乙", "测试甲")],
    }
    assert not list(game.rglob("persist.game.json"))


def test_running_game_prevents_writing_the_launch_file(tmp_path, monkeypatch):
    game = tmp_path / "游戏"
    install_game(game)
    [target] = campaigns.discover_launch_targets([game])

    def blocked(**_):
        raise OSError("游戏正在运行")

    monkeypatch.setattr(campaigns, "require_game_stopped", blocked)
    with pytest.raises(OSError, match="游戏正在运行"):
        campaigns.launch_campaign(target, tmp_path / "用户目录", [], [])
    assert not (game / ".darksort-launches").exists()


def test_workshop_mods_require_the_steam_executable(tmp_path, mods, monkeypatch):
    game = tmp_path / "Game"
    install_game(game)
    [target] = campaigns.discover_launch_targets([game])
    resolved = resolve_entries([ProfileEntry.from_mod(mods[0])], mods)
    monkeypatch.setattr(campaigns, "require_game_stopped", lambda **_: None)
    with pytest.raises(ValueError, match="Steam 版"):
        campaigns.launch_campaign(target, tmp_path / "data", resolved, mods)


def test_start_failure_removes_only_the_unused_launch_file(tmp_path, monkeypatch):
    game = tmp_path / "游戏"
    install_game(game)
    [target] = campaigns.discover_launch_targets([game])
    directory = game / ".darksort-launches"
    directory.mkdir()
    previous = directory / "older.json"
    previous.write_text("{}", encoding="utf-8")

    def fail(*_):
        raise OSError("启动失败")

    monkeypatch.setattr(campaigns, "require_game_stopped", lambda **_: None)
    monkeypatch.setattr(campaigns, "start_game", fail)
    with pytest.raises(OSError, match="启动失败"):
        campaigns.launch_campaign(target, tmp_path / "用户目录", [], [])
    assert list(directory.iterdir()) == [previous]


def test_campaign_dialog_starts_the_previewed_game(qtbot, tmp_path, monkeypatch):
    game = tmp_path / "Game"
    install_game(game)
    [target] = campaigns.discover_launch_targets([game])
    calls = []
    process = SimpleNamespace(pid=10)

    def spawn(*args):
        calls.append(args)
        return process

    monkeypatch.setattr(campaign_dialog, "launch_campaign", spawn)
    dialog = campaign_dialog.CampaignDialog(tmp_path, [target], "测试配置", [], [])
    qtbot.addWidget(dialog)
    dialog.launch_button.click()
    assert calls == [(target, tmp_path, [], [])]
    assert dialog.process is process and dialog.result() == QDialog.DialogCode.Accepted
