from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from zipfile import BadZipFile, ZipFile

from darksort.conflicts import safe_relative
from darksort.scanning.content import RESOURCE_ROOTS

ASSET_ROOTS = (RESOURCE_ROOTS - {"scripts"}) | {
    "activity_log", "audio", "cursors", "dlc", "fe_flow", "fx", "game_over", "loading_screen",
    "maps", "overlays", "raid_result", "raid_results", "scrolls", "shaders", "video",
}
ASSET_SUFFIXES = {
    ".atlas", ".bmp", ".csv", ".darkest", ".dds", ".fnt", ".jpeg", ".jpg", ".json", ".loc",
    ".loc2", ".ogg", ".otf", ".png", ".skel", ".tga", ".ttf", ".wav", ".webp", ".xml",
}
METADATA_FILES = {
    "project.xml", "modfiles.txt", "preview_icon.png", "preview.png", "preview.jpg",
    "preview.jpeg", "thumbnail.png", "icon.png",
}
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_PACKAGE_BYTES = 1024 * 1024 * 1024
MAX_FILES = 8192


def relative_path(text: str) -> str:
    relative = safe_relative(text)
    if relative is None or any(
        part.rstrip(" .") != part or PureWindowsPath(part).is_reserved()
        or any(char in part for char in '<>"|?*')
        for part in relative.split("/")
    ):
        raise ValueError(f"路径无效：{text}")
    return relative


def asset_path(text: str) -> str:
    relative = relative_path(text)
    parts = relative.split("/")
    if (
        len(parts) < 2 or parts[0].casefold() not in ASSET_ROOTS
        or Path(parts[-1]).suffix.casefold() not in ASSET_SUFFIXES
        or parts[-1].casefold() in {"project.xml", "modfiles.txt"}
    ):
        raise ValueError(f"这个位置或文件类型不能作为 DD1 资源部署：{relative}")
    return relative


def is_redirect(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def contained_path(root: Path, relative: str) -> Path:
    """Reject redirected parent directories as well as redirected files."""
    target = root
    for part in relative_path(relative).split("/"):
        target /= part
        try:
            info = target.lstat()
        except FileNotFoundError:
            continue
        if is_redirect(info):
            raise ValueError(f"路径包含链接或重定向：{target}")
    return target


def file_info(path: Path) -> os.stat_result:
    info = path.lstat()
    if is_redirect(info) or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"请选择普通文件：{path}")
    return info


@dataclass(frozen=True)
class SourceFile:
    name: str
    location: Path | str
    size: int
    stamp: int


@dataclass(frozen=True)
class ImportLayout:
    prefix: str
    manual: bool
    members: tuple[SourceFile, ...]

    def targets(self, directory: str = "") -> tuple[tuple[str, SourceFile], ...]:
        if self.manual and not directory:
            raise ValueError("请选择游戏内的目标资源文件夹。")
        files = []
        seen = set()
        prefix = f"{self.prefix}/" if self.prefix else ""
        for member in self.members:
            path = member.name.removeprefix(prefix)
            relative = asset_path(f"{directory}/{path}" if self.manual else path)
            key = relative.casefold()
            if key in seen:
                raise ValueError(f"这个包有多个文件对应同一个位置：{relative}")
            seen.add(key)
            files.append((relative, member))
        if len(files) > MAX_FILES or sum(member.size for _, member in files) > MAX_PACKAGE_BYTES:
            raise ValueError("资源包过大，请分开导入。")
        if any(member.size > MAX_FILE_BYTES for _, member in files):
            raise ValueError("资源包中有超过 128 MiB 的单个文件。")
        return tuple(sorted(files, key=lambda item: item[0].casefold()))


@dataclass(frozen=True)
class ImportSource:
    name: str
    archive: Path | None
    layouts: tuple[ImportLayout, ...]


def folder_members(root: Path):
    def fail(error):
        raise error

    for directory, children, names in os.walk(root, onerror=fail, followlinks=False):
        parent = Path(directory)
        children[:] = sorted(
            name for name in children
            if not name.startswith(".") and not is_redirect((parent / name).lstat())
        )
        for name in sorted(names):
            path = parent / name
            if path.suffix.casefold() in ASSET_SUFFIXES and not is_redirect(path.lstat()):
                info = file_info(path)
                yield SourceFile(
                    relative_path(path.relative_to(root).as_posix()), path, info.st_size, info.st_mtime_ns,
                )


