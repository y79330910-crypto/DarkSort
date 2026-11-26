from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from PySide6.QtCore import QLockFile

from darksort.adapters.campaigns import EXECUTABLES
from darksort.adapters.saves import locked_read, replace_with_displaced, require_game_stopped
from darksort.resource_import import (
    MAX_FILE_BYTES,
    MAX_FILES,
    ImportLayout,
    ImportSource,
    asset_path,
    contained_path,
    copy_payloads,
    file_info,
    inspect_import,
    source_stamp,
)
from darksort.storage import atomic_bytes, atomic_json, read_json
from darksort.workshop import workshop_id

DEPLOYMENT_DIR = ".darksort-file-replacements"
ID_PATTERN = re.compile(r"[0-9a-f]{32}")
HASH_PATTERN = re.compile(r"[0-9a-f]{64}")


def valid_hash(value) -> bool:
    return isinstance(value, str) and HASH_PATTERN.fullmatch(value) is not None


def game_installation(path: Path) -> Path:
    root = path.resolve(strict=True)
    if not any((root / relative).is_file() for relative, _ in EXECUTABLES):
        raise ValueError("请选择已安装《暗黑地牢 1》的游戏目录。")
    return root


def bound_game(path: str) -> Path:
    root = Path(path)
    if root.resolve(strict=True) != root or not root.is_dir():
        raise OSError("上次选择的游戏目录已变化，请恢复后再操作。")
    return root


@dataclass(frozen=True)
class Asset:
    relative: str
    size: int
    digest: str


@dataclass(frozen=True)
class ModOrigin:
    path: str
    key: str
    reference: str
    workshop_id: str | None = None
    layout: str = ""
    directory: str = ""
    stamp: str = ""

    @classmethod
    def read(cls, data: dict) -> ModOrigin:
        if not isinstance(data, dict):
            raise ValueError("资源包来源无效。")
        origin = cls(**data)
        if (
            not all(isinstance(value, str) and value for value in (origin.path, origin.key, origin.reference))
            or not Path(origin.path).is_absolute() or not valid_hash(origin.stamp)
            or not isinstance(origin.layout, str) or not isinstance(origin.directory, str)
            or origin.workshop_id is not None and workshop_id(origin.workshop_id) is None
        ):
            raise ValueError("资源包来源无效。")
        return origin


@dataclass(frozen=True)
class FilePack:
    id: str
    name: str
    game: str
    revision: str
    files: tuple[Asset, ...]
    origin: ModOrigin | None


@dataclass(frozen=True)
class Deployment:
    phase: str
    files: tuple[Asset, ...]
    originals: tuple[str | None, ...]
    reference: str | None


def read_assets(data) -> tuple[Asset, ...]:
    if not isinstance(data, list) or not 0 < len(data) <= MAX_FILES:
        raise ValueError("资源文件清单无效。")
    assets = []
    seen = set()
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("资源文件记录无效。")
        path, size, digest = (item.get(key) for key in ("relative", "size", "digest"))
        if not isinstance(path, str) or type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
            raise ValueError("资源文件记录无效。")
        path = asset_path(path)
        if path.casefold() in seen or not valid_hash(digest):
            raise ValueError("资源文件路径或校验值无效。")
        seen.add(path.casefold())
        assets.append(Asset(path, size, digest))
    return tuple(assets)


def read_deployment(directory: Path) -> Deployment:
    data = read_json(contained_path(directory, "state.json"))
    files = read_assets(data.get("files"))
    phase, originals, reference = (data.get(field) for field in ("phase", "originals", "reference"))
    if (
        data.get("version") != 1 or phase not in ("installing", "enabled", "restoring")
        or not isinstance(originals, list) or len(originals) != len(files)
        or not all(value is None or valid_hash(value) for value in originals)
        or reference is not None and not isinstance(reference, str)
    ):
        raise ValueError(f"资源部署记录无效：{directory}")
    return Deployment(phase, files, tuple(originals), reference)


