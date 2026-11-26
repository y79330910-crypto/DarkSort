import re
import shutil
import stat
from pathlib import Path, PureWindowsPath
from tempfile import TemporaryDirectory
from xml.etree import ElementTree as Xml

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from darksort.file_replacements import game_installation
from darksort.models import Source
from darksort.resource_import import contained_path, is_redirect
from darksort.scanning.metadata import MAX_PROJECT_SIZE, parse_mod
from darksort.search import search_text, unique_title
from darksort.workshop import workshop_id


def mod_parent(game: Path) -> Path:
    return contained_path(game_installation(game), "mods")


def check_folder(folder: Path) -> Path:
    info = folder.lstat()
    if is_redirect(info) or not stat.S_ISDIR(info.st_mode) or folder.resolve(strict=True) != folder:
        raise ValueError(f"请选择实际的 Mod 目录：{folder}")
    return folder


def local_installation(mod, roots) -> Path:
    folder = Path(mod.path)
    if mod.source != Source.LOCAL or not any(
        root.source == Source.LOCAL and folder.parent == root.path.resolve() for root in roots
    ):
        raise ValueError("只可卸载已扫描的本地 Mod 子目录。")
    return check_folder(folder)


def folder_name(title: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title)[:80].strip(" .")
    if not name or PureWindowsPath(name).is_reserved() or name.casefold() in (
        "downloads", "temp", "downloading",
    ):
        name = f"Mod {name}".rstrip()
    return name


def project_xml(title, *, description="", source_id=None, original=None):
    project = original if original is not None else Xml.Element("project")
    values = {"Title": title, "ModDataPath": ".", "UploadMode": "dont_submit"}
    if original is None:
        values.update(Language="english", VersionMajor="1", VersionMinor="0", TargetBuild="0",
                      ItemDescription=description)
    if source_id:
        values["PublishedFileId"] = source_id
    for tag, value in values.items():
        for child in tuple(project):
            if child.tag.rsplit("}", 1)[-1].casefold() == tag.casefold():
                project.remove(child)
        Xml.SubElement(project, tag).text = value
    return Xml.tostring(project, encoding="utf-8", xml_declaration=True)


def existing_titles(parent: Path):
    for child in parent.iterdir():
        if not child.name.startswith(".") and child.is_dir() and not is_redirect(child.lstat()):
            title = parse_mod(child, Source.LOCAL).project_title
            if title:
                yield title


def create_blank(parent: Path, title: str, description="", *, manual=False) -> Path:
    title = title.strip()
    if not 0 < len(title) <= 120 or any(ord(char) < 32 for char in title):
        raise ValueError("请输入 1–120 个字符的名称。")
    parent.mkdir(parents=True, exist_ok=True)
    check_folder(parent)
    name = folder_name(title)
    destination = parent / name
    if not manual and search_text(title) in {search_text(name) for name in existing_titles(parent)}:
        raise ValueError("已存在同名本地 Mod，请换一个名称。")
    if destination.exists():
        raise FileExistsError(f"目录已存在：{destination}")
    with TemporaryDirectory(prefix=".darksort-new-", dir=parent) as temp:
        scratch = Path(temp) / name
        scratch.mkdir()
        if manual:
            if description.strip():
                (scratch / "安装说明.txt").write_text(description.strip() + "\n", encoding="utf-8")
        else:
            (scratch / "project.xml").write_bytes(project_xml(title, description=description))
        scratch.rename(destination)
    return destination


def tree_snapshot(root):
    snapshot = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        for child in directory.iterdir():
            info = child.lstat()
            if is_redirect(info) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ValueError(f"Mod 目录包含重定向或特殊文件：{child}")
            snapshot[child.relative_to(root)] = (stat.S_ISDIR(info.st_mode), info.st_size,
                                                 info.st_mtime_ns, info.st_ino)
            if stat.S_ISDIR(info.st_mode):
                pending.append(child)
    return snapshot


def copy_workshop_to_local(mod, parent: Path) -> Path:
    identifier = workshop_id(mod.workshop_id)
    if mod.source != Source.WORKSHOP or not identifier:
        raise ValueError("请选择带有效工坊 ID 的 Mod。")
    source = check_folder(Path(mod.path))
    parent.mkdir(parents=True, exist_ok=True)
    check_folder(parent)
    if parent.is_relative_to(source) or source.is_relative_to(parent):
        raise ValueError("目标目录必须与创意工坊安装目录分开。")
    title = unique_title((mod.project_title or mod.name)[:120], existing_titles(parent))
    directory = unique_title(folder_name(title), (item.name for item in parent.iterdir()), limit=80)
    destination = parent / directory
    snapshot = tree_snapshot(source)
    with TemporaryDirectory(prefix=".darksort-workshop-", dir=parent) as temp:
        scratch = Path(temp) / directory
        scratch.mkdir()
        for relative, (is_directory, size, modified, _) in sorted(snapshot.items()):
            target = scratch / relative
            if is_directory:
                target.mkdir()
            else:
                origin = contained_path(source, relative.as_posix())
                shutil.copy2(origin, target)
                if (info := target.stat()).st_size != size or info.st_mtime_ns != modified:
                    raise OSError(f"副本校验失败：{relative}")
        if snapshot != tree_snapshot(source):
            raise OSError("Steam 正在更新此 Mod。请在更新完成后重新复制。")
        saved = scratch / "project.xml"
        project = None
        if saved.is_file() and saved.stat().st_size <= MAX_PROJECT_SIZE:
            try:
                project = ElementTree.fromstring(saved.read_bytes())
                if project.tag.casefold() != "project":
                    project = None
            except (Xml.ParseError, DefusedXmlException):
                pass
        saved.write_bytes(project_xml(title, description=mod.description, source_id=identifier, original=project))
        if parse_mod(scratch, Source.LOCAL).project_title != title or destination.exists():
            raise ValueError("无法确认本地副本的名称或目标目录已存在。")
        scratch.rename(destination)
    return destination
