import json

import pytest

from darksort.ai_tasks import sort_payload
from darksort.classification import classify_mod
from darksort.mod_groups import enable_group
from darksort.models import ModGroup, OrderRule, ProfileEntry, ProfileLock
from darksort.profiles import ProfileStore, resolve_entries
from darksort.sorting import preview_order


def test_groups_keep_membership_and_order_across_export_and_restore_complete_locks(tmp_path, mods):
    a, b, c = [ProfileEntry.from_mod(mod, pinned=True) for mod in mods]
    store = ProfileStore(tmp_path / "资料")
    profile = store.new("远征", [c])
    profile.staged = [a, b]
    profile.locks = [ProfileLock("需要一起移动", (a.id, b.id))]
    partial = ModGroup("局部", "#e3b574", (b,))
    bundle = ModGroup("机器人系列", "#75b9ea", (b, a))
    profile.mod_groups = [bundle, partial]
    path = tmp_path / "分组方案.json"
    store.export_file(profile, path)
    restored = store.import_file(path)
    assert restored.mod_groups == profile.mod_groups and restored.locks == profile.locks

    with pytest.raises(ValueError, match="只包含联锁"):
        enable_group(restored, partial, resolve_entries(list(partial.members), mods), 0)
    updated, skipped = enable_group(restored, bundle, resolve_entries(list(bundle.members), mods), 0)
    assert not skipped and not updated.staged
    assert [entry.id for entry in updated.entries] == [a.id, b.id, c.id]
    assert updated.locks == profile.locks and updated.mod_groups == profile.mod_groups
    assert restored.entries == [c] and restored.staged == [a, b]
    store.save(updated)
    assert store.load(updated.id).entries == [a, b, c]


def test_dynamic_rules_follow_group_tag_and_color_members_and_detect_cycles(tmp_path, mods):
    a, b, c = [ProfileEntry.from_mod(mod) for mod in mods]
    profile = ProfileStore(tmp_path).new("动态规则", [c, b, a])
    above = ModGroup("上组", "#75b9ea", (a, b))
    below = ModGroup("下组", "#bd99e8", (b, c))
    profile.mod_groups = [above, below]
    profile.rules = [OrderRule(f"group:{above.id}", f"group:{below.id}")]
    resolved = resolve_entries(profile.entries, mods)
    classifications = {mod.key: classify_mod(mod) for mod in mods}
    group_order = preview_order(profile, resolved, classifications)
    assert group_order.entries.index(a) < group_order.entries.index(c)
    assert not group_order.blockers and any("重合的成员" in line for line in group_order.notices)
    sent = json.loads(sort_payload(profile, resolved, classifications, group_order))
    assert sent["rules"] == [{"kind": "above", "first": [3], "second": [1]}]

    profile.rules = [OrderRule("tag:功能拓展", "color:#e3b574")]
    tags = {a.reference: ("功能拓展",), b.reference: ("功能拓展",)}
    colors = {c.reference: "#e3b574"}
    result = preview_order(profile, resolved, tags=tags, colors=colors)
    assert result.entries == (b, a, c)
    changed = preview_order(profile, resolved, tags={c.reference: tags[a.reference]}, colors={a.reference: "#e3b574"})
    assert changed.entries == (c, b, a)
    profile.rules.append(OrderRule(c.reference, a.reference, note="相反规则"))
    cycle = preview_order(profile, resolved, tags=tags, colors=colors)
    assert cycle.entries == (c, b, a) and any("循环" in line for line in cycle.blockers)
