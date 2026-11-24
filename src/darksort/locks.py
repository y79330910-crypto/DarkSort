from collections import deque

from darksort.models import ProfileEntry, ProfileLock


def lock_starts(entries: list[ProfileEntry], locks: list[ProfileLock]) -> dict[int, ProfileLock]:
    by_first = {lock.members[0]: lock for lock in locks}
    return {index: by_first[entry.id] for index, entry in enumerate(entries) if entry.id in by_first}


def entry_units(entries: list[ProfileEntry], locks: list[ProfileLock]) -> list[range]:
    starts = lock_starts(entries, locks)
    units = []
    start = 0
    while start < len(entries):
        lock = starts.get(start)
        end = start + (len(lock.members) if lock else 1)
        units.append(range(start, end))
        start = end
    return units


def expand_selection(units: list[range], indices) -> list[int]:
    selected = set(indices)
    return [index for unit in units if selected.intersection(unit) for index in unit]


def snap_boundary(units: list[range], boundary: int) -> int:
    for unit in units:
        if unit.start < boundary < unit.stop:
            return unit.start if (boundary - unit.start) * 2 <= len(unit) else unit.stop
    return boundary


def nudge_units(units: list[range], indices, direction: int) -> list[int]:
    selected = set(indices)
    occupied = {i for i, unit in enumerate(units) if selected.intersection(unit)}
    order = list(units)
    for i in sorted(occupied, reverse=direction > 0):
        neighbor = i + direction
        if 0 <= neighbor < len(order) and neighbor not in occupied:
            order[i], order[neighbor] = order[neighbor], order[i]
            occupied.remove(i)
            occupied.add(neighbor)
    return [index for unit in order for index in unit]


def place_units(units: list[range], ordered: list[range]) -> list[int]:
    """Fill the movable slots. Shift fixed units when a slot would split a lock."""
    movable = {unit.start for unit in ordered}
    positions = [index for unit in units if unit.start in movable for index in unit]
    result = list(range(sum(map(len, units))))
    offset = 0
    for unit in ordered:
        slots = positions[offset:offset + len(unit)]
        if slots[-1] - slots[0] != len(unit) - 1:
            break
        result[slots[0]:slots[-1] + 1] = unit
        offset += len(unit)
    else:
        return result

    fixed = deque(unit for unit in units if unit.start not in movable)
    result = []
    for unit in ordered:
        while fixed and len(result) + len(unit) > fixed[0].start:
            result.extend(fixed.popleft())
        result.extend(unit)
    for unit in fixed:
        result.extend(unit)
    return result
