import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import vdf
from PySide6.QtWidgets import QDialog

from darksort.adapters import campaigns, launch_options
from darksort.models import ProfileEntry, Source, path_key
from darksort.profiles import resolve_entries
from darksort.scanning.metadata import parse_mod
from darksort.settings import Settings
from darksort.ui import campaign_dialog
from darksort.ui.main_window import MainWindow


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

    def spawn(*args, rules=(), launch_options=""):
        calls.append((*args, rules, launch_options))
        return process

    monkeypatch.setattr(campaign_dialog, "launch_campaign", spawn)
    dialog = campaign_dialog.CampaignDialog(tmp_path, [target], "测试配置", [], [])
    qtbot.addWidget(dialog)
    dialog.launch_button.click()
    assert calls == [(target, tmp_path, [], [], (), "")]
    assert dialog.process is process and dialog.result() == QDialog.DialogCode.Accepted


@pytest.mark.skipif(os.name != "nt", reason="Windows command-line parsing")
def test_extra_options_preserve_quoted_values_and_current_profile_wins(tmp_path, monkeypatch):
    game = tmp_path / "游戏"
    executable = install_game(game)
    [target] = campaigns.discover_launch_targets([game])
    calls = []
    monkeypatch.setattr(campaigns, "require_game_stopped", lambda **_: None)
    monkeypatch.setattr(campaigns.subprocess, "Popen", lambda args, *, cwd: calls.append((args, cwd)))
    text = r'-skipfeflowintro -enabledebuginput -label "带 空格" -MOD_PROFILE "旧 方案.json" -mod_profile=other -size 1920 1080'
    campaigns.launch_campaign(target, tmp_path / "用户目录", [], [], launch_options=text)
    [(argv, cwd)] = calls
    assert argv[:2] == [str(executable), "-mod_profile"] and cwd == game
    assert argv[2].isascii() and (cwd / argv[2]).is_file()
    assert argv[3:] == ["-skipfeflowintro", "-enabledebuginput", "-label", "带 空格", "-size", "1920", "1080"]


def test_steam_wrappers_get_reported_before_a_manifest_is_written(tmp_path):
    game = tmp_path / "游戏"
    install_game(game)
    [target] = campaigns.discover_launch_targets([game])
    with pytest.raises(ValueError, match="%command%"):
        campaigns.launch_campaign(target, tmp_path / "用户目录", [], [], launch_options='wrapper.exe %command%')
    assert not (game / ".darksort-launches").exists()


def test_reads_only_dd1_options_from_the_active_steam_account(tmp_path, monkeypatch):
    account = 123
    current = tmp_path / "current"
    stale = tmp_path / "stale"

    def write(root, user, text):
        file = root / "userdata" / str(user) / "config/localconfig.vdf"
        file.parent.mkdir(parents=True)
        (root / "steam.exe").touch()
        apps = {"1": {"LaunchOptions": "-other-app"}, "262060": {"launchoptions": text}}
        config = {"UserLocalConfigStore": {"Software": {"Valve": {"Steam": {"Apps": apps}}}}}
        file.write_text(vdf.dumps(config), encoding="utf-8")
        return file

    expected = r'-skipfeflowintro -label "路径 空格 C:\Data"'
    config = write(current, account, expected)
    write(current, 987, "-other-account")
    write(stale, account, "-stale-client")
    before = config.read_bytes()
    monkeypatch.setattr(launch_options, "active_steam_account", lambda: (account, current))
    monkeypatch.setattr(launch_options, "steam_locations", lambda: [stale, current])
    assert launch_options.read_steam_launch_options() == expected
    assert config.read_bytes() == before
    monkeypatch.setattr(launch_options, "active_steam_account", lambda: (account, None))
    with pytest.raises(OSError, match="无法确定"):
        launch_options.read_steam_launch_options()


@pytest.mark.skipif(os.name != "nt", reason="Windows command-line parsing")
def test_launch_window_previews_steam_and_remembers_drafts_for_each_install(qtbot, tmp_path, monkeypatch):
    steam = tmp_path / "SteamGame"
    local = tmp_path / "LocalGame"
    install_game(steam, steam=True)
    install_game(local)
    targets = campaigns.discover_launch_targets([steam, local])
    inherited = '-skipfeflowintro -mod_profile "old plan.json"'
    loaded = []
    monkeypatch.setattr(campaign_dialog, "read_steam_launch_options", lambda roots: loaded.append(roots) or inherited)
    starts = []
    monkeypatch.setattr(
        campaign_dialog, "launch_campaign",
        lambda *args, **kw: starts.append((args, kw)) or SimpleNamespace(pid=12),
    )
    previous = {path_key(local): {"mode": "manual", "manual": "-size 1920 1080"}}
    dialog = campaign_dialog.CampaignDialog(
        tmp_path, targets, "测试", [], [], launch_preferences=previous, steam_roots=[str(tmp_path)],
    )
    qtbot.addWidget(dialog)
    assert dialog.option_mode.currentData() == "steam"
    assert loaded == [(str(tmp_path),)]
    assert dialog.option_text.isReadOnly() and dialog.option_text.text() == inherited
    assert "本次使用当前方案" in dialog.option_preview.text()
    dialog.option_mode.setCurrentIndex(dialog.option_mode.findData("manual"))
    dialog.use_steam_button.click()
    assert dialog.option_text.text() == inherited
    user_text = r'-enabledebuginput -label "带 空格"'
    dialog.option_text.setText(user_text)
    dialog.game.setCurrentIndex(1)
    assert not dialog.option_mode.model().item(0).isEnabled()
    assert dialog.option_text.text() == "-size 1920 1080"
    dialog.game.setCurrentIndex(0)
    assert not dialog.option_text.isReadOnly() and dialog.option_text.text() == user_text
    dialog.launch_button.click()
    assert starts[0][1]["launch_options"] == user_text
    assert dialog.launch_preference == (path_key(steam), {"mode": "manual", "manual": user_text})
    assert previous == {path_key(local): {"mode": "manual", "manual": "-size 1920 1080"}}


@pytest.mark.skipif(os.name != "nt", reason="Windows command-line parsing")
def test_manual_launch_options_save_through_main_window_and_reload(qtbot, tmp_path, monkeypatch):
    game = tmp_path / "LocalGame"
    install_game(game)
    data_dir = tmp_path / "DarkSort"
    Settings(game_path=str(game)).save(data_dir)
    window = MainWindow(data_dir, auto_scan=False)
    qtbot.addWidget(window)
    starts = []
    monkeypatch.setattr(
        campaign_dialog, "launch_campaign",
        lambda *args, **kw: starts.append(kw) or SimpleNamespace(pid=12, poll=lambda: None),
    )

    def first_launch(dialog):
        dialog.option_text.setText("-enabledebuginput")
        dialog.launch_button.click()
        return dialog.result()

    monkeypatch.setattr(campaign_dialog.CampaignDialog, "exec", first_launch)
    window.open_new_campaign()
    assert starts[0]["launch_options"] == "-enabledebuginput"
    saved = Settings.load(data_dir)
    assert saved.campaign_launch_options[path_key(game)] == {"mode": "manual", "manual": "-enabledebuginput"}

    def reopen(dialog):
        assert dialog.option_text.text() == "-enabledebuginput"
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(campaign_dialog.CampaignDialog, "exec", reopen)
    window.open_new_campaign()
    assert len(starts) == 1
