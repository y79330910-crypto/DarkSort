from __future__ import annotations

import csv
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from platformdirs import user_documents_path

from darksort.adapters.dson import MAGIC, UGCS, DsonDocument, SavedMod, supported_mod
from darksort.models import Mod, ProfileEntry, ResolvedEntry, Source, path_key
from darksort.scanning.discovery import steam_locations
from darksort.scanning.metadata import parse_mod
from darksort.storage import atomic_bytes, atomic_json, read_json

SAVE_FILE = "persist.game.json"
MAX_SAVE_BYTES = 24 * 1024 * 1024


@dataclass(frozen=True)
class Slot:
    label: str
    path: Path


@dataclass(frozen=True)
class SaveState:
    path: Path
    raw: bytes
    mods: tuple[SavedMod, ...]
    revision: int | None
    read_only_reason: str

    @property
    def digest(self) -> str:
        return sha256(self.raw)


@dataclass(frozen=True)
class SavePlan:
    state: SaveState
    payload: bytes
    mods: tuple[SavedMod, ...]
    reason: str = "应用计划顺序"


@dataclass(frozen=True)
class Backup:
    directory: Path
    created: datetime
    target: Path
    before: str
    after: str
    reason: str


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def discover_slots(extra_steam: list[str]) -> list[Slot]:
    slots = {}
    steam_roots = {Path(path).resolve() for path in extra_steam} | set(steam_locations())
    for steam in sorted(steam_roots):
        for target in (steam / "userdata").glob(f"*/262060/remote/profile_*/{SAVE_FILE}"):
            account = target.parents[3].name
            slots[target.resolve()] = Slot(f"Steam {account} · {target.parent.name}", target.resolve())
    darkest = user_documents_path() / "Darkest"
    for pattern in (f"profile_*/{SAVE_FILE}", f"remote/profile_*/{SAVE_FILE}"):
        for target in darkest.glob(pattern):
            slots[target.resolve()] = Slot(f"本地 · {target.parent.name}", target.resolve())
    return list(slots.values())


def read_bounded(stream) -> bytes:
    data = stream.read(MAX_SAVE_BYTES + 1)
    if len(data) > MAX_SAVE_BYTES:
        raise ValueError("存档超过 24 MiB，无法安全处理。")
    return data


def unique_json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"存档 JSON 含有重复键 {key}。")
        result[key] = value
    return result


def read_json_mods(raw: bytes) -> tuple[SavedMod, ...]:
    value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_json_pairs)
    base = None
    if isinstance(value, dict):
        base = value.get("base_root")
        # The game can create a version-2 JSON save with just the campaign
        # metadata on first entering the Old Road.
        if base is None and value.get("version") == 2 and isinstance(data := value.get("data"), dict):
            if isinstance(data.get("inraid"), bool) and isinstance(data.get("estatename"), str):
                base = data
                if UGCS not in base:
                    return ()
    applied = base.get(UGCS) if isinstance(base, dict) else None
    if not isinstance(applied, dict):
        raise ValueError("找不到 DD1 存档的启用列表。")
    entries = {}
    for index, row in applied.items():
        if not index.isascii() or not index.isdecimal() or len(index) > 6:
            raise ValueError("存档的 Mod 序号无效。")
        index = int(index)
        if index in entries or not isinstance(row, dict) or not all(
            isinstance(row.get(key), str) for key in ("name", "source")
        ):
            raise ValueError("存档 Mod 条目重复或格式无效。")
        entries[index] = SavedMod(row["name"], row["source"])
    return tuple(entries[index] for index in sorted(entries))


def read_slot(path: Path) -> SaveState:
    path = path.resolve(strict=True)
    with path.open("rb") as stream:
        raw = read_bounded(stream)
    if not raw.startswith(MAGIC):
        return SaveState(path, raw, read_json_mods(raw), None, "此 JSON 档位支持读取和导入。")
    document = DsonDocument.read(raw)
    mods = document.mods()
    try:
        reason = document.write_reason()
    except ValueError as exc:
        reason = str(exc)
    if path.name.casefold() != SAVE_FILE or not path.parent.name.casefold().startswith("profile_"):
        reason = "仅可应用到 profile_* 档位内的 persist.game.json。"
    return SaveState(path, raw, mods, document.revision, reason)


def save_entry_mod(mod: Mod) -> SavedMod | None:
    if mod.source is Source.WORKSHOP and mod.workshop_id:
        return SavedMod(mod.workshop_id, "Steam")
    if mod.source is Source.LOCAL and mod.project_title:
        return SavedMod(mod.project_title, "mod_local_source")
    return None


