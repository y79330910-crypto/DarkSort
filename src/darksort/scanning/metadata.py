from __future__ import annotations

from pathlib import Path

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from darksort.models import Mod, Source

MAX_PROJECT_SIZE = 4 * 1024 * 1024
PREVIEWS = ("preview_icon.png", "preview.png", "preview.jpg", "thumbnail.png", "icon.png")


def parse_mod(folder: Path, source: Source) -> Mod:
    warnings: list[str] = []
    fields: dict[str, str] = {}
    tags: list[str] = []
    project = folder / "project.xml"
    if not project.is_file():
        warnings.append("缺少 project.xml，已按旧版 Mod 识别，请核对名称和类别。")
    else:
        try:
            if project.stat().st_size > MAX_PROJECT_SIZE:
                raise ValueError("project.xml 超过 4 MiB")
            tree = ElementTree.fromstring(project.read_bytes())
            if tree.tag.rsplit("}", 1)[-1].casefold() != "project":
                warnings.append("project.xml 根节点不是 project，请核对元数据。")
            for child in tree:
                tag = child.tag.rsplit("}", 1)[-1].casefold()
                if tag == "tags":
                    nodes = list(child) or [child]
                    tags.extend(text for node in nodes if (text := (node.text or "").strip()))
                else:
                    fields[tag] = "".join(child.itertext()).strip()
        except (OSError, ValueError, ElementTree.ParseError, DefusedXmlException) as exc:
            warnings.append(f"project.xml 解析失败，已保留目录：{exc}")
    name = fields.get("title") or folder.name
    if fields and not fields.get("title"):
        warnings.append("缺少标题，使用目录名。")
    workshop_id = fields.get("publishedfileid")
    if workshop_id and (
        not workshop_id.isascii()
        or not workshop_id.isdecimal()
        or len(workshop_id) > 20
        or not workshop_id.strip("0")
    ):
        warnings.append("PublishedFileId 无效，已忽略。")
        workshop_id = None
    if (
        source == Source.WORKSHOP and folder.name.isascii() and folder.name.isdecimal()
        and len(folder.name) <= 20 and folder.name.strip("0")
    ):
        if workshop_id and workshop_id != folder.name:
            warnings.append("Workshop ID 与安装目录不一致，使用安装目录 ID。")
        workshop_id = folder.name
    version = fields.get("version", "")
    major, minor = fields.get("versionmajor"), fields.get("versionminor")
    if major is not None or minor is not None:
        version = f"{major or '0'}.{minor or '0'}"
    preview = None
    declared = fields.get("previewiconfile", "")
    candidates = [declared] if declared else []
    candidates.extend(PREVIEWS)
    for candidate in dict.fromkeys(candidates):
        image = (folder / candidate.replace("\\", "/")).resolve()
        if not image.is_relative_to(folder.resolve()):
            warnings.append("缩略图路径超出 Mod 目录，已忽略。")
            continue
        if image.is_file():
            preview = str(image)
            break
    if declared and preview is None:
        warnings.append("声明的缩略图不存在，使用默认图标。")
    return Mod(
        path=str(folder.resolve()),
        source=source,
        name=name,
        description=fields.get("itemdescription") or fields.get("itemdescriptionshort", ""),
        tags=tuple(dict.fromkeys(tags)),
        version=version or "未知",
        workshop_id=workshop_id,
        preview_path=preview,
        warnings=tuple(dict.fromkeys(warnings)),
        target_build=fields.get("targetbuild", ""),
        project_title=fields.get("title", ""),
    )