def archive_members(path: Path):
    if file_info(path).st_size > MAX_PACKAGE_BYTES:
        raise ValueError("ZIP 超过 1 GiB，请分开导入。")
    try:
        with ZipFile(path) as archive:
            if len(archive.infolist()) > MAX_FILES * 4:
                raise ValueError("ZIP 中的条目过多。")
            for entry in archive.infolist():
                name = relative_path(entry.orig_filename.rstrip("/\\"))
                if entry.is_dir() or Path(name).suffix.casefold() not in ASSET_SUFFIXES:
                    continue
                if stat.S_ISLNK(entry.external_attr >> 16):
                    raise ValueError(f"ZIP 中包含资源链接：{name}")
                yield SourceFile(name, entry.filename, entry.file_size, entry.CRC)
    except BadZipFile as exc:
        raise ValueError("无法读取 ZIP 压缩包。") from exc


def inspect_import(paths: tuple[Path, ...]) -> ImportSource:
    if not paths:
        raise ValueError("请选择要导入的文件或目录。")
    first = paths[0]
    single = len(paths) == 1
    archive = first if single and first.suffix.casefold() == ".zip" and first.is_file() else None
    if single and first.is_dir():
        if is_redirect(first.lstat()):
            raise ValueError("请选择资源文件夹的实际位置。")
        members = folder_members(first)
    elif archive:
        members = archive_members(archive)
    else:
        files = []
        for path in paths:
            if path.suffix.casefold() not in ASSET_SUFFIXES:
                raise ValueError(f"不支持这种资源文件：{path.name}")
            info = file_info(path)
            files.append(SourceFile(relative_path(path.name), path, info.st_size, info.st_mtime_ns))
        name = first.stem if single else first.parent.name
        return ImportSource(name, None, (ImportLayout("", True, tuple(files)),))

    inferred, flat = {}, {}
    for number, member in enumerate(members, 1):
        if number > MAX_FILES * 4:
            raise ValueError("源目录文件过多，请选择资源所在的文件夹。")
        parts = member.name.split("/")
        if any(part.startswith(".") or part.casefold() == "__macosx" for part in parts):
            continue
        index = next((i for i, part in enumerate(parts[:-1]) if part.casefold() in ASSET_ROOTS), None)
        if parts[-1].casefold() in {"project.xml", "modfiles.txt"}:
            continue
        if index is not None:
            inferred.setdefault("/".join(parts[:index]), []).append(member)
        elif parts[-1].casefold() not in METADATA_FILES:
            flat.setdefault("/".join(parts[:-1]), []).append(member)
    choices = inferred or flat
    if not choices:
        raise ValueError("没有找到可以导入的资源文件。")
    return ImportSource(
        first.stem if archive else first.name, archive,
        tuple(ImportLayout(path, not inferred, tuple(files)) for path, files in sorted(choices.items())),
    )


def source_stamp(files: tuple[tuple[str, SourceFile], ...]) -> str:
    rows = [(path, item.name, item.size, item.stamp) for path, item in files]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()


def copy_payloads(source: ImportSource, files, destination: Path):
    def copy(archive):
        for relative, item in files:
            output = contained_path(destination, relative)
            output.parent.mkdir(parents=True, exist_ok=True)
            if archive is None:
                info = file_info(item.location)
                if (info.st_size, info.st_mtime_ns) != (item.size, item.stamp):
                    raise ValueError(f"导入期间源文件已变化：{item.name}")
                stream = item.location.open("rb")
            else:
                member = archive.getinfo(item.location)
                if (member.file_size, member.CRC) != (item.size, item.stamp):
                    raise ValueError(f"导入期间 ZIP 文件已变化：{item.name}")
                stream = archive.open(member)
            with stream, output.open("xb") as target:
                remaining = item.size
                digest = hashlib.sha256()
                while block := stream.read(1024 * 1024):
                    remaining -= len(block)
                    if remaining < 0:
                        raise ValueError(f"文件大小超出预览：{item.name}")
                    digest.update(block)
                    target.write(block)
                if remaining:
                    raise ValueError(f"文件大小与预览不同：{item.name}")
                target.flush()
                os.fsync(target.fileno())
            yield relative, item.size, digest.hexdigest()

    if source.archive:
        try:
            with ZipFile(source.archive) as archive:
                yield from copy(archive)
        except (BadZipFile, KeyError, RuntimeError, NotImplementedError) as exc:
            raise ValueError(f"无法读取 ZIP 中的资源：{exc}") from exc
    else:
        yield from copy(None)
