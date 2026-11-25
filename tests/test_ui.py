import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QListWidget, QMenu, QToolButton

from darksort.models import ModGroup, OrderRule, ProfileEntry, ProfileGroup, ScanResult, Source
from darksort.save_bindings import SaveBindings
from darksort.scanning.discovery import Discovery
from darksort.search import ModLocation
from darksort.settings import DEFAULT_THEME, Settings
from darksort.ui.group_panel import ModGroupDialog
from darksort.ui.main_window import MainWindow
from darksort.ui.mod_list import ROLE, GroupDragHandle, column_rects
from darksort.ui.plan_dialogs import HistoryDialog


def make_window(qtbot, tmp_path, mods):
    window = MainWindow(tmp_path, auto_scan=False)
    qtbot.addWidget(window, before_close_func=lambda w: setattr(w, "maybe_save", lambda: True))
    window.on_scan_completed(Discovery(), ScanResult(mods=mods))
    window.show()
    return window


def filter_list(bar, text):
    bar.set_mode("filter")
    bar.query.setText(text)
    bar.advance()


def test_dual_lists_batch_add_search_reorder_remove_and_save(qtbot, tmp_path, mods):
    window = make_window(qtbot, tmp_path, mods)
    assert window.library_list.count() == 3
    window.library_list.item(0).setSelected(True)
    window.library_list.item(1).setSelected(True)
    window.add_selected()
    assert len(window.profile.entries) == 2
    assert window.library_list.count() == 1
    assert window.dirty
    initial = list(window.profile.entries)
    window.profile_list.clearSelection()
    window.profile_list.item(1).setSelected(True)
    window.nudge(-1)
    assert window.profile.entries == initial[::-1]
    window.save_profile()
    assert not window.dirty
    assert window.store.load(window.profile.id).entries == initial[::-1]
    filter_list(window.library_search, "not present")
    assert window.library_list.count() == 0
    window.library_search.clear_filters()
    assert window.library_list.count() == 1
    window.profile_list.clearSelection()
    window.profile_list.item(0).setSelected(True)
    window.remove_selected()
    assert len(window.profile.entries) == 1
    # Leave the test window clean to avoid the interactive close prompt.
    window.save_profile()


def test_drop_routes_move_and_preserves_filtered_order(qtbot, tmp_path, mods):
    window = make_window(qtbot, tmp_path, mods)
    window.handle_drop("profile", {"side": "library", "values": [m.key for m in mods]}, 0)
    original = list(window.profile.entries)
    window.handle_drop("profile", {"side": "profile", "values": [0, 2]}, 3)
    assert window.profile.entries == [original[1], original[0], original[2]]
    before = list(window.profile.entries)
    filter_list(window.profile_search, "甲")
    window.handle_drop("profile", {"side": "profile", "values": [0]}, 3)
    assert window.profile.entries == before
    window.save_profile()


def test_background_scan_keeps_qt_event_loop_responsive(qtbot, tmp_path, monkeypatch):
    import darksort.ui.worker as worker_module

    def slow_scan(*args, **kwargs):
        time.sleep(0.25)
        return ScanResult()

    monkeypatch.setattr(worker_module, "discover", lambda _: Discovery())
    monkeypatch.setattr(worker_module, "scan", slow_scan)
    window = MainWindow(tmp_path, auto_scan=False)
    qtbot.addWidget(window)
    timer_ticks = []
    timer = QTimer(window)
    timer.setInterval(15)
    timer.timeout.connect(lambda: timer_ticks.append(True))
    timer.start()
    window.start_scan()
    qtbot.waitUntil(lambda: window.worker is None, timeout=3000)
    assert len(timer_ticks) >= 5
    assert window.scan_button.isEnabled()


def test_cancelled_scan_preserves_previous_results(qtbot, tmp_path, mods):
    window = make_window(qtbot, tmp_path, mods)
    window.on_scan_completed(Discovery(), ScanResult(cancelled=True))
    assert window.mods == mods
    assert window.library_list.count() == 3


def test_switch_profile_and_cancel_unsaved_switch(qtbot, tmp_path, mods, monkeypatch):
    window = make_window(qtbot, tmp_path, mods)
    first_id = window.profile.id
    second = window.store.new("第二个")
    window.populate_profiles()
    window.add_keys([mods[0].key])
    monkeypatch.setattr(window, "maybe_save", lambda: False)
    window.profile_combo.setCurrentIndex(window.profile_combo.findData(second.id))
    assert window.profile.id == first_id
    assert window.profile_combo.currentData() == first_id
    window.save_profile()
    monkeypatch.setattr(window, "maybe_save", lambda: True)
    window.profile_combo.setCurrentIndex(window.profile_combo.findData(second.id))
    assert window.profile.id == second.id
    assert window.profile.entries == []
    window.profile_combo.setCurrentIndex(window.profile_combo.findData(first_id))
    assert len(window.profile.entries) == 1
    assert window.profile_list.item(0).data(ROLE)["number"] == 1


def test_keyboard_save_shortcut(qtbot, tmp_path, mods):
    window = make_window(qtbot, tmp_path, mods)
    window.add_keys([mods[0].key])
    qtbot.waitExposed(window)
    window.raise_()
    window.activateWindow()
    qtbot.waitUntil(window.isActiveWindow)
    query = window.library_search.query
    query.setFocus()
    qtbot.waitUntil(lambda: QApplication.focusWidget() is query)
    QApplication.processEvents()
    qtbot.keyClick(
        query, Qt.Key.Key_S, modifier=Qt.KeyboardModifier.ControlModifier
    )
    qtbot.waitUntil(lambda: not window.dirty)


@pytest.mark.parametrize(
    "choice,allowed,dirty", [("取消", False, True), ("放弃改动", True, True), ("保存", True, False)]
)
def test_unsaved_dialog_decisions(qtbot, tmp_path, mods, choice, allowed, dirty):
    window = make_window(qtbot, tmp_path, mods)
    window.add_keys([mods[0].key])

    def choose():
        dialog = QApplication.activeModalWidget()
        next(button for button in dialog.buttons() if button.text() == choice).click()

    QTimer.singleShot(0, choose)
    assert window.maybe_save() is allowed
    assert window.dirty is dirty


