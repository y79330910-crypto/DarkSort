from dataclasses import replace

from darksort.locks import entry_units, snap_boundary
from darksort.models import ModGroup, Profile, ProfileEntry, ResolvedEntry

MAX_MOD_GROUPS = 250
MAX_GROUP_MEMBERS = 20_000


def members_in_plan(group: ModGroup, entries: list[ProfileEntry]) -> dict[str, ProfileEntry]:
    references = {member.reference for member in group.members}
    members = {}
    for entry in entries:
        reference = entry.reference
        if reference in references:
            if reference in members:
                raise ValueError(f"「{entry.name}」在计划或暂存中有重复项，请先保留要用的那一份。")
            members[reference] = entry
    return members


def check_complete_locks(profile: Profile, entries) -> None:
    identities = {entry.id for entry in entries}
    for lock in profile.locks:
        if identities.intersection(lock.members) and not identities.issuperset(lock.members):
            raise ValueError(f"分组只包含联锁「{lock.title}」的一部分。把其余成员加进分组，或先解除联锁。")


def enable_group(
    profile: Profile, group: ModGroup, installed: list[ResolvedEntry], boundary: int | None = None,
) -> tuple[Profile, int]:
    """Make a single block at the target boundary while retaining entry IDs and completed locks."""
    occupied = members_in_plan(group, profile.entries + profile.staged)
    check_complete_locks(profile, occupied.values())
    locks_by_member = {member: lock for lock in profile.locks for member in lock.members}
    occupied_by_id = {entry.id: entry for entry in occupied.values()}
    selected, skipped, block = set(), 0, []
    for row in installed:
        existing = occupied.get(row.entry.reference)
        if existing:
            if existing.id in selected:
                continue
            lock = locks_by_member.get(existing.id)
            if lock:
                block.extend(occupied_by_id[identity] for identity in lock.members)
                selected.update(lock.members)
            else:
                block.append(existing)
                selected.add(existing.id)
        elif row.mod:
            block.append(ProfileEntry.from_mod(row.mod, pinned=row.entry.pinned_path))
        else:
            skipped += 1
    if not block:
        return profile, skipped

    units = entry_units(profile.entries, profile.locks)
    positions = [index for index, entry in enumerate(profile.entries) if entry.id in selected]
    if boundary is None:
        boundary = min(positions, default=len(profile.entries))
    if not 0 <= boundary <= len(profile.entries):
        raise ValueError("无效的分组插入位置。")
    boundary = snap_boundary(units, boundary)
    target = boundary - sum(index < boundary for index in positions)
    entries = [entry for entry in profile.entries if entry.id not in selected]
    entries[target:target] = block
    staged = [entry for entry in profile.staged if entry.id not in selected]
    return replace(profile, entries=entries, staged=staged), skipped
