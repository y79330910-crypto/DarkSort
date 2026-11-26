from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from pathlib import Path

import vdf

from darksort.scanning.discovery import APP_ID, steam_locations


@dataclass(frozen=True)
class LaunchArguments:
    arguments: tuple[str, ...] = ()
    replaced_profile: bool = False


def split_windows_arguments(text: str) -> tuple[str, ...]:
    if os.name != "nt":
        raise OSError("启动参数解析适用于 Windows。")
    from ctypes import wintypes

    parse = ctypes.WinDLL("shell32", use_last_error=True).CommandLineToArgvW
    parse.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int))
    parse.restype = ctypes.POINTER(wintypes.LPWSTR)
    release = ctypes.WinDLL("kernel32").LocalFree
    release.argtypes = (wintypes.HLOCAL,)
    release.restype = wintypes.HLOCAL
    count = ctypes.c_int()
    arguments = parse("Darkest.exe " + text, ctypes.byref(count))
    if not arguments:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return tuple(arguments[index] for index in range(1, count.value))
    finally:
        release(ctypes.cast(arguments, wintypes.HLOCAL))


def parse_launch_options(text: str) -> LaunchArguments:
    if "\0" in text:
        raise ValueError("启动项包含无效字符。")
    if "%command%" in text.casefold():
        raise ValueError("含 %command% 的命令由 Steam 解释，请在手动输入里填写游戏本身的启动参数。")
    if not text.strip():
        return LaunchArguments()
    arguments = iter(split_windows_arguments(text.strip()))
    output = []
    replaced = False
    token = next(arguments, None)
    while token is not None:
        option = token.casefold()
        if option == "-mod_profile":
            replaced = True
            token = next(arguments, None)
            if token is not None and not token.startswith(("-", "+")):
                token = next(arguments, None)
            continue
        if option.startswith("-mod_profile="):
            replaced = True
        else:
            output.append(token)
        token = next(arguments, None)
    return LaunchArguments(tuple(output), replaced)


def active_steam_account() -> tuple[int, Path | None]:
    if os.name != "nt":
        raise OSError("读取 Steam 启动项需要 Windows Steam 客户端。")
    import winreg

    client = None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam\ActiveProcess") as key:
            account, _ = winreg.QueryValueEx(key, "ActiveUser")
            for name in ("SteamClientDll64", "SteamClientDll"):
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                except OSError:
                    continue
                if isinstance(value, str) and value:
                    root = Path(value).resolve().parent
                    if (root / "steam.exe").is_file():
                        client = root
                        break
    except OSError as exc:
        raise OSError("未识别到当前登录的 Steam 账号。") from exc
    if type(account) is not int or account <= 0:
        raise OSError("未识别到当前登录的 Steam 账号。")
    return account, client


def read_steam_launch_options(configured_roots=()) -> str:
    account, active_root = active_steam_account()
    relative = Path("userdata", str(account), "config", "localconfig.vdf")
    if active_root:
        paths = [active_root / relative]
    else:
        roots = dict.fromkeys(
            Path(path).expanduser().resolve() for path in (*steam_locations(), *configured_roots) if str(path).strip()
        )
        paths = [root / relative for root in roots if (root / "steam.exe").is_file()]
    available = [path for path in paths if path.is_file()]
    if len(available) != 1:
        raise OSError("无法确定当前 Steam 账号的本地启动项。")
    try:
        with available[0].open(encoding="utf-8-sig") as stream:
            data = vdf.load(stream)
    except (OSError, ValueError, SyntaxError) as exc:
        raise OSError("无法读取当前 Steam 账号的本地启动项。") from exc
    for key in ("UserLocalConfigStore", "Software", "Valve", "Steam", "apps", APP_ID, "LaunchOptions"):
        if not isinstance(data, dict):
            return ""
        data = next((value for name, value in data.items() if name.casefold() == key.casefold()), None)
    return data if isinstance(data, str) else ""
