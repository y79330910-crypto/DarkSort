from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from darksort.adapters.dson import SavedMod
from darksort.adapters.saves import mods_for_slot, require_game_stopped
from darksort.adapters.windows_launch import external_program_environment
from darksort.models import Mod, ResolvedEntry, Source
from darksort.scanning.discovery import APP_ID
from darksort.storage import atomic_json

EXECUTABLES = (
    ("_windows/win64/Darkest.exe", True),
    ("_windows/win32/Darkest.exe", True),
    ("_windowsnosteam/win64/Darkest.exe", False),
    ("_windowsnosteam/win32/Darkest.exe", False),
)
MAX_EXECUTABLE_BYTES = 64 * 1024 * 1024
LOADOUT_MARKERS = (b"\0mod_profile\0", b"-mod_profile: applied %u mods from", b"'mods' array")


@dataclass(frozen=True)
class LaunchTarget:
    root: Path
    executable: Path
    steam: bool

    @property
    def label(self) -> str:
        platform = "Steam" if self.steam else "本地"
        return f"{platform} · {self.root}"


def supports_launch_profile(executable: Path) -> bool:
    try:
        with executable.open("rb") as stream:
            data = stream.read(MAX_EXECUTABLE_BYTES + 1)
    except OSError:
        return False
    return len(data) <= MAX_EXECUTABLE_BYTES and data.startswith(b"MZ") and all(
        marker in data for marker in LOADOUT_MARKERS
    )


def discover_launch_targets(game_roots: list[Path]) -> list[LaunchTarget]:
    targets = []
    for root in dict.fromkeys(path.resolve() for path in game_roots):
        for relative, steam in EXECUTABLES:
            executable = (root / relative).resolve()
            if executable.is_relative_to(root) and supports_launch_profile(executable):
                targets.append(LaunchTarget(root, executable, steam))
                break
    return targets


def validate_steam_installation(target: LaunchTarget) -> None:
    # The official executable uses SteamAPI_Init and reads the supplied app id
    # from its working directory when started directly.
    try:
        app_id = (target.root / "steam_appid.txt").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        app_id = ""
    if app_id != APP_ID:
        raise ValueError("此 Steam 安装缺少游戏自带的应用标识。可通过 Steam 验证游戏文件后重试。")


def write_launch_profile(data_dir: Path, root: Path, mods: tuple[SavedMod, ...]) -> tuple[Path, str]:
    name = f"{uuid4().hex}.json"
    target = data_dir.resolve() / "launch-loadouts" / name
    argument = str(target)
    if not argument.isascii() or len(argument) >= 512:
        # The native loader uses narrow fopen on UTF-8 arguments on Windows.
        # An ASCII path relative to the game's working directory works even
        # when the user's folders or the Steam library contain Chinese.
        directory = root / ".darksort-launches"
        if directory.resolve() != directory:
            raise ValueError("游戏目录中的 DarkSort 启动清单目录指向了其他位置。")
        target = directory / name
        argument = str(Path(directory.name) / name)
    atomic_json(target, {
        "version": 1,
        "mods": [{"name": mod.name, "source": mod.source} for mod in mods],
    })
    return target, argument


def start_game(target: LaunchTarget, profile_argument: str) -> subprocess.Popen:
    arguments = [str(target.executable), "-mod_profile", profile_argument]
    with external_program_environment() as env:
        if env is None:
            return subprocess.Popen(arguments, cwd=target.root)
        return subprocess.Popen(arguments, cwd=target.root, env=env)


def launch_campaign(
    target: LaunchTarget, data_dir: Path, resolved: list[ResolvedEntry], installed: list[Mod],
) -> subprocess.Popen:
    require_game_stopped(require_steam=target.steam)
    if not supports_launch_profile(target.executable):
        raise ValueError("游戏文件已变化，请重新扫描支持方案启动的版本。")
    if target.steam:
        validate_steam_installation(target)
    elif any(row.entry.source is Source.WORKSHOP for row in resolved):
        raise ValueError("计划中有创意工坊 Mod，请选择 Steam 版游戏。")
    mods = mods_for_slot(resolved, installed, [target.root])
    file, argument = write_launch_profile(data_dir, target.root, mods)
    try:
        require_game_stopped(require_steam=target.steam)
        return start_game(target, argument)
    except (OSError, subprocess.SubprocessError):
        file.unlink(missing_ok=True)
        raise