def game_deployments(root: Path):
    base = contained_path(root, DEPLOYMENT_DIR)
    if base.exists():
        for path in base.iterdir():
            if ID_PATTERN.fullmatch(path.name):
                directory = contained_path(root, f"{DEPLOYMENT_DIR}/{path.name}")
                yield directory, read_deployment(directory)


def deployed_mod_references(game_roots: Iterable[Path]) -> set[str]:
    references = set()
    for root in {path.resolve() for path in game_roots}:
        for _, deployment in game_deployments(root):
            if deployment.reference:
                references.add(deployment.reference)
    return references


@contextmanager
def game_operation(root: Path):
    base = contained_path(root, DEPLOYMENT_DIR)
    base.mkdir(exist_ok=True)
    lock = QLockFile(str(contained_path(base, "operation.lock")))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        raise OSError("另一个 DarkSort 正在处理此游戏的资源，请稍后重试。")
    try:
        yield base
    finally:
        lock.unlock()


def file_digest(path: Path) -> str | None:
    try:
        size = file_info(path).st_size
    except FileNotFoundError:
        return None
    if size > MAX_FILE_BYTES:
        raise ValueError(f"目标文件超过可备份大小：{path}")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def checked_bytes(path: Path, digest: str) -> bytes:
    file_info(path)
    with path.open("rb") as stream:
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError(f"文件已变化或校验失败：{path}")
    return data


def move_new_file(source: Path, target: Path) -> None:
    if sys.platform == "win32":
        os.rename(source, target)
    else:
        os.link(source, target)
        source.unlink()


def switch_file(target: Path, before: str | None, content: bytes | None, after: str | None) -> None:
    if file_digest(target) != before:
        raise ValueError(f"目标文件已被外部修改：{target}")
    if before == after:
        return
    prefix = f".darksort-{uuid4().hex}"
    pending = target.with_name(f"{prefix}.tmp")
    displaced = target.with_name(f"{prefix}.previous")
    try:
        if content is not None:
            atomic_bytes(pending, content)
        if before is None:
            move_new_file(pending, target)
        else:
            reader = locked_read(target) if sys.platform == "win32" else target.open("rb")
            with reader as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != before:
                    raise ValueError(f"目标文件已被外部修改：{target}")
                if content is None:
                    os.rename(target, displaced)
                elif sys.platform == "win32":
                    replace_with_displaced(target, pending, displaced)
                else:
                    os.link(target, displaced)
                    os.replace(pending, target)
            if file_digest(displaced) != before:
                raise OSError(f"发现不同的外部版本，已保留在：{displaced}")
        if file_digest(target) != after:
            raise OSError(f"提交后目标文件已变化：{target}")
        displaced.unlink(missing_ok=True)
    except (OSError, ValueError) as exc:
        if displaced.exists() and not target.exists():
            try:
                move_new_file(displaced, target)
            except OSError:
                pass
        if displaced.exists():
            raise OSError(f"{exc}\n保留下来的文件：{displaced}") from exc
        raise
    finally:
        pending.unlink(missing_ok=True)


def retire_directory(directory: Path) -> None:
    # The rename commits removal. A locked leftover can never be mistaken for a live record.
    if contained_path(directory.parent, directory.name) != directory or not directory.is_dir():
        raise ValueError("无法确认资源数据目录。")
    retired = directory.with_name(f".retired-{uuid4().hex}")
    directory.rename(retired)
    shutil.rmtree(retired, ignore_errors=True)


def import_preview(root: Path, paths: Iterable[str]) -> tuple[tuple[str, str], ...]:
    result = []
    seen = set()
    for relative in paths:
        target = contained_path(root, asset_path(relative))
        canonical = str(target.resolve()).casefold()
        if canonical in seen:
            raise ValueError(f"多个资源对应同一个游戏文件：{relative}")
        seen.add(canonical)
        status = "新增"
        if target.exists():
            file_info(target)
            status = "替换"
        result.append((relative, status))
    return tuple(result)


class FilePackStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir

    @property
    def directory(self) -> Path:
        return contained_path(self.data_dir, "game-file-packs")

    def load(self, identity: str) -> FilePack:
        if not ID_PATTERN.fullmatch(identity):
            raise ValueError("资源包标识无效。")
        data = read_json(contained_path(self.directory, f"{identity}/pack.json"))
        name, game, revision = (data.get(field) for field in ("name", "game", "revision"))
        if (
            data.get("version") != 1 or not isinstance(name, str) or not name.strip()
            or not isinstance(game, str) or not Path(game).is_absolute()
            or not isinstance(revision, str) or ID_PATTERN.fullmatch(revision) is None
        ):
            raise ValueError("资源包记录无效。")
        origin = data.get("origin")
        return FilePack(identity, name, game, revision, read_assets(data.get("files")),
                        ModOrigin.read(origin) if origin is not None else None)

    def list(self) -> tuple[list[FilePack], list[str]]:
        packs, errors = [], []
        try:
            paths = list(self.directory.iterdir())
        except FileNotFoundError:
            return [], []
        except (OSError, ValueError) as exc:
            return [], [f"无法读取游戏资源包 · {exc}"]
        for path in paths:
            if not ID_PATTERN.fullmatch(path.name):
                continue
            try:
                packs.append(self.load(path.name))
            except (OSError, TypeError, ValueError) as exc:
                errors.append(f"{path.name} · {exc}")
        return sorted(packs, key=lambda pack: pack.name.casefold()), errors

    def state(self, pack: FilePack) -> Deployment | None:
        root = bound_game(pack.game)
        directory = contained_path(root, f"{DEPLOYMENT_DIR}/{pack.id}")
        return read_deployment(directory) if directory.exists() else None

    def import_pack(
        self, name: str, root: Path, source: ImportSource, layout: ImportLayout, directory: str,
        *, origin: ModOrigin | None = None, previous: FilePack | None = None,
    ) -> FilePack:
        root = game_installation(root)
        name = name.strip()
        if not name:
            raise ValueError("请输入资源包名称。")
        if previous:
            if self.load(previous.id) != previous or previous.game != str(root):
                raise ValueError("资源包已变动，请重新打开。")
            if self.state(previous):
                raise ValueError("请先停用这个资源包再更新。")
        elif origin and any(item.origin and item.origin.key == origin.key for item in self.list()[0]):
            raise ValueError("这个 Mod 已经转入，请在工具中查看。")
        files = layout.targets(directory)
        import_preview(root, (relative for relative, _ in files))
        if origin:
            origin = replace(origin, layout=layout.prefix, directory=directory, stamp=source_stamp(files))
        identity = previous.id if previous else uuid4().hex
        revision = uuid4().hex
        storage = self.directory
        storage.mkdir(parents=True, exist_ok=True)
        parent = contained_path(storage, identity)
        with TemporaryDirectory(prefix=".import-", dir=storage) as scratch:
            prepared = Path(scratch) / identity
            staged = prepared / revision
            staged.mkdir(parents=True)
            assets = tuple(Asset(*item) for item in copy_payloads(source, files, staged))
            pack = FilePack(identity, name, str(root), revision, assets, origin)
            payload = {"version": 1, **asdict(pack)}
            if previous:
                staged.rename(parent / revision)
                try:
                    atomic_json(parent / "pack.json", payload)
                except (OSError, ValueError):
                    retire_directory(parent / revision)
                    raise
                try:
                    retire_directory(contained_path(parent, previous.revision))
                except (OSError, ValueError):
                    # The updated manifest is already committed. A stale revision is unused.
                    pass
            else:
                atomic_json(prepared / "pack.json", payload)
                prepared.rename(parent)
        return pack

    def has_update(self, pack: FilePack) -> bool:
        origin = pack.origin
        if not origin:
            return False
        source = inspect_import((Path(origin.path),))
        layout = next((choice for choice in source.layouts if choice.prefix == origin.layout), None)
        if layout is None or layout.manual != bool(origin.directory):
            return True
        return source_stamp(layout.targets(origin.directory)) != origin.stamp

    def preview(self, pack: FilePack) -> tuple[str | None, ...]:
        root = game_installation(bound_game(pack.game))
        import_preview(root, (asset.relative for asset in pack.files))
        return tuple(file_digest(contained_path(root, asset.relative)) for asset in pack.files)

    def enable(self, pack: FilePack, originals: tuple[str | None, ...]) -> None:
        if self.load(pack.id) != pack:
            raise ValueError("资源包已变动，请重新预览。")
        root = game_installation(bound_game(pack.game))
        require_game_stopped()
        with game_operation(root) as base:
            if self.state(pack):
                raise ValueError("这个资源包已有部署记录，请先停用或还原。")
            requested = {str(contained_path(root, item.relative).resolve()).casefold() for item in pack.files}
            for _, other in game_deployments(root):
                occupied = {
                    str(contained_path(root, asset.relative).resolve()).casefold(): asset.relative
                    for asset in other.files
                }
                if shared := requested.intersection(occupied):
                    raise ValueError(f"已有启用的资源包占用：{occupied[min(shared)]}。请先停用它。")
            if self.preview(pack) != originals:
                raise ValueError("游戏文件与预览不同，请重新预览。")
            directory = contained_path(base, pack.id)
            payloads = contained_path(self.directory, f"{pack.id}/{pack.revision}")
            deployment = Deployment("installing", pack.files, originals,
                                    pack.origin.reference if pack.origin else None)
            with TemporaryDirectory(prefix=".backup-", dir=base) as scratch:
                prepared = Path(scratch) / pack.id
                prepared.mkdir()
                for asset, original in zip(pack.files, originals, strict=True):
                    if file_digest(contained_path(payloads, asset.relative)) != asset.digest:
                        raise ValueError(f"导入的资源校验失败：{asset.relative}")
                    if original is not None:
                        data = checked_bytes(contained_path(root, asset.relative), original)
                        atomic_bytes(contained_path(prepared, f"original/{asset.relative}"), data)
                self.write_state(prepared, deployment)
                prepared.rename(directory)
            try:
                require_game_stopped()
                for asset, original in zip(pack.files, originals, strict=True):
                    data = checked_bytes(contained_path(payloads, asset.relative), asset.digest)
                    switch_file(contained_path(root, asset.relative), original, data, asset.digest)
                self.write_state(directory, replace(deployment, phase="enabled"))
            except Exception as exc:
                try:
                    self.restore(root, directory, deployment)
                except Exception as recovery:
                    raise OSError(f"{exc}\n请在工具中继续还原。备份：{directory}\n{recovery}") from exc
                raise

    def disable(self, pack: FilePack) -> None:
        root = bound_game(pack.game)
        require_game_stopped()
        with game_operation(root) as base:
            deployment = self.state(pack)
            if deployment:
                self.restore(root, contained_path(base, pack.id), deployment)

    def restore(self, root: Path, directory: Path, deployment: Deployment) -> None:
        pending = []
        for asset, original in zip(deployment.files, deployment.originals, strict=True):
            target = contained_path(root, asset.relative)
            current = file_digest(target)
            if current == original:
                continue
            if current != asset.digest:
                raise ValueError(f"文件已被外部修改，先核对再还原：{target}\n备份：{directory}")
            backup = contained_path(directory, f"original/{asset.relative}") if original else None
            if backup and file_digest(backup) != original:
                raise ValueError(f"原文件备份校验失败：{backup}")
            pending.append((asset, original, backup))
        require_game_stopped()
        self.write_state(directory, replace(deployment, phase="restoring"))
        for asset, original, backup in pending:
            data = checked_bytes(backup, original) if backup else None
            switch_file(contained_path(root, asset.relative), asset.digest, data, original)
        retire_directory(directory)

    def remove(self, pack: FilePack) -> None:
        if self.load(pack.id) != pack:
            raise ValueError("资源包已变动，请重新打开。")
        root = bound_game(pack.game)
        with game_operation(root):
            if self.state(pack):
                raise ValueError("请先停用并还原，再移除资源包。")
            retire_directory(contained_path(self.directory, pack.id))

    @staticmethod
    def write_state(directory: Path, deployment: Deployment) -> None:
        atomic_json(contained_path(directory, "state.json"), {"version": 1, **asdict(deployment)})