def test_export_cannot_write_into_game_directory(qtbot, tmp_path, mods, monkeypatch):
    window = make_window(qtbot, tmp_path / "data", mods)
    game = tmp_path / "game"
    game.mkdir()
    window.discovery = Discovery(games=[game])
    destination = game / "persist.game.json"
    destination.write_bytes(b"original game bytes")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(destination), ""))
    errors = []
    monkeypatch.setattr(window, "error", lambda *a: errors.append(a))
    window.export_profile()
    assert destination.read_bytes() == b"original game bytes"
    assert errors


def test_group_collapse_preserves_order_and_removing_anchor_keeps_group(qtbot, tmp_path, mods):
    window = make_window(qtbot, tmp_path, mods)
    window.add_keys([mod.key for mod in mods])
    draft = list(window.profile.entries)
    window.profile.groups = [ProfileGroup(draft[0].id, "一组")]
    window.render_lists()
    group = window.profile_list.item(0)
    target = window.profile_list.visualItemRect(group).center()
    qtbot.mouseClick(window.profile_list.viewport(), Qt.MouseButton.LeftButton, pos=target)
    assert window.profile_list.count() == 1
    assert window.profile.entries == draft
    filter_list(window.profile_search, mods[1].name)
    assert window.profile_list.item(1).data(ROLE)["number"] == 2
    window.remove_indices([0])
    assert window.profile.groups == [ProfileGroup(draft[1].id, "一组")]


def test_sort_preview_creates_snapshot_before_changing_the_working_order(qtbot, tmp_path, mods, monkeypatch):
    window = make_window(qtbot, tmp_path, mods)
    window.add_keys([mod.key for mod in mods])
    draft = list(window.profile.entries)
    window.profile.rules = [OrderRule(draft[2].reference, draft[0].reference)]
    monkeypatch.setattr(
        "darksort.ui.main_window.SortPreviewDialog.exec", lambda _: QDialog.DialogCode.Accepted
    )
    window.sort_profile(rules_only=True)
    snapshots, errors = window.history.list(window.profile.id)
    assert not errors
    assert snapshots[0].profile.entries == draft
    assert window.profile.entries == [draft[1], draft[2], draft[0]]
    assert window.store.load(window.profile.id).entries == []
    assert window.save_profile()


def test_theme_preview_reverts_on_cancel_and_persists_on_accept(qtbot, tmp_path, mods, monkeypatch):
    window = make_window(qtbot, tmp_path, mods)

    def cancelling(dialog):
        dialog.choose("paper")
        assert QApplication.instance().property("darksortTheme") == "paper"
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr("darksort.ui.main_window.AppearanceDialog.exec", cancelling)
    window.edit_appearance()
    assert QApplication.instance().property("darksortTheme") == DEFAULT_THEME

    def accepting(dialog):
        dialog.choose("ice")
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr("darksort.ui.main_window.AppearanceDialog.exec", accepting)
    window.edit_appearance()
    assert Settings.load(tmp_path).theme == "ice"
    assert QApplication.instance().property("darksortTheme") == "ice"


def test_list_searches_keep_independent_text_filters_and_plan_order(qtbot, tmp_path, mods):
    window = make_window(qtbot, tmp_path, mods)
    window.add_keys([mods[0].key, mods[1].key])
    draft = list(window.profile.entries)
    filter_list(window.library_search, "无匹配")
    assert window.library_list.count() == 0
    assert window.profile_list.count() == 2
    window.library_search.set_mode("locate")
    window.library_search.query.setText(mods[2].name)
    window.library_search.advance()
    assert window.library_list.count() == 1
    assert window.library_list.item(0).data(ROLE)["temporary"]
    window.add_visible()
    assert window.profile.entries == draft
    filter_list(window.profile_search, mods[1].name)
    assert window.profile_list.count() == 1
    window.library_search.set_mode("filter")
    assert window.library_search.query.text() == "无匹配"
    window.library_search.clear_filters()
    assert window.profile_list.count() == 1


def test_locate_search_reveals_a_match_in_collapsed_group(qtbot, tmp_path, mods):
    window = make_window(qtbot, tmp_path, mods)
    window.add_keys([mod.key for mod in mods])
    original = list(window.profile.entries)
    window.profile.groups = [ProfileGroup(original[0].id, "成组")]
    window.collapsed_groups.add(original[0].id)
    window.render_lists()
    bar = window.profile_search
    bar.query.setText(mods[1].name)
    assert window.profile_list.count() == 1
    assert bar.count.text() == "1 命中"

    qtbot.keyClick(bar.query, Qt.Key.Key_Return)
    assert window.profile_list.count() == 4
    assert window.profile_list.currentItem().data(ROLE)["entry_id"] == original[1].id
    assert window.profile_list.search_marks == {1}
    assert window.profile.entries == original
    assert bar.count.text() == "1/1"
    assert not window.profile_list.reorder_blocked


def test_locate_cycles_both_directions_without_changing_batch_selection(qtbot, tmp_path, mods):
    window = make_window(qtbot, tmp_path, mods)
    window.add_keys([mod.key for mod in mods])
    window.profile_list.item(0).setSelected(True)
    window.profile_list.item(2).setSelected(True)
    entries = window.profile.entries
    query = window.profile_search.query
    qtbot.waitExposed(window)
    window.activateWindow()
    query.setFocus()
    qtbot.waitUntil(lambda: QApplication.focusWidget() is query)
    query.setText("ｍｏｄ")
    qtbot.keyClick(query, Qt.Key.Key_Return)
    assert window._detail_target == ModLocation("profile", entries[0].id)
    qtbot.keyClick(query, Qt.Key.Key_Return, modifier=Qt.KeyboardModifier.ShiftModifier)
    assert window._detail_target == ModLocation("profile", entries[2].id)
    assert window.selected_profile_indices() == [0, 2]
    assert window.profile_search.count.text() == "3/3"


