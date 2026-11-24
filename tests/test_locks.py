from dataclasses import replace

import pytest

from darksort.classification import classify_mod
from darksort.models import OrderRule, ProfileEntry, ProfileLock, Source
from darksort.profiles import ProfileStore, parse_profile, profile_dict, resolve_entries
from darksort.sorting import preview_order


def test_profile_round_trips_a_parked_lock_and_rejects_split_members(tmp_path, mods):
    store = ProfileStore(tmp_path)
    a, b, c = [ProfileEntry.from_mod(mod) for mod in mods]
    profile = store.new("队列", [c])
    profile.staged = [a, b]
    profile.locks = [ProfileLock("配套包", (a.id, b.id))]
    store.save(profile)
    loaded = store.load(profile.id)
    assert profile_dict(loaded) == profile_dict(profile)

    broken = replace(profile, entries=[b, c], staged=[a])
    with pytest.raises(ValueError, match="连续"):
        parse_profile(profile_dict(broken))


def test_sort_moves_a_contiguous_lock_across_a_reserved_slot(tmp_path, mods):
    mods = [replace(mods[0], name="新地区", tags=()), replace(mods[1], name="字体"), mods[2]]
    a, b, c = [ProfileEntry.from_mod(mod) for mod in mods]
    missing = ProfileEntry("未匹配", Source.LOCAL, "")
    profile = ProfileStore(tmp_path).new("联锁排序", [a, missing, b, c])
    profile.locks = [ProfileLock("字体组", (b.id, c.id))]
    classifications = {mod.key: classify_mod(mod) for mod in mods}
    preview = preview_order(profile, resolve_entries(profile.entries, mods), classifications)
    assert not preview.blockers
    assert preview.entries == (missing, b, c, a)
    assert "保持联锁相邻" in "\n".join(preview.notices)
    assert "字体组" in preview.decisions[2].reason
    profile.entries = list(preview.entries)
    repeat = preview_order(profile, resolve_entries(profile.entries, mods), classifications)
    assert repeat.entries == preview.entries


def test_ordering_rules_cannot_reverse_or_split_a_lock(tmp_path, mods):
    a, b, c = [ProfileEntry.from_mod(mod) for mod in mods]
    profile = ProfileStore(tmp_path).new("规则冲突", [a, b, c])
    profile.locks = [ProfileLock("联锁示例", (a.id, b.id))]
    resolved = resolve_entries(profile.entries, mods)
    profile.rules = [OrderRule(b.reference, a.reference)]
    result = preview_order(profile, resolved)
    assert result.entries == (a, b, c)
    assert "固定顺序" in result.blockers[0]
    profile.rules = [OrderRule(a.reference, c.reference), OrderRule(c.reference, b.reference)]
    result = preview_order(profile, resolved)
    assert result.entries == (a, b, c)
    assert "循环" in result.blockers[0]
    assert "插在联锁内部" in result.blockers[-1]
