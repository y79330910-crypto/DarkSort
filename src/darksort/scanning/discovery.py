from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import vdf

from darksort.models import ScanIssue, ScanRoot, Source, path_key
from darksort.settings import Settings

APP_ID = "262060"


@dataclass
class Discovery:
    games: list[Path] = field(default_factory=list)
    libraries: list[Path] = field(default_factory=list)
    roots: list[ScanRoot] = field(default_factory=list)
    issues: list[ScanIssue] = field(default_factory=list)


def steam_locations() -> list[Path]:
    locations: list[Path] = []
    if os.name == "nt":
        import winreg

        for hive, key_name, value_name in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        ):
            try:
                with winreg.OpenKey(hive, key_name) as key:
                    value, _ = winreg.QueryValueEx(key, value_name)
                    locations.append(Path(value))
            except OSError:
                pass
    program_files = os.environ.get("ProgramFiles(x86)")
    if program_files:
        locations.append(Path(program_files) / "Steam")
    return _unique_paths(locations)


def _unique_paths(paths) -> list[Path]:
    result, seen = [], set()
    for path in paths:
        path = Path(path).expanduser().resolve()
        key = path_key(path)
        if key not in seen:
            result.append(path)
            seen.add(key)
    return result


def _read_vdf(path: Path) -> dict:
    with path.open(encoding="utf-8-sig", errors="replace") as file:
        return vdf.load(file)


def discover(settings: Settings, steam_roots: list[Path] | None = None) -> Discovery:
    result = Discovery()
    steam = []
    if settings.auto_detect:
        steam = steam_locations() if steam_roots is None else list(steam_roots)
    steam += [Path(p) for p in settings.steam_paths if p.strip()]
    libraries = []
    for root in _unique_paths(steam):
        libraries.append(root)
        file = root / "steamapps" / "libraryfolders.vdf"
        if file.exists():
            try:
                data = _read_vdf(file)
                folders = data.get("libraryfolders", data.get("LibraryFolders", {}))
                for key, item in folders.items():
                    if str(key).isdigit():
                        value = item.get("path") if isinstance(item, dict) else item
                        if isinstance(value, str) and value:
                            libraries.append(Path(value))
            except (OSError, ValueError, SyntaxError, AttributeError, TypeError) as exc:
                result.issues.append(ScanIssue(str(file), f"Steam 库解析失败：{exc}"))
    result.libraries = _unique_paths(libraries)
    games = [Path(settings.game_path)] if settings.game_path.strip() else []
    for library in result.libraries:
        manifest = library / "steamapps" / f"appmanifest_{APP_ID}.acf"
        if manifest.exists():
            try:
                app = _read_vdf(manifest).get("AppState", {})
                folder = app.get("installdir", "DarkestDungeon")
                # A manifest is data, never an authority to escape common/.
                common = (library / "steamapps" / "common").resolve()
                candidate = (common / folder).resolve()
                if not candidate.is_relative_to(common):
                    raise ValueError("installdir 超出 Steam 游戏目录")
                if candidate.is_dir():
                    games.append(candidate)
            except (OSError, ValueError, SyntaxError, AttributeError, TypeError) as exc:
                result.issues.append(ScanIssue(str(manifest), f"游戏清单解析失败：{exc}"))
        else:
            fallback = library / "steamapps" / "common" / "DarkestDungeon"
            if fallback.is_dir():
                games.append(fallback)
    result.games = _unique_paths(games)
    roots = [ScanRoot(Path(p), Source.WORKSHOP) for p in settings.workshop_paths if p.strip()]
    roots += [ScanRoot(Path(p), Source.LOCAL) for p in settings.local_paths if p.strip()]
    # Manual Workshop/local paths override the automatically discovered paths of that type.
    if not settings.workshop_paths and settings.auto_detect:
        roots += [
            ScanRoot(library / "steamapps" / "workshop" / "content" / APP_ID, Source.WORKSHOP)
            for library in result.libraries
            if (library / "steamapps" / "workshop" / "content" / APP_ID).is_dir()
        ]
    if not settings.local_paths:
        roots += [ScanRoot(game / "mods", Source.LOCAL) for game in result.games]
    seen = set()
    for root in roots:
        key = (path_key(root.path), root.source)
        if key not in seen:
            result.roots.append(ScanRoot(root.path.expanduser().resolve(), root.source))
            seen.add(key)
    return result