def test_selected_skin_reveals_its_hero_and_keeps_the_plan(qtbot, tmp_path, mods):
    hero = replace(mods[0], name="辉石战士", hero_definitions=("glint",))
    skin = replace(mods[1], name="辉石战士皮肤", skin_targets=("glint",))
    window = make_window(qtbot, tmp_path, [hero, skin, mods[2]])
    window.add_keys([hero.key])
    origin = ModLocation("library", skin.key)
    window.navigate(origin)
    item = window.utility.relations.item(0)
    assert "对应人物" in item.text()
    assert window.profile_list.relation_marks == {0}
    window.profile_search.set_condition("status", "missing")
    assert window.profile_list.count() == 0

    window.reveal_relation(window.utility.relations.item(0).data(ROLE))
    assert window.profile_search.conditions["status"] == "missing"
    assert window.profile_list.item(0).data(ROLE)["temporary"]
    assert window.detail_title.text() == hero.name
    assert len(window.profile.entries) == 1
    assert window._relation_anchor == origin
    assert window.utility.subject.text() == skin.name
    window.return_to_relation_origin()
    assert window.detail_title.text() == skin.name
    assert window.profile_list.count() == 0
    assert window._temporary_target is None


def test_relation_uses_entry_id_after_reorder_and_restores_collapsed_group(qtbot, tmp_path, mods):
    window = make_window(qtbot, tmp_path, mods)
    window.add_keys([mod.key for mod in mods])
    first, target, group = window.profile.entries
    window.profile.rules = [OrderRule(first.reference, target.reference, "requires")]
    source = ModLocation("profile", first.id)
    window.render_lists()
    window.navigate(source)
    saved_card = window.utility.relations.item(0).data(ROLE)
    window.profile.entries = [first, group, target]
    window.profile.groups = [ProfileGroup(group.id, "候选组")]
    window.collapsed_groups = {group.id}
    window.render_lists()
    assert window.profile_list.count() == 2
    window.reveal_relation(saved_card)
    assert window.profile_list.count() == 4
    assert window.profile_list.currentItem().data(ROLE)["entry_id"] == target.id
    assert window.utility.subject.text() == first.name
    window.return_to_relation_origin()
    assert window.collapsed_groups == {group.id}
    assert window.profile_list.count() == 2
    assert window.profile.entries == [first, group, target]


def test_offscreen_relation_indicator_jumps_and_preserves_previous_scroll(qtbot, tmp_path, mods):
    installed = [
        replace(
            mods[0], name=f"样例 Mod {i:02}", path=str(tmp_path / f"local-{i}"),
            source=Source.LOCAL, workshop_id=None,
        )
        for i in range(36)
    ]
    window = make_window(qtbot, tmp_path, installed)
    window.resize(1030, 670)
    window.add_keys([mod.key for mod in installed])
    first = window.profile.entries[5]
    last = window.profile.entries[-1]
    window.profile.rules = [OrderRule(first.reference, last.reference, "requires")]
    window.render_lists()
    window.navigate(ModLocation("profile", first.id))
    view = window.profile_list
    scrollbar = view.verticalScrollBar()
    scrollbar.setValue(58)
    qtbot.waitUntil(lambda: any("下方有" in tooltip for _, _, tooltip in view.relationship_hits))
    pill = next(rect for rect, identity, tip in view.relationship_hits if identity == last.id and "下方有" in tip)
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=pill.center())
    assert window.detail_title.text() == last.name
    assert scrollbar.value() > 58
    assert window.utility.subject.text() == first.name
    qtbot.waitUntil(lambda: view.viewport().rect().contains(view.visualItemRect(view.currentItem())))
    window.return_to_relation_origin()
    assert scrollbar.value() == 58
    assert window.detail_title.text() == first.name
    assert not window.selected_profile_indices()


def test_global_search_prefers_exact_id_and_reveals_staged_entry(qtbot, tmp_path, mods, monkeypatch):
    installed = [mods[0], mods[1], replace(mods[2], description="与 1002 同时使用")]
    window = make_window(qtbot, tmp_path, installed)
    entry = ProfileEntry.from_mod(mods[1])
    unresolved = ProfileEntry("离线暂存", Source.LOCAL, str(tmp_path / "offline"))
    window.profile.staged = [entry, unresolved]
    window.render_lists()
    window.utility.staging.item(1).setSelected(True)
    target = ModLocation("staging", entry.id)

    def choose(dialog):
        dialog.query.setText("１００２")
        assert dialog.results.count() == 2
        assert dialog.results.item(0).data(ROLE) == target
        dialog.choose(dialog.results.item(0))
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr("darksort.ui.main_window.GlobalSearchDialog.exec", choose)
    qtbot.waitExposed(window)
    window.activateWindow()
    query = window.library_search.query
    query.setFocus()
    qtbot.waitUntil(lambda: QApplication.focusWidget() is query)
    qtbot.keyClick(query, Qt.Key.Key_F, modifier=Qt.KeyboardModifier.ControlModifier)
    qtbot.waitUntil(lambda: window._detail_target == target)
    assert window.utility.tabs.currentWidget() == window.utility.stage_page
    assert window.utility.staging.currentItem().data(ROLE) == (entry.id,)
    assert window.stage_members() == {unresolved.id}
    assert window.detail_title.text() == entry.name
    assert not window.profile.entries
    assert window.profile.staged == [entry, unresolved]

    opened, copied = [], []
    monkeypatch.setattr("darksort.ui.workshop.steam_protocol_registered", lambda: False)

    def open_link(url):
        opened.append(url)
        return True

    monkeypatch.setattr("darksort.ui.workshop.dispatch_url", open_link)
    monkeypatch.setattr(QApplication, "clipboard", lambda: SimpleNamespace(setText=copied.append))
    window.workshop_button.click()
    window.copy_workshop_id()
    assert opened == ["https://steamcommunity.com/sharedfiles/filedetails/?id=1002"]
    assert copied == ["1002"]


def test_ambiguous_relation_lists_distinct_install_candidates(qtbot, tmp_path, mods, monkeypatch):
    hero = replace(mods[0], name="辉石战士", hero_definitions=("glint",))
    duplicate = replace(hero, path=str(tmp_path / "另一份"))
    skin = replace(mods[1], name="辉石战士皮肤", skin_targets=("glint",))
    window = make_window(qtbot, tmp_path, [hero, duplicate, skin])
    window.navigate(ModLocation("library", skin.key))
    relation = window.utility.relations.item(0).data(ROLE)
    assert relation.location == "匹配多项"
    assert relation.target is None
    assert {choice.target.identity for choice in relation.choices} == {hero.key, duplicate.key}
    assert window.detail_title.text() == skin.name

    def choose(dialog):
        choices = dialog.findChild(QListWidget, "globalResults")
        index = next(i for i in range(choices.count()) if choices.item(i).data(ROLE).identity == duplicate.key)
        choices.setCurrentRow(index)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr("darksort.ui.main_window.QDialog.exec", choose)
    window.reveal_relation(relation)
    assert window._detail_target == ModLocation("library", duplicate.key)
    assert window._relation_anchor == ModLocation("library", skin.key)


