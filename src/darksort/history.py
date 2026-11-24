from __future__ import annotations

import uuid
from bisect import bisect_left
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from darksort.models import Profile, ProfileEntry
from darksort.profiles import parse_profile, profile_dict
from darksort.storage import atomic_json, read_json


@dataclass(frozen=True)
class Snapshot:
    id: str
    taken: datetime
    reason: str
    name: str
    profile: Profile


@dataclass(frozen=True)
class EntryChange:
    before: int | None
    after: int | None
    old_entry: ProfileEntry | None
    new_entry: ProfileEntry | None
    moved: bool = False

    @property
    def updated(self) -> bool:
        return self.old_entry is not None and self.new_entry is not None and self.old_entry != self.new_entry

    @property
    def status(self) -> str:
        if self.before is None:
            return "加入"
        if self.after is None:
            return "移除"
        if self.moved:
            return "移位 + 更新" if self.updated else "移位"
        if self.updated:
            return "更新"
        return "顺延" if self.before != self.after else "一致"


class ProfileHistory:
    def __init__(self, data_dir: Path):
        self.root = data_dir / "history"

    def capture(self, profile: Profile, reason: str, *, name: str = "") -> Snapshot:
        payload = profile_dict(profile)
        validated = parse_profile(payload)
        snapshot_id = str(uuid.uuid4())
        taken = datetime.now(UTC)
        name = name.strip()
        atomic_json(
            self.root / validated.id / f"{snapshot_id}.json",
            {
                "format": "darksort.snapshot",
                "version": 1,
                "id": snapshot_id,
                "taken": taken.isoformat(),
                "reason": reason,
                "name": name,
                "profile": payload,
            },
        )
        return Snapshot(snapshot_id, taken, reason, name, validated)

    def list(self, profile_id: str) -> tuple[list[Snapshot], list[str]]:
        profile_id = str(uuid.UUID(profile_id))
        snapshots, errors = [], []
        for path in (self.root / profile_id).glob("*.json"):
            try:
                data = read_json(path)
                if (
                    data.get("format") != "darksort.snapshot"
                    or data.get("version") != 1
                    or data.get("id") != path.stem
                    or not isinstance(data.get("profile"), dict)
                ):
                    raise ValueError("快照格式无效")
                profile = parse_profile(data["profile"])
                if profile.id != profile_id:
                    raise ValueError("快照属于其他配置")
                taken = datetime.fromisoformat(data["taken"])
                if taken.tzinfo is None or not all(
                    isinstance(data[key], str) for key in ("reason", "name")
                ):
                    raise ValueError("快照时间或说明无效")
                snapshots.append(Snapshot(path.stem, taken, data["reason"], data["name"], profile))
            except (OSError, ValueError, TypeError, KeyError) as exc:
                errors.append(f"{path.name} · {exc}")
        snapshots.sort(key=lambda snapshot: (snapshot.taken, snapshot.id), reverse=True)
        return snapshots, errors


def compare_entries(before: Profile, after: Profile) -> list[EntryChange]:
    old, new = before.entries, after.entries
    old_ids = {entry.id: index for index, entry in enumerate(old)}
    pairs = {}
    for right, entry in enumerate(new):
        left = old_ids.get(entry.id)
        if left is None:
            continue
        # v1 generated IDs from row positions. A reference change there may be a different entry.
        if old[left].reference == entry.reference or uuid.UUID(entry.id).version != 5:
            pairs[right] = left
    used = set(pairs.values())
    by_reference = defaultdict(deque)
    for left, entry in enumerate(old):
        if left not in used:
            by_reference[entry.reference].append(left)
    for right, entry in enumerate(new):
        if right not in pairs and by_reference[entry.reference]:
            left = by_reference[entry.reference].popleft()
            pairs[right] = left
            used.add(left)

    ordered = sorted(pairs.items())
    retained = retained_order(ordered)
    rows = []
    for right, entry in enumerate(new):
        left = pairs.get(right)
        rows.append(EntryChange(
            left + 1 if left is not None else None,
            right + 1,
            old[left] if left is not None else None,
            entry,
            left is not None and right not in retained,
        ))
    rows.extend(
        EntryChange(left + 1, None, entry, None) for left, entry in enumerate(old) if left not in used
    )
    return rows


def retained_order(ordered: list[tuple[int, int]]) -> set[int]:
    """Find a longest subsequence in the old order, using positions from the new order."""
    tails, ending, previous = [], [], []
    for index, (_, old_position) in enumerate(ordered):
        level = bisect_left(tails, old_position)
        previous.append(ending[level - 1] if level else -1)
        if level == len(tails):
            tails.append(old_position)
            ending.append(index)
        else:
            tails[level] = old_position
            ending[level] = index
    retained = set()
    index = ending[-1] if ending else -1
    while index != -1:
        retained.add(ordered[index][0])
        index = previous[index]
    return retained


def describe_change(before: Profile, after: Profile) -> str:
    lines = []
    for row in compare_entries(before, after):
        if row.status not in {"顺延", "一致"}:
            lines.append(f"{row.status}  {row.before or '—'} → {row.after or '—'}   {(row.new_entry or row.old_entry).name}")
    if before.groups != after.groups:
        lines.append("分组标题或位置已变化")
    if before.mod_groups != after.mod_groups:
        lines.append(f"Mod 分组发生变化 · {len(before.mod_groups)} → {len(after.mod_groups)} 组")
    if before.rules != after.rules:
        lines.append("该方案的排序规则已变化")
    if before.staged != after.staged:
        lines.append(f"暂存篮发生变化 · {len(before.staged)} → {len(after.staged)} 项")
    if before.locks != after.locks:
        lines.append(f"联锁发生变化 · {len(before.locks)} → {len(after.locks)} 组")
    return "\n".join(lines) or "条目与顺序一致"
