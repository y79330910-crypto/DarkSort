from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from darksort.conflicts import read_manifest
from darksort.models import Mod

ART = {".png", ".dds", ".tga", ".bmp", ".atlas", ".skel"}
RULES = {".darkest", ".json", ".xml", ".csv", ".lua"}
PLAY = {
    "campaign",
    "curios",
    "effects",
    "inventory",
    "loot",
    "modes",
    "quirks",
    "raid",
    "scripts",
    "shared",
    "trinkets",
    "upgrades",
}
RESOURCE_ROOTS = PLAY | {
    "backgrounds",
    "colours",
    "dungeons",
    "fonts",
    "heroes",
    "localization",
    "monsters",
    "panels",
    "props",
    "textures",
}
FALLBACK_FILE_LIMIT = 2048


def skin_resource(path: str) -> tuple[str, bool] | None:
    parts = path.split("/")
    if len(parts) < 4 or parts[0].casefold() != "heroes" or Path(parts[-1]).suffix.casefold() not in ART:
        return None
    hero = parts[1].casefold()
    palette, _, letter = parts[2].rpartition("_")
    variant = bool(palette.casefold().endswith(hero) and len(letter) == 1 and "A" <= letter <= "Z")
    if variant and any(part.casefold() == "anim" for part in parts[3:-1]):
        return hero, True
    if parts[2].casefold() == "anim":
        return hero, False
    return None


def fallback_files(root: Path, cancelled) -> Iterable[str]:
    seen = 0
    for path in sorted(root.iterdir()):
        if path.name.casefold() not in RESOURCE_ROOTS or not path.is_dir():
            continue
        if path.is_symlink() or path.is_junction():
            continue
        for directory, children, files in os.walk(path, followlinks=False):
            if cancelled():
                return
            parent = Path(directory)
            children[:] = sorted(
                name
                for name in children
                if not name.startswith(".")
                and not (parent / name).is_junction()
                and not (parent / name).is_symlink()
            )
            for file in sorted(files):
                seen += 1
                yield (parent / file).relative_to(root).as_posix()
                if seen >= FALLBACK_FILE_LIMIT or cancelled():
                    return


def inspect_resources(mod: Mod, cancelled) -> Mod:
    root = Path(mod.path)
    try:
        files = read_manifest(root / "modfiles.txt")
    except (OSError, ValueError):
        files = fallback_files(root, cancelled)
    kinds, definitions, targets = set(), set(), set()
    appearances: dict[tuple[str, bool], str] = {}
    try:
        for file in files:
            if cancelled():
                break
            parts = file.casefold().split("/")
            if len(parts) < 2 or "_template" in parts:
                continue
            directory, filename = parts[0], parts[-1]
            suffix = os.path.splitext(filename)[1]
            if directory == "fonts" and suffix in ART | {".fnt", ".ttf", ".otf"}:
                kinds.add("font")
            if directory == "localization" and suffix in {".loc", ".loc2", ".xml", ".csv"}:
                kinds.add("translation")
            if directory in PLAY and suffix in RULES:
                kinds.add("gameplay")
            if (
                directory in {"dungeons", "monsters"}
                and suffix in RULES
                and not filename.endswith((".art.darkest", ".override.darkest"))
            ):
                kinds.add("region")
                subtype = "monster" if directory == "monsters" else "dungeon"
                if subtype not in kinds and (root / file).is_file():
                    kinds.add(subtype)
            if suffix in ART:
                kinds.add("texture")
            if directory == "heroes" and len(parts) >= 3:
                hero = parts[1]
                if len(parts) == 3 and filename == f"{hero}.info.darkest":
                    definitions.add(hero)
                if suffix in ART or filename in {f"{hero}.art.darkest", f"{hero}.override.darkest"}:
                    targets.add(hero)
                evidence = skin_resource(file)
                if evidence and (evidence not in appearances or file < appearances[evidence]):
                    if (root / file).is_file():
                        appearances[evidence] = file
    except OSError:
        pass
    return replace(
        mod,
        resource_kinds=tuple(sorted(kinds)),
        hero_definitions=tuple(sorted(definitions)),
        skin_targets=tuple(sorted(targets)),
        skin_files=tuple(sorted(appearances.values())),
    )