def test_association_to_staging_keeps_return_available(qtbot, tmp_path, mods):
    window = make_window(qtbot, tmp_path, mods)
    window.add_keys([mods[0].key])
    source = window.profile.entries[0]
    staged = ProfileEntry.from_mod(mods[1])
    window.profile.staged.append(staged)
    window.profile.rules.append(OrderRule(source.reference, staged.reference, "requires"))
    window.render_lists()
    window.navigate(ModLocation("profile", source.id))
    relation = window.utility.relations.item(0).data(ROLE)
    assert relation.location == "暂存篮"

    window.reveal_relation(relation)
    assert window.utility.tabs.currentWidget() == window.utility.stage_page
    assert window.utility.relation_navigation.isVisible()
    assert not window.stage_members()
    assert window.detail_title.text() == staged.name
    window.return_to_relation_origin()
    assert window.utility.tabs.currentWidget() == window.utility.relation_page
    assert not window.utility.relation_navigation.isVisible()
    assert window.detail_title.text() == source.name


def test_history_diff_hides_cascading_position_changes(qtbot, tmp_path, mods):
    window = make_window(qtbot, tmp_path, mods)
    window.add_keys([mod.key for mod in mods])
    snapshot = window.history.capture(window.profile, "完整方案")
    window.remove_indices([0])
    dialog = HistoryDialog(window.history, window.profile, window, snapshot_id=snapshot.id)
    qtbot.addWidget(dialog)
    assert dialog.diff.rowCount() == 1
    assert dialog.diff.item(0, 2).text() == "加入"
    assert "顺延 2" in dialog.diff_summary.text()
    dialog.hide_unchanged.setChecked(False)
    assert dialog.diff.rowCount() == 3
    dialog.restore_button.click()
    assert dialog.restored.entries == snapshot.profile.entries
    assert len(window.profile.entries) == 2


def test_lock_moves_as_a_unit_and_can_be_disabled_and_enabled(qtbot, tmp_path, mods, monkeypatch):
    window = make_window(qtbot, tmp_path, mods)
    window.add_keys([mod.key for mod in mods])
    a, b, c = window.profile.entries
    monkeypatch.setattr("darksort.ui.main_window.QInputDialog.getText", lambda *a, **k: ("配套包", True))
    window.profile_list.item(0).setSelected(True)
    window.profile_list.item(2).setSelected(True)
    window.lock_selected()
    assert window.profile.entries == [a, c, b]
    assert window.profile.locks[0].members == (a.id, c.id)

    window.profile_list.clearSelection()
    window.profile_list.item(1).setSelected(True)
    assert window.profile_list.lock_selection == {0}
    window.handle_drop("profile", {"side": "profile", "values": [1]}, 3)
    assert window.profile.entries == [b, a, c]
    window.nudge(-1)
    assert window.profile.entries == [a, c, b]

    window.remove_selected()
    assert window.profile.entries == [b]
    assert window.profile.staged == [a, c]
    assert window.utility.staging.count() == 1
    assert window.utility.stage_activate.isEnabled()
    window.save_profile()
    assert window.store.load(window.profile.id).locks[0].members == (a.id, c.id)
    window.add_staged()
    assert not window.profile.staged
    assert window.profile.entries == [b, a, c]
    assert window.profile.locks[0].members == (a.id, c.id)


def open_mod_context(qtbot, view, row):
    point = view.visualItemRect(view.item(row)).center()
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.RightButton, pos=point)
    menu = view.findChild(QMenu, "modContextMenu")
    if menu is None or not menu.isVisible():
        event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, view.viewport().mapToGlobal(point))
        QApplication.sendEvent(view.viewport(), event)
        menu = view.findChild(QMenu, "modContextMenu")
    assert menu is not None and menu.isVisible()
    return menu


def menu_action(menu, text):
    return next(action for action in menu.actions() if action.text() == text)


def test_plan_context_adds_rule_using_exact_target_search(qtbot, tmp_path, mods, monkeypatch):
    first = mods[0]
    original = "Camp Helper"
    second = replace(mods[1], name=original)
    same_name = replace(mods[2], name=original)
    window = make_window(qtbot, tmp_path, [first, second, same_name])
    window.add_keys([first.key, second.key])
    window.save_mod_display({second.reference: "营地助手"}, {})
    window.render_lists()

    def choose(dialog):
        assert dialog.rule_target
        assert first.reference not in {record.target.identity for record in dialog.records}
        dialog.query.setText(original)
        captions = [dialog.results.item(index).text() for index in range(dialog.results.count())]
        assert len(captions) == 2
        assert "营地助手" in captions[0] and "计划 #2" in captions[0] and second.path in captions[0]
        assert "可用" in captions[1] and same_name.path in captions[1]
        for query in (
            "营地助手",
            "１００２",
            "steam://url/CommunityFilePage/1002",
            "https://steamcommunity.com/sharedfiles/filedetails/?id=1002&searchtext=camp",
        ):
            dialog.query.setText(query)
            assert dialog.results.count() == 1
            assert dialog.results.item(0).data(ROLE).identity == second.reference
        dialog.choose(dialog.results.item(0))
        return dialog.result()

    def add(dialog):
        assert dialog.selected == [(first.reference,), ()]
        dialog.show()
        target_button = dialog.target_buttons[1]
        assert target_button.isVisible() and target_button.width() >= 160
        qtbot.mouseClick(target_button, Qt.MouseButton.LeftButton)
        assert dialog.selected == [(first.reference,), (second.reference,)]
        dialog.accept_new_rule()
        return dialog.result()

    monkeypatch.setattr("darksort.ui.search.GlobalSearchDialog.exec", choose)
    monkeypatch.setattr("darksort.ui.plan_dialogs.RuleDialog.exec", add)
    menu = open_mod_context(qtbot, window.profile_list, 0)
    menu_action(menu, "添加排序规则…").trigger()
    menu.hide()
    assert window.profile.rules == [OrderRule(first.reference, second.reference)]
    window.undo_stack.undo()
    assert not window.profile.rules