def entries_from_slot(state: SaveState, installed: list[Mod]) -> list[ProfileEntry]:
    by_key = {}
    for mod in installed:
        if saved := save_entry_mod(mod):
            by_key.setdefault(saved.key, []).append(mod)
    entries = []
    for saved in state.mods:
        candidates = by_key.get(saved.key, [])
        if len(candidates) == 1:
            entries.append(ProfileEntry.from_mod(candidates[0]))
        elif saved.source.casefold() == "steam" and saved.name.isascii() and saved.name.isdecimal():
            name = candidates[0].name if candidates else f"Workshop {saved.name}"
            entries.append(ProfileEntry(name, Source.WORKSHOP, "", saved.name))
        else:
            name = saved.name if saved.source == "mod_local_source" else f"{saved.name} · {saved.source}"
            entries.append(ProfileEntry(name, Source.LOCAL, ""))
    return entries


def mods_for_slot(
    resolved: list[ResolvedEntry], installed: list[Mod], game_roots: list[Path]
) -> tuple[SavedMod, ...]:
    game_mods = {(root / "mods").resolve() for root in game_roots}
    by_key = {}
    for mod in installed:
        if mod.source == Source.LOCAL and Path(mod.path).parent.resolve() not in game_mods:
            continue
        if saved := save_entry_mod(mod):
            by_key.setdefault(saved.key, []).append(mod)
    result = []
    seen = set()
    for row in resolved:
        mod = row.mod
        if mod is None:
            raise ValueError(f"请先定位 {row.entry.name}。")
        root = Path(mod.path)
        if not root.is_dir():
            raise ValueError(f"{mod.name} 的安装目录已变动，请先重新扫描。")
        freshly_read = parse_mod(root, mod.source)
        saved = save_entry_mod(freshly_read)
        if (
            not saved or not supported_mod(saved) or saved != save_entry_mod(mod)
            or (mod.source == Source.WORKSHOP and root.name != saved.name)
        ):
            raise ValueError(f"{mod.name} 的游戏标识无法确认，请检查目录和 project.xml。")
        candidates = by_key.get(saved.key, [])
        if len(candidates) != 1 or candidates[0].key != mod.key or saved.key in seen:
            raise ValueError(f"{mod.name} 在游戏 Mod 目录中的来源或标题不唯一。")
        seen.add(saved.key)
        result.append(saved)
    return tuple(result)


def prepare_write(state: SaveState, mods: tuple[SavedMod, ...]) -> SavePlan:
    if state.read_only_reason:
        raise ValueError(state.read_only_reason)
    if any(not supported_mod(mod) for mod in mods) or len({mod.key for mod in mods}) != len(mods):
        raise ValueError("计划含有无效或重复的游戏 Mod 标识。")
    original = DsonDocument.read(state.raw)
    reason = original.write_reason()
    if reason:
        raise ValueError(reason)
    if original.encode() != state.raw:
        raise ValueError("原始 DSON 未通过逐字节往返验证。")
    payload = state.raw if original.mods() == mods else original.with_mods(mods).encode()
    verified = DsonDocument.read(payload)
    if verified.mods() != mods or verified.outside_mods() != original.outside_mods():
        raise ValueError("候选存档中存在非目标字段变化。")
    return SavePlan(state, payload, mods)


class SaveBackups:
    def __init__(self, data_dir: Path):
        self.root = data_dir / "save-backups"

    def capture(self, plan: SavePlan) -> Backup:
        state = plan.state
        created = datetime.now(UTC)
        key = sha256(path_key(state.path).encode("utf-8"))[:20]
        backup = Backup(
            self.root / key / str(uuid.uuid4()), created, state.path,
            state.digest, sha256(plan.payload), plan.reason,
        )
        original = backup.directory / SAVE_FILE
        atomic_bytes(original, state.raw)
        if sha256(original.read_bytes()) != backup.before:
            raise OSError("原始存档备份校验失败。")
        atomic_json(
            backup.directory / "backup.json",
            {
                "format": "darksort.save-backup",
                "target": str(backup.target),
                "created": created.isoformat(),
                "before": backup.before,
                "after": backup.after,
                "reason": backup.reason,
            },
        )
        return backup

    def list(self, target: Path) -> tuple[list[Backup], list[str]]:
        key = sha256(path_key(target).encode("utf-8"))[:20]
        backups, errors = [], []
        for path in (self.root / key).glob("*/backup.json"):
            try:
                data = read_json(path)
                if (
                    data.get("format") != "darksort.save-backup"
                    or path_key(data["target"]) != path_key(target)
                    or not all(isinstance(data[field], str) for field in ("before", "after", "reason"))
                ):
                    raise ValueError("备份目录与档位不匹配。")
                created = datetime.fromisoformat(data["created"])
                if created.tzinfo is None:
                    raise ValueError("备份时间无效。")
                backups.append(
                    Backup(path.parent, created, target, data["before"], data["after"], data["reason"])
                )
            except (OSError, TypeError, ValueError, KeyError) as exc:
                errors.append(f"{path.parent.name} · {exc}")
        backups.sort(key=lambda backup: backup.created, reverse=True)
        return backups, errors

    def prepare_restore(self, state: SaveState, backup: Backup) -> SavePlan:
        if state.path != backup.target or state.digest != backup.after:
            raise ValueError("档位内容已经变化。可打开备份目录查看原始文件。")
        if state.read_only_reason:
            raise ValueError(state.read_only_reason)
        with (backup.directory / SAVE_FILE).open("rb") as stream:
            raw = read_bounded(stream)
        if sha256(raw) != backup.before:
            raise ValueError("原始存档备份的 SHA-256 不匹配。")
        doc = DsonDocument.read(raw)
        if doc.write_reason() or doc.revision != state.revision:
            raise ValueError("备份的 DSON 版本或状态不支持恢复。")
        if doc.outside_mods() != DsonDocument.read(state.raw).outside_mods():
            raise ValueError("备份与当前档位的进度字段不同，请在副本中核对。")
        return SavePlan(state, raw, doc.mods(), "恢复原始备份")


