import os
import sys
from types import SimpleNamespace

import pytest

from darksort.adapters import windows_launch
from darksort.ui import workshop


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shell handoff")
def test_frozen_workshop_launch_restores_environment_between_fallbacks(tmp_path, monkeypatch):
    bundle = tmp_path / "_internal"
    system = tmp_path / "system"
    original_path = os.pathsep.join((f'"{bundle / "Qt"}"', str(system)))
    monkeypatch.setenv("PATH", original_path)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    dll_paths, requests = [], []

    def set_dll_directory(path):
        dll_paths.append(path)
        return True

    monkeypatch.setattr(windows_launch.ctypes, "WinDLL", lambda *a, **k: SimpleNamespace(
        SetDllDirectoryW=set_dll_directory,
    ))
    monkeypatch.setattr(workshop, "steam_protocol_registered", lambda: True)

    def shell_open(url, verb):
        assert os.environ["PATH"] == str(system) and dll_paths[-1] is None
        requests.append((url, verb))
        if url.startswith("steam:"):
            raise OSError("Steam scheme unavailable")

    monkeypatch.setattr(workshop.os, "startfile", shell_open)
    assert workshop.open_workshop_page("12345") == "浏览器"
    assert requests == [
        ("steam://url/CommunityFilePage/12345", "open"),
        ("https://steamcommunity.com/sharedfiles/filedetails/?id=12345", "open"),
    ]
    assert dll_paths == [None, str(bundle), None, str(bundle)]
    assert os.environ["PATH"] == original_path