def test_rule_targets_shift_click_individually_across_searches(qtbot, tmp_path, mods, monkeypatch):
    source, first, middle = mods
    last = replace(first, name="其他配套", workshop_id="1003", path=str(tmp_path / "1003"))
    installed = [source, first, middle, last]
    window = make_window(qtbot, tmp_path, installed)
    window.add_keys([mod.key for mod in installed])

    def choose(dialog):
        dialog.show()
        view = dialog.results

        def shift_click(row):
            qtbot.mouseClick(
                view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier,
                pos=view.visualItemRect(view.item(row)).center(),
            )

        shift_click(0)
        shift_click(2)
        assert [view.item(row).checkState() for row in range(3)] == [
            Qt.CheckState.Checked, Qt.CheckState.Unchecked, Qt.CheckState.Checked,
        ]
        shift_click(2)
        assert view.item(2).checkState() == Qt.CheckState.Unchecked
        shift_click(0)
        assert not dialog.jump.isEnabled()
        shift_click(0)
        dialog.query.setText(last.name)
        assert view.count() == 1 and view.item(0).checkState() == Qt.CheckState.Unchecked
        shift_click(0)
        dialog.query.setText("没有匹配项")
        assert view.count() == 0 and dialog.jump.isEnabled() and "已选 2 项" in dialog.summary.text()
        dialog.jump.click()
        return dialog.result()

    def add(dialog):
        dialog.target_buttons[1].click()
        assert dialog.selected == [(source.reference,), (first.reference, last.reference)]
        assert "2 组" in dialog.add.text()
        dialog.accept_new_rule()
        return dialog.result()

    monkeypatch.setattr("darksort.ui.search.GlobalSearchDialog.exec", choose)
    monkeypatch.setattr("darksort.ui.plan_dialogs.RuleDialog.exec", add)
    window.edit_rules(first=source.reference)
    assert window.profile.rules == [
        OrderRule(source.reference, first.reference),
        OrderRule(source.reference, last.reference),
    ]


def test_linked_mod_folder_and_metadata_actions_follow_clicked_row(qtbot, tmp_path, mods, monkeypatch):
    window = make_window(qtbot, tmp_path, mods)
    window.add_keys([mod.key for mod in mods])
    view = window.profile_list
    view.item(0).setSelected(True)
    view.item(1).setSelected(True)
    window.lock_selected(quick=True)
    view.clearSelection()
    opened, tagged = [], []
    monkeypatch.setattr("darksort.ui.main_window.QDesktopServices.openUrl", lambda url: opened.append(url.toLocalFile()) or True)
    monkeypatch.setattr(window, "change_tags", lambda selected, **_: tagged.extend(selected))

    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualItemRect(view.item(1)).center())
    assert window.profile_selection.text() == "2 已选"
    assert window._detail_mod == mods[1]
    window.open_folder_button.click()

    menu = open_mod_context(qtbot, view, 0)
    assert window._detail_mod == mods[0] and window.selected_profile_indices() == [1]
    menu_action(menu, "打开 Mod 文件夹").trigger()
    menu_action(menu, "编辑标签…").trigger()
    assert menu_action(menu, "打开创意工坊").isEnabled()
    assert menu_action(menu, "匹配此 Mod…").isEnabled()
    menu.hide()
    assert [Path(path) for path in opened] == [Path(mods[1].path), Path(mods[0].path)]
    assert tagged == [mods[0]]
    window.remove_selected()
    assert [row.mod for row in window.staged_resolved] == mods[:2]


def test_undo_redo_restores_full_link_and_tracks_saved_state(qtbot, tmp_path, mods):
    window = make_window(qtbot, tmp_path, mods)
    assert not window.undo_action.isEnabled()
    window.add_keys([mod.key for mod in mods])
    count = window.undo_stack.count()
    window.handle_drop("profile", {"side": "profile", "values": [0]}, 0)
    assert window.undo_stack.count() == count
    view = window.profile_list
    view.item(0).setSelected(True)
    view.item(1).setSelected(True)
    window.lock_selected(quick=True)
    initial = [entry.id for entry in window.profile.entries]
    lock, = window.profile.locks
    window.remove_indices([1])
    assert [entry.id for entry in window.profile.staged] == initial[:2]
    assert window.save_profile() and not window.dirty

    qtbot.waitExposed(window)
    window.raise_()
    window.activateWindow()
    view.setFocus()
    qtbot.waitUntil(lambda: window.isActiveWindow() and view.hasFocus())
    qtbot.keyClick(view, Qt.Key.Key_Z, modifier=Qt.KeyboardModifier.ControlModifier)
    assert window.dirty and [entry.id for entry in window.profile.entries] == initial
    assert not window.profile.staged and window.profile.locks == [lock]
    qtbot.keyClick(view, Qt.Key.Key_Z, modifier=Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert not window.dirty and [entry.id for entry in window.profile.staged] == initial[:2]
    qtbot.keyClick(view, Qt.Key.Key_Z, modifier=Qt.KeyboardModifier.ControlModifier)
    qtbot.keyClick(view, Qt.Key.Key_Y, modifier=Qt.KeyboardModifier.ControlModifier)
    assert not window.dirty
    window.undo_action.trigger()
    window.remove_indices([2])
    assert not window.redo_action.isEnabled()
    window.activate_profile(window.store.new("另一方案"))
    assert not window.undo_action.isEnabled() and not window.redo_action.isEnabled()


def test_share_preview_creates_independent_plan_and_keeps_unmatched_row(qtbot, tmp_path, mods, monkeypatch):
    window = make_window(qtbot, tmp_path / "data", mods)
    window.add_keys([mods[2].key, mods[0].key])
    source_id = window.profile.id
    path = tmp_path / "分享.darksort.zip"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(path), ""))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    window.export_share_package()
    assert path.is_file() and window.dirty
    previews = []

    def confirm(_parent, _title, summary, _action):
        previews.append(summary)
        return len(previews) == 2

    monkeypatch.setattr("darksort.ui.main_window.confirmation_dialog", confirm)
    monkeypatch.setattr(window, "maybe_save", lambda: True)
    window.import_share_package()
    assert window.profile.id == source_id and len(window.store.list()[0]) == 1
    window.import_share_package()
    assert window.profile.id != source_id and not window.dirty
    assert "1 已匹配 · 1 待匹配" in previews[0]
    assert [row.mod for row in window.resolved] == [None, mods[0]]
    assert window.store.load(source_id).entries == []


