from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from darksort.adapters.dson import SavedMod, supported_mod
from darksort.adapters.saves import SaveState, entries_from_slot, save_entry_mod
from darksort.history import retained_order
from darksort.models import Mod, ResolvedEntry, Source, path_key
from darksort.storage import atomic_json, read_json


class SaveBindings:
    def __init__(self, data_dir: Path):
        self.file = data_dir / "save-bindings.json"

    def read(self) -> dict[str, str]:
        try:
            payload = read_json(self.file)
        except FileNotFoundError:
            return {}
        slots = payload.get("slots")
        if payload.get("version") != 1 or not isinstance(slots, dict):
            raise ValueError("档位绑定格式无效。")
        result = {}
        for path, profile_id in slots.items():
            if not path or "\x00" in path or not Path(path).is_absolute() or not isinstance(profile_id, str):
                raise ValueError("档位绑定的路径或方案标识无效。")
            result[path_key(path)] = str(UUID(profile_id))
        return result

    def get(self, slot: Path) -> str | None:
        return self.read().get(path_key(slot))

    def set(self, slot: Path, profile_id: str | None) -> None:
        mapping = self.read()
        key = path_key(slot)
        if profile_id is None:
            mapping.pop(key, None)
        else:
            mapping[key] = str(UUID(profile_id))
        self.write(mapping)

    def remove_profile(self, profile_id: str) -> None:
        mapping = self.read()
        remaining = {key: identity for key, identity in mapping.items() if identity != profile_id}
        if len(remaining) != len(mapping):
            self.write(remaining)

    def write(self, mapping: dict[str, str]) -> None:
        atomic_json(self.file, {"version": 1, "slots": mapping})


@dataclass(frozen=True)
class SlotComparison:
    current: tuple[str, ...]
    desired: tuple[str, ...]
    plan_only: int
    slot_only: int
    moves: int
    unknown: int


def plan_identity(row: ResolvedEntry) -> SavedMod | None:
    entry = row.entry
    candidate = save_entry_mod(row.mod) if row.mod else None
    if candidate is None and entry.source == Source.WORKSHOP and entry.workshop_id:
        candidate = SavedMod(entry.workshop_id, "Steam")
    return candidate if candidate and supported_mod(candidate) else None


def compare_slot(
    state: SaveState, installed: list[Mod], resolved: list[ResolvedEntry]
) -> SlotComparison:
    current = entries_from_slot(state, installed)
    positions = defaultdict(deque)
    for old, mod in enumerate(state.mods):
        positions[mod.key].append(old)
    identities = [plan_identity(row) for row in resolved]
    matches = {}
    for new, identity in enumerate(identities):
        if identity and positions[identity.key]:
            matches[new] = positions[identity.key].popleft()
    stationary = retained_order(list(matches.items()))
    missing = {old for remainder in positions.values() for old in remainder}
    current_lines = tuple(
        f"{old + 1:02d}  {entry.name}" + ("  ·  仅档位" if old in missing else "")
        for old, entry in enumerate(current)
    )
    desired_lines = []
    plan_only = unknown = 0
    for new, (row, identity) in enumerate(zip(resolved, identities, strict=True)):
        old = matches.get(new)
        if identity is None:
            explanation = "身份待确认"
            unknown += 1
        elif old is None:
            explanation = "仅方案"
            plan_only += 1
        elif new not in stationary:
            explanation = f"档位 #{old + 1} → #{new + 1}"
        else:
            explanation = "已有"
        if row.mod is None:
            explanation += " · 待匹配安装"
        desired_lines.append(f"{new + 1:02d}  {row.entry.name}  ·  {explanation}")
    return SlotComparison(
        current_lines, tuple(desired_lines), plan_only, len(missing), len(matches) - len(stationary), unknown
    )
