from collections import defaultdict
from dataclasses import replace

from darksort.locks import entry_units, snap_boundary
from darksort.models import ModGroup, Profile, ProfileEntry, ProfileLock, ResolvedEntry
from darksort.search import unique_title

MAX_MOD_GROUPS = 250
MAX_GROUP_MEMBERS = 20_000


def members_in_plan(group: ModGroup, entries: list[ProfileEntry]) -> dict[str, ProfileEntry]:
    by_reference = defaultdict(list)
    by_id = {entry.id: entry for entry in entries}
    for entry in entries:
        by_reference[entry.reference].append(entry)
    members = {}
    for member in group.members:
        candidates = by_reference[member.reference]
        if (entry := by_id.get(member.id)) and entry.reference == member.reference:
            members[member.id] = entry
        elif len(candidates) == 1:
            members[member.id] = candidates[0]
        elif candidates:
            raise ValueError(f"「{member.name}」在计划中有多个安装，请先匹配这一组使用的安装。")
    return members


def groups_for_disabled_locks(profile, locks, entries):
    groups = list(profile.mod_groups)
    by_id = {entry.id: entry for entry in entries}
    existing = {frozenset(member.id for member in group.members) for group in groups if group.locked}
    for lock in locks:
        if frozenset(lock.members) not in existing:
            title = unique_title(lock.title, (group.title for group in groups))
            groups.append(ModGroup(title, "#bd99e8", tuple(by_id[mid] for mid in lock.members), locked=True))
    return groups


def check_complete_locks(profile: Profile, entries) -> None:
    identities = {entry.id for entry in entries}
    for lock in profile.locks:
        if identities.intersection(lock.members) and not identities.issuperset(lock.members):
            raise ValueError(f"分组只包含联锁「{lock.title}」的一部分。把其余成员加进分组，或先解除联锁。")


def enable_group(
    profile: Profile, group: ModGroup, installed: list[ResolvedEntry], boundary: int | None = None,
) -> tuple[Profile, int]:
    """Make a single block at the target boundary while retaining entry IDs and completed locks."""
    occupied = members_in_plan(group, profile.entries)
    check_complete_locks(profile, occupied.values())
    locks_by_member = {member: lock for lock in profile.locks for member in lock.members}
    occupied_by_id = {entry.id: entry for entry in occupied.values()}
    selected, skipped, block = set(), 0, []
    used_ids = {entry.id for entry in profile.entries}
    used_installs = {row.mod.key for row in installed if row.mod and row.entry.id in occupied}
    for row in installed:
        existing = occupied.get(row.entry.id)
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
        elif row.mod and row.mod.key not in used_installs:
            entry = ProfileEntry.from_mod(row.mod, pinned=row.entry.pinned_path)
            if row.entry.id not in used_ids:
                entry = replace(entry, id=row.entry.id)
            block.append(entry)
            used_ids.add(entry.id)
            used_installs.add(row.mod.key)
        else:
            skipped += 1
    if group.locked and (skipped or len(block) != len(group.members)):
        raise ValueError(f"联锁分组「{group.title}」需要匹配全部组员才能加入计划。")
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
    locks = profile.locks
    if group.locked:
        locks = [lock for lock in locks if selected.isdisjoint(lock.members)]
        locks.append(ProfileLock(group.title, tuple(entry.id for entry in block)))
    return replace(profile, entries=entries, locks=locks), skipped