def test_load_and_match_group_template_as_undoable_draft(qtbot, tmp_path, mods, monkeypatch):
    window = make_window(qtbot, tmp_path / "data", mods)
    group = ModGroup("皮肤组", "#bd99e8", (ProfileEntry.from_mod(mods[2]), ProfileEntry.from_mod(mods[1])))
    assert window.save_mod_group(group)
    file = tmp_path / "模板.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(file), ""))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(file), ""))
    monkeypatch.setattr("darksort.ui.main_window.confirmation_dialog", lambda *a: True)
    window.export_group_template(group.id)
    assert file.is_file()
    window.activate_profile(window.store.new("新方案"))
    window.import_group_template()
    imported, = window.profile.mod_groups
    assert imported.id != group.id and imported.color == group.color and not window.profile.entries
    window.undo_action.trigger()
    assert not window.profile.mod_groups and not window.dirty
    window.redo_action.trigger()
    assert window.profile.mod_groups == [imported] and window.dirty
    monkeypatch.setattr(window, "choose_installed_mod", lambda _: mods[2])
    window.relink_group_member(imported.id, imported.members[0].reference)
    window.apply_mod_group(imported.id, "profile")
    assert [row.mod for row in window.resolved] == [mods[2], mods[1]]


def test_workshop_button_falls_back_and_unmatched_mod_has_browser_and_copy_actions(qtbot, tmp_path, mods, monkeypatch):
    window = make_window(qtbot, tmp_path, mods)
    entry = ProfileEntry("待匹配 Mod", Source.WORKSHOP, "", workshop_id="123456789")
    window.profile.entries = [entry]
    window.render_lists()
    window.navigate(ModLocation("profile", entry.id), select=True)
    assert window.workshop_button.isVisible()
    opened, copied = [], []

    def open_link(url):
        opened.append(url)
        return url.startswith("https://")

    monkeypatch.setattr("darksort.ui.workshop.steam_protocol_registered", lambda: True)
    monkeypatch.setattr("darksort.ui.workshop.dispatch_url", open_link)
    monkeypatch.setattr(QApplication, "clipboard", lambda: SimpleNamespace(setText=copied.append))
    website = "https://steamcommunity.com/sharedfiles/filedetails/?id=123456789"
    client = "steam://url/CommunityFilePage/123456789"

    window.workshop_button.click()
    assert opened == [client, website] and "浏览器" in window.statusBar().currentMessage()
    menu_action(window.workshop_button.menu(), "在浏览器打开工坊").trigger()
    assert opened == [client, website, website]
    menu = open_mod_context(qtbot, window.profile_list, 0)
    menu_action(menu, "打开创意工坊").trigger()
    menu_action(menu, "复制工坊链接").trigger()
    menu.hide()
    assert opened[-2:] == [client, website] and copied == [website]


def test_saved_group_can_be_dragged_and_toggled_and_marks_shared_members(qtbot, tmp_path, mods, monkeypatch):
    window = make_window(qtbot, tmp_path, mods)
    window.add_keys([mods[2].key])
    view = window.library_list
    for index in range(view.count()):
        view.item(index).setSelected(True)
    library_order = window.selected_library_keys()

    def name_group(dialog):
        dialog.title.setText("机器人系列")
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ModGroupDialog, "exec", name_group)
    menu = open_mod_context(qtbot, view, 0)
    submenu = menu_action(menu, "分组管理").menu()
    menu_action(submenu, "新建分组…").trigger()
    menu.hide()
    group, = window.profile.mod_groups
    shared = ModGroup("玩法拓展", "#bd99e8", (group.members[0],))
    assert window.save_mod_group(shared)
    marks = view.item(0).data(ROLE)["group_colors"]
    assert marks == (group.color, shared.color)
    shelf = window.utility.group_shelf
    shelf.search.setText("机器人")
    assert shelf.tree.topLevelItemCount() == 1
    item = shelf.tree.topLevelItem(0)
    assert item.childCount() == 2 and item.isExpanded()
    handle = shelf.tree.itemWidget(item, 0).findChild(GroupDragHandle)
    plan = window.profile_list
    destination = plan

    class DropOnList:
        def __init__(self, source):
            self.source = source

        def setMimeData(self, mime):
            self.mime = mime

        def setPixmap(self, _pixmap):
            pass

        def setHotSpot(self, _hotspot):
            pass

        def exec(self, _action):
            rect = destination.visualItemRect(destination.item(0)) if destination.count() else destination.viewport().rect()
            accepted = []
            destination.dropEvent(SimpleNamespace(
                source=lambda: self.source, mimeData=lambda: self.mime,
                position=lambda: QPointF(rect.center().x(), rect.top() + 1),
                acceptProposedAction=lambda: accepted.append(True), ignore=lambda: None,
            ))
            assert accepted == [True]

    monkeypatch.setattr("darksort.ui.mod_list.QDrag", DropOnList)

    def drag_group(grip):
        grip.origin = QPoint(0, 0)
        grip.mouseMoveEvent(SimpleNamespace(
            position=lambda: QPointF(60, 0), buttons=lambda: Qt.MouseButton.LeftButton,
        ))

    drag_group(handle)
    qtbot.waitUntil(lambda: len(window.profile.entries) == 3)
    assert [row.mod.key for row in window.resolved] == [*library_order, mods[2].key]
    header = shelf.tree.itemWidget(shelf.tree.topLevelItem(0), 0)
    shortcut = next(control for control in header.findChildren(QToolButton)
                    if control.accessibleName() == "整组移出计划")
    shortcut.click()
    assert [row.mod.key for row in window.resolved] == [mods[2].key]
    assert window.library_list.count() == 2 and shelf.tree.topLevelItem(0).childCount() == 2
    header = shelf.tree.itemWidget(shelf.tree.topLevelItem(0), 0)
    next(control for control in header.findChildren(QToolButton) if control.accessibleName() == "整组加入计划").click()
    assert len(window.profile.entries) == 3
    destination = view
    header = shelf.tree.itemWidget(shelf.tree.topLevelItem(0), 0)
    drag_group(header.findChild(GroupDragHandle))
    qtbot.waitUntil(lambda: len(window.profile.entries) == 1)
    assert window.resolved[0].mod == mods[2] and view.count() == 2
    assert window.save_profile()
    assert window.store.load(window.profile.id).mod_groups == [group, shared]