def require_game_stopped(*, require_steam: bool = False) -> None:
    if sys.platform != "win32":
        raise OSError("存档替换功能适用于 Windows。")
    tasklist = Path(os.environ["SystemRoot"]) / "System32" / "tasklist.exe"
    result = subprocess.run(
        [str(tasklist), "/FO", "CSV", "/NH"], check=True, timeout=8,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    rows = csv.reader(result.stdout.decode("oem", errors="replace").splitlines())
    names = {row[0].casefold() for row in rows if len(row) > 1 and row[1].isdigit()}
    if not names:
        raise OSError("无法确认游戏进程状态。")
    if names & {"darkest.exe", "ddmodmanager.exe"} or any(
        "darkest" in name and "manager" in name and name.endswith(".exe") for name in names
    ):
        raise OSError("请关闭《暗黑地牢》和官方 Mod 管理器后再重试。")
    if require_steam and "steam.exe" not in names:
        raise OSError("请打开 Steam 并登录安装《暗黑地牢》的账号后再启动。")


def locked_read(path: Path):
    """Deny concurrent writes to the opened file while allowing the guarded rename."""
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    create.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    generic_read = 0x80000000
    share_read_and_delete = 0x5
    open_existing = 3
    normal_file = 0x80
    handle = create(windows_path(path), generic_read, share_read_and_delete, None, open_existing, normal_file, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except OSError:
        kernel.CloseHandle(handle)
        raise
    return os.fdopen(fd, "rb")


def windows_path(path: Path) -> str:
    text = str(path)
    if text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def replace_with_displaced(target: Path, candidate: Path, displaced: Path) -> None:
    from ctypes import wintypes

    function = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
    function.argtypes = (
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
    )
    function.restype = wintypes.BOOL
    ignore_merge_errors = 2
    if not function(
        windows_path(target), windows_path(candidate), windows_path(displaced),
        ignore_merge_errors, None, None,
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def commit(plan: SavePlan, backups: SaveBackups) -> Backup | None:
    state, target = plan.state, plan.state.path
    if plan.payload == state.raw:
        return None
    if state.read_only_reason or target.resolve(strict=True) != target or target.name.casefold() != SAVE_FILE:
        raise ValueError("目标档位或格式已变化。")
    require_game_stopped()
    original = DsonDocument.read(state.raw)
    candidate_doc = DsonDocument.read(plan.payload)
    if (
        original.write_reason() or candidate_doc.write_reason() or candidate_doc.mods() != plan.mods
        or original.outside_mods() != candidate_doc.outside_mods()
        or original.encode() != state.raw or candidate_doc.encode() != plan.payload
    ):
        raise ValueError("存档方案验证失败。")

    prefix = f".darksort-{uuid.uuid4()}"
    pending = target.with_name(prefix + ".tmp")
    displaced = target.with_name(prefix + ".previous")
    backup = None
    try:
        with locked_read(target) as stream:
            if read_bounded(stream) != state.raw:
                raise ValueError("档位内容已改变，请重新读取和预览。")
            backup = backups.capture(plan)
            atomic_bytes(pending, plan.payload)
            if pending.read_bytes() != plan.payload:
                raise OSError("同目录临时存档校验失败。")
            require_game_stopped()
            with locked_read(target) as latest:
                if read_bounded(latest) != state.raw:
                    raise ValueError("档位在备份期间发生变化，请重新读取。")
                replace_with_displaced(target, pending, displaced)
        with locked_read(target) as updated, locked_read(displaced) as previous:
            if read_bounded(updated) != plan.payload:
                raise OSError("提交后目标已被外部程序改写，保留外部版本。")
            if read_bounded(previous) != state.raw:
                raise OSError("替换时发现外部版本，已保留被替换的原文件供恢复。")
        displaced.unlink(missing_ok=True)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if displaced.is_file() and not target.exists():
            try:
                # Windows rename refuses to overwrite a target recreated by another process.
                os.rename(displaced, target)
            except OSError:
                pass
        details = f"\n原始备份：{backup.directory}" if backup else ""
        if displaced.is_file():
            details += f"\n保留的原位置文件：{displaced}"
        raise OSError(f"{exc}{details}") from exc
    finally:
        pending.unlink(missing_ok=True)
    return backup
