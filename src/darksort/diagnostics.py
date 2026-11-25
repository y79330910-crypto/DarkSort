import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, replace
from functools import cached_property
from typing import Literal

from darksort.conflicts import ConflictReport, FileStatus
from darksort.models import ResolvedEntry


@dataclass(frozen=True)
class PlanProblem:
    level: Literal["warning", "error", "review"]
    title: str
    message: str
    advice: str
    targets: tuple[tuple[str, str], ...]
    details: str = ""
    order_pair: tuple[str, str] | None = None

    @cached_property
    def key(self) -> str:
        # Keep an ignore tied to the evidence and roles, independent of list positions.
        signature = (self.level, self.title, self.message, sorted(self.targets), self.details)
        return hashlib.sha256(json.dumps(signature).encode()).hexdigest()


def installation_problems(entries: list[ResolvedEntry], report: ConflictReport | None):
    seen = {}
    unavailable = set(report.unavailable) if report else set()
    for row in entries:
        identity = row.entry.id
        name = row.entry.name
        if row.mod is None:
            yield PlanProblem(
                "error", "找不到这个 Mod", f"这份方案里有「{name}」，但管理器找不到它。",
                "先安装这个 Mod，或选中这一行后点「匹配」。",
                ((identity, "找不到"),), row.problem,
            )
        elif row.mod.key in seen:
            yield PlanProblem(
                "error", "同一个 Mod 加了两次", f"这两行指向同一个「{name}」。",
                "选中重复的那一行，从方案移出。",
                ((seen[row.mod.key], "第一项"), (identity, "重复项")),
            )
        else:
            seen[row.mod.key] = identity
            if row.mod.key in unavailable:
                yield PlanProblem(
                    "error", "找不到 Mod 文件夹", f"「{name}」的文件夹打不开。",
                    "检查是否已移动或删除这个 Mod，然后重新扫描。",
                    ((identity, "打不开"),), row.mod.path,
                )


def file_problems(entries: list[ResolvedEntry], report: ConflictReport):
    grouped = defaultdict(list)
    for problem in explain_files(entries, report):
        grouped[replace(problem, details="")].append(problem.details)
    for problem, paths in grouped.items():
        message = problem.message if len(paths) == 1 else f"{problem.message} 类似情况共 {len(paths)} 处。"
        yield replace(problem, message=message, details="\n".join(paths))


def explain_files(entries: list[ResolvedEntry], report: ConflictReport):
    for hit in report.conflicts:
        candidates = tuple(p for p in hit.providers if p.listed is not False)
        if hit.status == FileStatus.MISSING:
            missing = tuple(p for p in candidates if p.listed and p.size is None)
            names = "、".join(dict.fromkeys(p.mod.name for p in missing))
            yield PlanProblem(
                "error", "Mod 缺文件", f"「{names}」少了安装清单里的一份文件。",
                "重新安装缺文件的 Mod，再点「重新检查」。",
                tuple(dict.fromkeys((entries[p.rank - 1].entry.id, "缺文件") for p in missing)), hit.path,
            )
        elif hit.status == FileStatus.OVERWRITE and hit.winner:
            winner = hit.winner
            resource = {".info.darkest": "英雄数值", ".art.darkest": "英雄外观"}
            name = next((label for suffix, label in resource.items() if hit.path.endswith(suffix)), "英雄设置")
            order_pair = None
            if len(candidates) == 2:
                loser = next(p for p in candidates if p != winner)
                first, second = entries[winner.rank - 1].entry, entries[loser.rank - 1].entry
                if first.reference != second.reference:
                    order_pair = (first.id, second.id)
            yield PlanProblem(
                "warning", f"{name}文件被覆盖",
                f"「{winner.mod.name}」排得更靠上，游戏用它的这份文件。",
                "想用另一份，把对应的 Mod 放到上面。",
                tuple((entries[p.rank - 1].entry.id, "生效" if p == winner else "被覆盖")
                      for p in candidates), hit.path, order_pair=order_pair,
            )
        elif hit.status == FileStatus.UNKNOWN and len({p.rank for p in candidates}) > 1:
            yield PlanProblem(
                "review", "几个 Mod 有同名文件",
                "文件名和位置相同，游戏怎么用它们还需要确认。",
                "遇到实际异常时，可以先看这些 Mod 的说明。",
                tuple(dict.fromkeys((entries[p.rank - 1].entry.id, "相关 Mod") for p in candidates)),
                hit.path,
            )