def test_right_click_preserves_multiselect_and_creates_visible_lock(qtbot, tmp_path, mods, monkeypatch):
    window = make_window(qtbot, tmp_path, mods)
    window.add_keys([mod.key for mod in mods])
    a, b, c = window.profile.entries
    view = window.profile_list
    view.item(0).setSelected(True)
    view.item(2).setSelected(True)
    choices = []

    def confirm(_parent, _title, changes, _action):
        choices.append(changes)
        return len(choices) == 2

    monkeypatch.setattr("darksort.ui.main_window.confirmation_dialog", confirm)
    menu = open_mod_context(qtbot, view, 2)
    assert window.selected_profile_indices() == [0, 2]
    assert menu_action(menu, "批量添加标签…").isEnabled()
    assert menu_action(menu, "标记颜色").menu() is not None
    action = menu_action(menu, "联锁选中项")
    assert action.isEnabled()
    action.trigger()
    assert window.profile.entries == [a, b, c] and not window.profile.locks
    assert "#03 → #02" in choices[0]
    action.trigger()
    menu.hide()
    assert window.profile.entries == [a, c, b]
    assert view.item(0).data(ROLE)["lock"] == view.item(1).data(ROLE)["lock"]
    assert view.pulsing_lock == a.id

    view.clearSelection()
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualItemRect(view.item(1)).center())
    assert window.profile_selection.text() == "2 已选"
    qtbot.mouseClick(
        view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier,
        pos=view.visualItemRect(view.item(0)).center(),
    )
    assert window.profile_selection.text() == "0 已选"
    qtbot.waitUntil(lambda: view.findChild(QMenu, "modContextMenu") is None)
    menu = open_mod_context(qtbot, view, 0)
    assert menu_action(menu, "解除联锁").isEnabled()
    menu_action(menu, "解除联锁").trigger()
    menu.hide()
    assert not window.profile.locks and window.profile.entries == [a, c, b]


@pytest.mark.parametrize("separated", [False, True], ids=["adjacent", "separated"])
def test_right_click_locks_selected_plan_entries_while_filtered(
    qtbot, tmp_path, mods, monkeypatch, separated,
):
    first = replace(mods[0], name="配套本体")
    patch = replace(mods[2], name="配套补丁")
    hidden = mods[1]
    ordered = [first, hidden, patch] if separated else [first, patch, hidden]
    window = make_window(qtbot, tmp_path, ordered)
    window.add_keys([mod.key for mod in ordered])
    before = window.profile.entries.copy()
    filter_list(window.profile_search, "配套")
    view = window.profile_list
    assert view.count() == 2 and view.reorder_blocked
    view.item(0).setSelected(True)
    view.item(1).setSelected(True)
    previews = []

    def confirm(_parent, _title, changes, _action):
        previews.append(changes)
        return len(previews) == 2

    monkeypatch.setattr("darksort.ui.main_window.confirmation_dialog", confirm)
    menu = open_mod_context(qtbot, view, 1)
    action = menu_action(menu, "联锁选中项")
    assert action.isEnabled()
    if separated:
        action.trigger()
        assert window.profile.entries == before and not window.profile.locks
        assert f"#02 → #03  {hidden.name}" in previews[0]
        assert f"#03 → #02  {patch.name}" in previews[0]
    action.trigger()
    menu.hide()
    assert len(previews) == (2 if separated else 0)
    assert [entry.name for entry in window.profile.entries] == [first.name, patch.name, hidden.name]
    assert window.profile.locks[0].members == tuple(entry.id for entry in window.profile.entries[:2])
    assert window.profile_search.filter_query == "配套" and view.count() == 2
    assert view.reorder_blocked
    window.undo_stack.undo()
    assert window.profile.entries == before and not window.profile.locks
    assert window.profile_search.filter_query == "配套"


def test_badges_explain_and_follow_current_dd1_priority(qtbot, tmp_path, mods, monkeypatch):
    relative = "heroes/crusader/crusader.info.darkest"
    for index, mod in enumerate(mods):
        root = Path(mod.path)
        root.mkdir(parents=True)
        if index < 2:
            resource = root / relative
            resource.parent.mkdir(parents=True)
            resource.write_bytes(bytes([index]))
            (root / "modfiles.txt").write_text(f"{relative} 1\n", encoding="utf-8")
    window = make_window(qtbot, tmp_path, mods)
    window.add_keys([mod.key for mod in mods])
    window.profile.rules = [OrderRule(mods[2].reference, "workshop:999", "requires")]
    window.render_lists()
    qtbot.waitUntil(lambda: window.file_report is not None, timeout=5000)
    report = window.file_report
    assert window.problem_buttons["warning"].text() == "警告 2"
    assert window.problem_buttons["error"].text() == "需处理 1"

    view = window.profile_list
    view.item(0).setSelected(True)
    view.item(1).setSelected(True)
    flag = column_rects(view.visualItemRect(view.item(1)), True)["status"].center()
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=flag)
    panel = window.utility.conflict_panel
    assert window.utility.tabs.currentWidget() is panel
    assert window.selected_profile_indices() == [0, 1]
    assert panel.title.text() == "英雄数值文件被覆盖"
    assert "甲 Mod" in panel.message.text()
    assert panel.targets.item(1).text().startswith("被覆盖")
    assert not panel.rule.isHidden()
    prefilled = []

    def read_rule(dialog):
        prefilled.append(tuple(selection[0] for selection in dialog.selected))
        dialog.swap.click()
        assert dialog.selected == [(mods[1].reference,), (mods[0].reference,)]
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr("darksort.ui.plan_dialogs.RuleDialog.exec", read_rule)
    panel.rule.click()
    assert prefilled == [(mods[0].reference, mods[1].reference)]
    assert len(window.profile.rules) == 1
    assert panel.technical.isHidden()
    panel.files.click()
    assert panel.technical.toPlainText() == relative
    panel.ask.click()
    assert window.assistant.attachment.kind == "problem"
    attached = window.assistant.prepare().messages[-1][1]
    assert "甲 Mod" in attached and relative not in attached
    window.assistant.hide()

    window.problem_buttons["warning"].click()
    assert [view.item(i).data(ROLE)["number"] for i in range(view.count())] == [1, 2]
    window.problem_buttons["error"].click()
    assert view.count() == 1 and view.item(0).data(ROLE)["number"] == 3
    assert panel.title.text() == "少了一个需要的 Mod"
    assert panel.rule.isHidden()
    window.clear_problem_button.click()
    assert view.count() == 3

    window.handle_drop("profile", {"side": "profile", "values": [1]}, 0)
    assert window.file_report is report
    window.inspect_problem(window.profile.entries[0].id)
    assert "乙 Mod" in panel.message.text()
    panel.targets.itemClicked.emit(panel.targets.item(1))
    assert window._detail_target == ModLocation("profile", window.profile.entries[1].id)


