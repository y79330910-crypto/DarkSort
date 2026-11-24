"""Read-only smoke check using actual Steam installation, writing only to artifacts/."""

import hashlib
import json
import sys
from pathlib import Path
from time import perf_counter

from darksort.scanning.discovery import discover
from darksort.scanning.scanner import scan
from darksort.settings import Settings


def digest(files):
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}


def main():
    output = Path(__file__).resolve().parents[1] / "artifacts"
    output.mkdir(exist_ok=True)
    discovery = discover(Settings())
    project_files = [
        file
        for root in discovery.roots
        if root.path.is_dir()
        for file in root.path.glob("*/project.xml")
    ]
    before = digest(project_files)
    started = perf_counter()
    first = scan(discovery.roots, output / "verification-cache.sqlite3", refresh=True)
    elapsed = perf_counter() - started
    second = scan(discovery.roots, output / "verification-cache.sqlite3")
    unchanged = before == digest(project_files)
    success = (
        bool(first.mods)
        and not first.cancelled
        and unchanged
        and len(first.mods) == len(second.mods)
    )
    report = {
        "success": success,
        "game_paths": [str(path) for path in discovery.games],
        "roots": [
            {"path": str(root.path), "source": root.source.value} for root in discovery.roots
        ],
        "mods": [mod.to_dict() for mod in first.mods],
        "mod_count": len(first.mods),
        "cached_count": second.cache_hits,
        "scan_seconds": round(elapsed, 4),
        "project_xml_unchanged": unchanged,
        "issues": [issue.__dict__ for issue in discovery.issues + first.issues],
    }
    report_path = output / "installation-verification.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {key: value for key, value in report.items() if key not in {"mods", "roots"}},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
