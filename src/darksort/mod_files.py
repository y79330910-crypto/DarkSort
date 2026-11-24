import codecs
import re
import stat
from pathlib import Path

from darksort.conflicts import read_manifest, safe_relative
from darksort.scanning.content import RESOURCE_ROOTS, fallback_files
from darksort.search import search_text

TEXT_TYPES = {".darkest", ".json", ".xml", ".csv", ".txt", ".md", ".lua"}
METADATA_FILES = {"project.xml", "modfiles.txt", "readme.md", "readme.txt"}
FILE_KEYWORDS = (
    ("技能", ("skill", ".info.darkest", "upgrades/"), ("combat_skill:", "camping_skill:")),
    ("数值", ("stat", ".info.darkest", "upgrades/"), ("weapon:", "armour:", "resistances:")),
    ("饰品", ("trinket", "inventory/"), ("trinket",)),
    ("怪物", ("monsters/",), ("monster:", "monster_brain:")),
    ("地区", ("dungeons/",), ()),
    ("贴图", ("/anim/", ".art.darkest"), ()),
    ("皮肤", ("/anim/", ".art.darkest"), ()),
    ("配置", (".info.darkest", "project.xml"), ()),
)
MAX_TEXT_BYTES = 65_536
MAX_RESULTS = 3
MAX_FILES_TO_READ = 18
QUOTED = re.compile(r'["「“`][^"」”`\r\n]{2,90}["」”`]')


def mod_relative(path: str) -> str | None:
    safe = safe_relative(path)
    if not safe:
        return None
    parts = safe.casefold().split("/")
    if any(part.startswith(".") or part == "_template" for part in parts):
        return None
    if len(parts) == 1:
        return safe if parts[0] in METADATA_FILES else None
    return safe if parts[0] in RESOURCE_ROOTS else None


def read_mod_text(root: Path, relative: str) -> tuple[str, bool]:
    relative = mod_relative(relative)
    if not relative or Path(relative).suffix.casefold() not in TEXT_TYPES:
        raise ValueError("超出可读取的 Mod 文本范围。")
    path = root
    if path.is_symlink() or path.is_junction():
        raise ValueError("Mod 目录指向其他位置。")
    for part in relative.split("/"):
        path = path / part
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("Mod 文件指向其他位置。")
    if not stat.S_ISREG(info.st_mode) or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("无法在 Mod 目录内读取该文件。")
    with path.open("rb") as stream:
        raw = stream.read(MAX_TEXT_BYTES + 1)
    clipped = len(raw) > MAX_TEXT_BYTES
    raw = raw[:MAX_TEXT_BYTES]
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        content = raw.decode("utf-16", errors="replace" if clipped else "strict")
    else:
        if b"\x00" in raw:
            raise ValueError("该文件是二进制资源。")
        try:
            content = raw.decode("utf-8-sig", errors="replace" if clipped else "strict")
        except UnicodeDecodeError:
            content = raw.decode("gb18030")
    if clipped and "\n" in content:
        content = content.rpartition("\n")[0]
    return content, clipped


def inspect_mod_files(root: Path, question: str, *, previous_file="", subject_name="") -> tuple[str, str]:
    """Return a short, line-numbered excerpt or a list of this Mod's matching paths."""
    if root.is_symlink() or root.is_junction() or not root.is_dir():
        return "目前打不开这个 Mod 的安装目录。", ""
    try:
        try:
            paths = read_manifest(root / "modfiles.txt")
            origin = "Mod 文件清单"
        except (OSError, ValueError):
            paths = set(fallback_files(root, lambda: False))
            origin = "当前目录的有限扫描"
        paths.update(name for name in METADATA_FILES if (root / name).is_file())
    except OSError:
        return "目前打不开这个 Mod 的文件清单。", ""

    terms = search_text(question.replace("\\", "/"))
    known = {item for path in paths if (item := mod_relative(path))}
    explicit = [
        path for path in sorted(known)
        if search_text(path) in terms or (
            Path(path).suffix.casefold() in TEXT_TYPES and search_text(Path(path).name) in terms
        )
    ]
    if previous_file in known and ("这个文件" in terms or "该文件" in terms) and not explicit:
        explicit = [previous_file]
    hints = [item for word, matches, _ in FILE_KEYWORDS if word in terms for item in matches]
    quoted = [
        search_text(value[1:-1]) for value in QUOTED.findall(question)
        if search_text(value[1:-1]) != search_text(subject_name)
        and not any(search_text(value[1:-1]) in search_text(path) for path in explicit)
    ]
    line_terms = quoted or [item for word, _, lines in FILE_KEYWORDS if word in terms for item in lines]
    candidates = sorted(known, key=lambda path: (
        not any(hint in path.casefold() for hint in hints) if hints else path.casefold() != "project.xml",
        Path(path).suffix.casefold() not in TEXT_TYPES,
        path.casefold(),
    ))
    if not explicit and not line_terms and (
        "列出" in terms or "哪些文件" in terms or "文件列表" in terms or "文件清单" in terms
    ):
        shown = candidates[:16]
        if not shown:
            return "没有查到可列出的资源路径。", ""
        more = f"\n另有 {len(candidates) - len(shown)} 条路径。" if len(candidates) > len(shown) else ""
        return f"{origin}中的资源路径\n" + "\n".join(shown) + more, ""

    text_paths = explicit or [path for path in candidates if Path(path).suffix.casefold() in TEXT_TYPES]
    excerpts = []
    first_path = ""
    for path in text_paths[:MAX_FILES_TO_READ]:
        try:
            value, clipped = read_mod_text(root, path)
        except (OSError, ValueError):
            continue
        numbered = ((i, line.strip()) for i, line in enumerate(value.splitlines(), 1))
        matching = [(i, line) for i, line in numbered if line and (
            not line_terms or any(term in line.casefold() for term in line_terms)
        )]
        if not matching:
            continue
        first_path = first_path or path
        lines = "\n".join(f"第 {i} 行  {line[:260]}" for i, line in matching[:8])
        suffix = "\n（显示匹配片段）" if clipped or len(matching) > 8 else ""
        excerpts.append(f"{path}\n{lines}{suffix}")
        if len(excerpts) == MAX_RESULTS:
            break
    if excerpts:
        return "\n\n".join(excerpts), first_path
    if explicit:
        return "找到了所指的路径，但文件无法读取，或这次没查到匹配的文字。", ""
    return "这次读取的文本片段里没有找到匹配内容。可以指定文件名或换个关键词。", ""