def test_missing_install_is_only_marked_after_scan(qtbot, tmp_path, mods):
    window = MainWindow(tmp_path, auto_scan=False)
    qtbot.addWidget(window, before_close_func=lambda w: setattr(w, "maybe_save", lambda: True))
    window.profile.entries = [ProfileEntry.from_mod(mods[0])]
    window.render_lists()
    assert window.problem_buttons["error"].text() == "需处理 0"
    assert "扫描本机" in window.utility.conflict_panel.status.text()
    window.on_scan_completed(Discovery(), ScanResult())
    assert window.problem_buttons["error"].text() == "需处理 1"
    assert window.plan_problems[0].title == "找不到这个 Mod"


def test_ignore_red_is_persisted_and_can_be_restored_without_changing_rules(qtbot, tmp_path, mods):
    for mod in mods[:2]:
        Path(mod.path).mkdir(parents=True)
    window = make_window(qtbot, tmp_path, mods[:2])
    window.add_keys([mod.key for mod in mods[:2]])
    first, second = window.profile.entries
    window.profile.rules = [
        OrderRule(first.reference, second.reference),
        OrderRule(second.reference, first.reference),
    ]
    window.render_lists()
    window.problem_buttons["error"].click()
    panel = window.utility.conflict_panel
    issue = panel.current_problem
    assert issue.level == "error" and window.problem_buttons["error"].text() == "需处理 2"
    panel.fill_ignore_menu()
    next(action for action in panel.ignore_menu.actions() if "所有红色提醒" in action.text()).trigger()
    assert window.problem_buttons["error"].text() == "需处理 0"
    assert window.plan_preview.blockers
    assert Settings.load(tmp_path).ignored_problems[window.profile.id] == [issue.key]
    panel.ignored_button.click()
    assert panel.current_problem == issue and "已忽略" in panel.badge.text()
    panel.fill_ignore_menu()
    menu_action(panel.ignore_menu, "恢复这条").trigger()
    assert window.problem_buttons["error"].text() == "需处理 2"
    assert not Settings.load(tmp_path).ignored_problems


def test_uncertain_path_stays_out_of_yellow_and_ignore_follows_change_in_winner(qtbot, tmp_path, mods):
    overlap = "heroes/crusader/crusader.art.darkest"
    unknown = "shared/extra.json"
    for index, mod in enumerate(mods):
        root = Path(mod.path)
        paths = (overlap, unknown) if index < 2 else (unknown,)
        for path in paths:
            file = root / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(bytes([index]))
        (root / "modfiles.txt").write_text("\n".join(f"{path} 1" for path in paths), encoding="utf-8")
    window = make_window(qtbot, tmp_path, mods)
    window.add_keys([mod.key for mod in mods])
    qtbot.waitUntil(lambda: window.file_report is not None, timeout=5000)
    assert window.problem_buttons["warning"].text() == "警告 2"
    assert window.profile.entries[2].id not in window.problem_levels
    window.show_conflicts()
    panel = window.utility.conflict_panel
    panel.review.click()
    assert panel.current_problem.level == "review"
    panel.overview.click()
    assert panel.current_problem.level == "warning"
    muted = panel.current_problem
    panel.fill_ignore_menu()
    menu_action(panel.ignore_menu, "忽略这条").trigger()
    assert window.problem_buttons["warning"].text() == "警告 0"
    window.handle_drop("profile", {"side": "profile", "values": [1]}, 0)
    assert window.problem_buttons["warning"].text() == "警告 2"
    assert panel.current_problem is not None and panel.current_problem.key != muted.key
    assert "乙 Mod" in panel.message.text()


def test_staging_candidates_is_per_profile_and_does_not_activate(qtbot, tmp_path, mods):
    window = make_window(qtbot, tmp_path, mods)
    window.library_list.item(0).setSelected(True)
    window.library_list.item(1).setSelected(True)
    window.stage_available()
    assert not window.profile.entries
    assert len(window.profile.staged) == 2
    assert window.library_list.count() == 1
    assert window.utility.staging.count() == 2
    window.add_staged()
    assert len(window.profile.entries) == 2 and not window.profile.staged
    window.profile_list.clearSelection()
    window.profile_list.item(0).setSelected(True)
    window.stage_plan()
    window.save_profile()
    previous = window.profile.id
    window.activate_profile(window.store.new("其它方案"))
    assert not window.profile.staged and window.library_list.count() == 3
    assert len(window.store.load(previous).staged) == 1


def test_main_window_reopens_the_same_slot_after_switching_to_bound_profile(
    qtbot, tmp_path, mods, monkeypatch
):
    window = make_window(qtbot, tmp_path / "data", mods)
    target = tmp_path / "Slot.json"
    target.write_text(json.dumps({"base_root": {"applied_ugcs_1_0": {}}}), encoding="utf-8")
    raw = target.read_bytes()
    bound = window.store.new("绑定测试")
    SaveBindings(window.data_dir).set(target, bound.id)
    visited = []

    def choose(dialog):
        visited.append(dialog.profile.id)
        if len(visited) == 1:
            dialog.select_path(target)
            dialog.open_bound_profile()
            return QDialog.DialogCode.Accepted
        assert dialog.state.path == target.resolve()
        assert dialog.bound_profile.id == bound.id
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr("darksort.ui.save_dialog.discover_slots", lambda _: [])
    monkeypatch.setattr("darksort.ui.main_window.SaveDialog.exec", choose)
    previous = window.profile.id
    window.manage_saves()
    assert visited == [previous, bound.id]
    assert window.profile.id == bound.id
    assert target.read_bytes() == raw
