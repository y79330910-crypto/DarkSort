from __future__ import annotations

import copy
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from pathlib import Path

from platformdirs import user_documents_path
from PySide6.QtCore import QItemSelectionModel, QSignalBlocker, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence, QUndoCommand, QUndoStack
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QInputDialog,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
)

from darksort import __version__
from darksort.adapters.campaigns import discover_launch_targets
from darksort.adapters.saves import entries_from_slot
from darksort.ai_client import AIError, AIProvider, AISettingsStore
from darksort.ai_tasks import preview_ai_order, sort_payload
from darksort.classification import Category, Classification, classify_mod
from darksort.conflicts import ConflictReport, reorder_conflicts
from darksort.diagnostics import file_problems, installation_problems
from darksort.history import ProfileHistory
from darksort.locks import entry_units, expand_selection, lock_starts, nudge_units, snap_boundary
from darksort.mod_groups import check_complete_locks, enable_group, members_in_plan
from darksort.mod_text_batch import MOD_TEXT_FIELDS
from darksort.models import (
    Mod,
    ModGroup,
    Profile,
    ProfileEntry,
    ProfileGroup,
    ProfileLock,
    ResolvedEntry,
    ScanResult,
)
from darksort.profiles import (
    MAX_ENTRIES,
    ProfileStore,
    parse_profile,
    profile_dict,
    resolve_entries,
)
from darksort.relations import RelatedMod, related_mods
from darksort.rule_targets import target_labels
from darksort.scanning.discovery import Discovery, steam_locations
from darksort.search import ModLocation, SearchRecord, search_text
from darksort.settings import DEFAULT_THEME, MARK_COLORS, Settings
from darksort.sharing import (
    export_filename,
    group_from_template,
    read_group_template,
    read_share_package,
    unique_title,
    write_group_template,
    write_share_package,
)
from darksort.sorting import browse_key, move_entries, preview_order
from darksort.tags import TagStore
from darksort.ui.ai_dialogs import AISettingsDialog, AssistantPanel, ModTextDialog
from darksort.ui.appearance import AppearanceDialog, show_artwork
from darksort.ui.campaign_dialog import CampaignDialog
from darksort.ui.conflict_dialog import ConflictWorker
from darksort.ui.group_panel import GroupMemberRow, ModGroupDialog
from darksort.ui.mod_list import ROLE
from darksort.ui.path_dialog import PathDialog
from darksort.ui.plan_dialogs import (
    HistoryDialog,
    RuleDialog,
    SortPreviewDialog,
    confirmation_dialog,
    transfer_matches,
)
from darksort.ui.save_dialog import SaveDialog
from darksort.ui.search import GlobalSearchDialog
from darksort.ui.theme import THEMES, apply_theme, icon, problem_color
from darksort.ui.workbench import build_workbench, button, label
from darksort.ui.worker import ScanWorker
from darksort.ui.workshop import open_workshop_page, workshop_id, workshop_page


@dataclass(frozen=True)
class RelationOrigin:
    target: ModLocation
    scroll_values: tuple[int, int, int]
    collapsed_groups: frozenset[str]
    temporary: ModLocation | None
    utility_tab: int


class ProfileEdit(QUndoCommand):
    def __init__(self, title, before: Profile, after: Profile, apply):
        super().__init__(title)
        self.before = before
        self.after = after
        self.apply = apply

    def undo(self):
        self.apply(self.before)

    def redo(self):
        self.apply(self.after)


class MainWindow(QMainWindow):
    def __init__(self, data_dir: Path, *, auto_scan: bool = True):
        super().__init__()
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.startup_errors = []
        try:
            self.settings = Settings.load(data_dir)
        except (OSError, ValueError, TypeError) as exc:
            self.settings = Settings()
            self.startup_errors.append(f"设置无法读取，当前使用默认设置：{exc}")
        self.store = ProfileStore(data_dir)
        self.history = ProfileHistory(data_dir)
        self.ai_store = AISettingsStore(data_dir)
        try:
            self.ai_provider, self.ai_warning = self.ai_store.load()
        except (OSError, ValueError):
            self.ai_provider = AIProvider()
            self.ai_warning = "AI 接入设置无法读取。可查看字段并重新保存设置。"
        self.assistant: AssistantPanel | None = None
        try:
            self.tags = TagStore(data_dir)
        except (OSError, ValueError) as exc:
            self.tags = None
            self.startup_errors.append(f"自定义标签无法读取 · {exc}")
        profiles, errors = self.store.list()
        self.startup_errors.extend(errors)
        if not profiles:
            profiles = [self.store.new("默认配置")]
        self.profile = next(
            (p for p in profiles if p.id == self.settings.last_profile), profiles[0]
        )
        self._baseline = copy.deepcopy(profile_dict(self.profile))
        self.undo_stack = QUndoStack(self)
        self.undo_stack.setUndoLimit(100)
        self.mods: list[Mod] = []
        self.classifications: dict[str, Classification] = {}
        self.resolved: list[ResolvedEntry] = []
        self.discovery: Discovery | None = None
        self.scan_result = ScanResult()
        self.worker: ScanWorker | None = None
        self._launched_game = None
        self._close_pending = False
        self._detail_mod: Mod | None = None
        self._detail_target: ModLocation | None = None
        self._relation_anchor: ModLocation | None = None
        self._relation_origin: RelationOrigin | None = None
        self._temporary_target: ModLocation | None = None
        self._scroll_target: ModLocation | None = None
        self._finish_jump = QTimer(self)
        self._finish_jump.setSingleShot(True)
        self._finish_jump.timeout.connect(self.settle_jump)
        self._detail_workshop_id: str | None = None
        self._search_hits = {"library": [], "profile": []}
        self._current_hits = {"library": None, "profile": None}
        self.collapsed_groups: set[str] = set()
        self.problem_filter = ""
        self.problem_levels = {}
        self.plan_problems = ()
        self.file_problems = ()
        self.file_warning_ids = set()
        self.file_report = None
        self.file_worker: ConflictWorker | None = None
        self._file_scope = None
        self._file_version = 0
        self._sort_basis = None
        self._problem_basis = None
        self._classification_revision = 0
        self._order_marks_revision = 0
        self._file_timer = QTimer(self, singleShot=True, interval=240)
        self._file_timer.timeout.connect(self.start_conflict_check)
        apply_theme(QApplication.instance(), self.settings.theme)
        self.setWindowTitle("DarkSort · 暗黑地牢 Mod 配置管理")
        self.resize(1510, 895)
        self.setMinimumSize(1030, 670)
        self.build_ui()
        self.populate_profiles()
        self.render_lists()
        self.refresh_history_panel()
        self.statusBar().showMessage(f"DarkSort {__version__} · 准备就绪")
        if self.startup_errors:
            QTimer.singleShot(0, self.show_startup_errors)
        if auto_scan:
            QTimer.singleShot(100, self.start_scan)

    @property
    def dirty(self) -> bool:
        return profile_dict(self.profile) != self._baseline

    def build_ui(self):
        build_workbench(self)
        self.add_shortcut("保存方案", QKeySequence.StandardKey.Save, self.save_profile)
        self.add_shortcut("扫描", "F5", self.start_scan)
        self.add_shortcut("搜全部", QKeySequence.StandardKey.Find, self.search_all)

    def add_shortcut(self, name, key, callback):
        action = QAction(name, self)
        action.setShortcut(key)
        action.triggered.connect(callback)
        self.addAction(action)

    def update_dirty(self):
        dirty = self.dirty
        self.dirty_label.setText("有未保存更改" if dirty else "方案已保存")
        self.save_button.setEnabled(dirty)
        self.setWindowTitle(f"{'* ' if dirty else ''}DarkSort · {self.profile.name}")

    def edit_profile(self, action: str, updated: Profile) -> bool:
        if profile_dict(updated) == profile_dict(self.profile):
            return False
        self.undo_stack.push(ProfileEdit(action, self.profile, updated, self.render_draft))
        return True

    def render_draft(self, profile: Profile):
        self.profile = profile
        self.render_lists()

    def populate_profiles(self):
        profiles, _ = self.store.list()
        with QSignalBlocker(self.profile_combo):
            self.profile_combo.clear()
            for profile in profiles:
                self.profile_combo.addItem(profile.name, profile.id)
            index = self.profile_combo.findData(self.profile.id)
            self.profile_combo.setCurrentIndex(index)

    def render_lists(self, *_):
        if not hasattr(self, "dock"):
            return
        lists = (self.library_list, self.profile_list)
        scroll_values = [widget.verticalScrollBar().value() for widget in lists]
        library_selected = {item.data(ROLE)["value"] for item in self.library_list.selectedItems()}
        profile_selected = {
            item.data(ROLE)["entry_id"] for item in self.profile_list.selectedItems()
            if "entry_id" in item.data(ROLE)
        }
        self.resolved = resolve_entries(self.profile.entries, self.mods)
        self.staged_resolved = resolve_entries(self.profile.staged, self.mods) if self.profile.staged else []
        self.mods_by_key = {mod.key: mod for mod in self.mods}
        self.staged_keys = {row.mod.key: row.entry.id for row in self.staged_resolved if row.mod}
        self.plan_positions = {row.entry.id: i for i, row in enumerate(self.resolved)}
        self.stage_positions = {row.entry.id: i for i, row in enumerate(self.staged_resolved)}
        self.group_marks = defaultdict(list)
        for group in self.profile.mod_groups:
            for member in group.members:
                self.group_marks[member.reference].append((group.title, group.color))
        self.plan_units = entry_units(self.profile.entries, self.profile.locks)
        self.active_locks = lock_starts(self.profile.entries, self.profile.locks)
        self.lock_by_entry = {member: lock for lock in self.profile.locks for member in lock.members}
        self.profile_list.units = self.plan_units if self.active_locks else []
        self.refresh_plan_problems()
        used = {entry.mod.key for entry in self.resolved if entry.mod} | self.staged_keys.keys()
        order = self.library_search.order
        if order == "category":
            mods = sorted(self.mods, key=lambda mod: (
                self.classifications[mod.key].label, self.display_name(mod.reference, mod.name).casefold(),
            ))
        else:
            mods = sorted(self.mods, key=lambda m: browse_key(m, order, self.display_name(m.reference, m.name)),
                          reverse=order == "name_desc")
        available = [mod for mod in mods if mod.key not in used]
        self.available_keys = {mod.key for mod in available}
        self.search_records = {
            "library": [self.search_record(ModLocation("library", mod.key), mod, None, "可用") for mod in available],
            "profile": [
                self.search_record(ModLocation("profile", row.entry.id), row.mod, row, f"计划 #{i + 1}")
                for i, row in enumerate(self.resolved)
            ],
            "staging": [
                self.search_record(ModLocation("staging", row.entry.id), row.mod, row, "暂存")
                for row in self.staged_resolved
            ],
        }
        if self._temporary_target and not self.location_exists(self._temporary_target):
            self._temporary_target = None
        self.matching_targets = {}
        self.matching_targets["library"] = {
            record.target for mod, record in zip(available, self.search_records["library"], strict=True)
            if self.passes_filters(self.library_search, record, mod)
        }
        self.matching_targets["profile"] = {
            record.target for row, record in zip(self.resolved, self.search_records["profile"], strict=True)
            if (
                self.problem_levels.get(row.entry.id) == self.problem_filter
                if self.problem_filter else self.passes_filters(self.profile_search, record, row.mod, row.entry)
            )
        }
        if self._temporary_target and self._temporary_target in self.matching_targets[self._temporary_target.side]:
            self._temporary_target = None
        shown = [
            mod for mod, record in zip(available, self.search_records["library"], strict=True)
            if record.target in self.matching_targets["library"] or record.target == self._temporary_target
        ]
        with QSignalBlocker(self.library_list), QSignalBlocker(self.profile_list):
            self.library_list.clear()
            for mod in shown:
                self.add_mod_item(
                    self.library_list, mod, value=mod.key, selected=mod.key in library_selected,
                    temporary=self._temporary_target == ModLocation("library", mod.key),
                )
            self.profile_list.clear()
            visible = self.render_profile_rows(profile_selected)
            for widget, value in zip(lists, scroll_values, strict=True):
                widget.verticalScrollBar().setValue(value)
        missing = sum(entry.mod is None for entry in self.resolved)
        for side, badge, total in (
            ("library", self.library_count, len(available)), ("profile", self.plan_count, len(self.resolved)),
        ):
            normal = len(self.matching_targets[side])
            badge.setText(f"{normal}/{total}" if normal != total else str(total))
            bar = self.search_bar(side)
            if self._temporary_target and self._temporary_target.side == side:
                label_text = "临时显示"
            elif side == "profile" and self.problem_filter:
                label_text = "正在看红色问题" if self.problem_filter == "error" else "正在看黄色警告"
            elif side == "profile" and bar.filtering:
                label_text = "暂停调序"
            elif side == "profile" and visible != total:
                label_text = "有折叠"
            elif side == "profile" and missing:
                label_text = f"{missing} 待匹配"
            else:
                label_text = ""
            bar.summary.setText(label_text)
        self.profile_list.reorder_blocked = self.plan_is_filtered() or visible != len(self.resolved)
        self.utility.set_groups(self.profile.id, self.mod_group_rows(), self.groups_by_index(), self.active_locks)
        self.utility.set_staged(self.staged_resolved, lock_starts(self.profile.staged, self.profile.locks))
        self.refresh_search_hits("library")
        self.refresh_search_hits("profile")
        self.refresh_detail()
        self.update_selection_totals()
        if self.discovery:
            self.path_summary.setText(
                f"DD1   ·   {len(self.mods)} 已安装   /   {len(self.profile.entries)} 计划"
            )
        self.update_dirty()
        if self.assistant:
            self.assistant.update_controls()

    def search_bar(self, side):
        return self.library_search if side == "library" else self.profile_search

    def search_record(self, target, mod, row, location):
        entry = row.entry if row else None
        original = mod.name if mod else entry.name
        reference = mod.reference if mod else entry.reference
        name = self.display_name(reference, original)
        workshop_id = (mod.workshop_id if mod else None) or (entry.workshop_id if entry else None)
        source = mod.source if mod else entry.source
        if mod:
            metadata = f"{source.label} {self.classifications[mod.key].label} {' '.join(self.mod_tags(mod))}"
            details = f"{mod.path} {mod.description[:8192]}"
        else:
            metadata = f"{source.label} 待匹配"
            details = entry.path
        group_names = " ".join(title for title, _ in self.group_marks[reference])
        metadata += f" {group_names}"
        if translated := self.settings.mod_translations.get(reference):
            details += f" {translated[:8192]}"
        if entry:
            details += f" {entry.path}"
        caption = f"{location} · {source.label}"
        if workshop_id:
            caption += f" · {workshop_id}"
        return SearchRecord.create(target, name, caption, (name, original, entry.name if entry else original),
                                   workshop_id, metadata, details)

    def passes_filters(self, bar, record, mod, entry=None):
        conditions = bar.conditions
        if bar.filter_query and record.rank(bar.filter_query) is None:
            return False
        source = conditions.get("source")
        if source and (mod.source if mod else entry.source) != source:
            return False
        category = conditions.get("category")
        if category:
            result = self.classifications[mod.key].category if mod else None
            actual = result.value if result else "other" if mod else "unresolved"
            if actual != category:
                return False
        tag = conditions.get("tag")
        if tag and (not mod or all(search_text(item) != tag for item in self.mod_tags(mod))):
            return False
        status = conditions.get("status")
        return (
            not status or (status == "missing" and mod is None)
            or (status == "warning" and mod is not None and bool(mod.warnings))
            or (status == "conflict" and entry is not None and entry.id in self.file_warning_ids)
            or (status == "lock" and entry is not None and entry.id in self.lock_by_entry)
        )

    def search_filters_changed(self, side):
        if self._temporary_target and self._temporary_target.side == side:
            self._temporary_target = None
        if side == "profile" and self.problem_filter:
            self.problem_filter = ""
            self.utility.conflict_panel.inspect()
        self.render_lists()

    def refresh_search_hits(self, side):
        bar = self.search_bar(side)
        query = search_text(bar.texts["locate"])
        hits = [record.target for record in self.search_records[side] if query and record.rank(query) is not None]
        self._search_hits[side] = hits
        if self._current_hits[side] not in hits:
            self._current_hits[side] = None
        view = self.library_list if side == "library" else self.profile_list
        view.search_marks = set(self.row_value(target) for target in hits) if bar.mode == "locate" else set()
        current = self._current_hits[side]
        view.current_match = self.row_value(current) if current and bar.mode == "locate" else None
        view.viewport().update()
        position = hits.index(current) + 1 if current is not None else 0
        hidden = sum(hit not in self.matching_targets[side] for hit in hits)
        bar.update_count(len(hits), position, hidden)

    def locate_next(self, side, direction=1):
        hits = self._search_hits[side]
        if self.search_bar(side).mode != "locate" or not hits:
            return
        current = self._current_hits[side]
        anchor = hits.index(current) if current in hits else (-1 if direction > 0 else 0)
        target = hits[(anchor + direction) % len(hits)]
        self._current_hits[side] = target
        self.navigate(target, focus=False)
        self.refresh_search_hits(side)

    def search_all(self):
        records = self.search_records
        dialog = GlobalSearchDialog(records["profile"] + records["library"] + records["staging"], self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.target:
            self.navigate(dialog.target)

    def update_selection_totals(self):
        self.library_selection.setText(f"{len(self.library_list.selectedItems())} 已选")
        indices = self.selected_profile_indices()
        expanded = expand_selection(self.plan_units, indices) if self.active_locks else indices
        self.profile_selection.setText(f"{len(expanded)} 已选")
        self.profile_list.lock_selection = set(expanded).difference(indices)
        self.profile_list.viewport().update()

    def mod_tags(self, mod: Mod) -> tuple[str, ...]:
        tags = self.tags.tags.get(mod.reference, ()) if self.tags else ()
        return tuple(dict.fromkeys((*mod.tags, *tags)))

    def display_name(self, reference, original):
        return self.settings.mod_aliases.get(reference) or original

    def refresh_tag_options(self):
        tags = {}
        for mod in self.mods:
            for tag in self.mod_tags(mod):
                tags.setdefault(search_text(tag), tag)
        for bar in (self.library_search, self.profile_search):
            bar.update_options(sorted(tags.items()))

    def groups_by_index(self):
        groups = {group.anchor: group for group in self.profile.groups}
        starts = [index for index, entry in enumerate(self.profile.entries) if entry.id in groups]
        return {
            index: (groups[self.profile.entries[index].id], end)
            for index, end in zip(starts, starts[1:] + [len(self.profile.entries)])
        }

    def render_profile_rows(self, selected):
        filtering = self.plan_is_filtered()
        matches = [
            record.target in self.matching_targets["profile"] or record.target == self._temporary_target
            for record in self.search_records["profile"]
        ]
        groups = self.groups_by_index()
        self.profile_list.total_entries = len(self.resolved)
        collapsed = False
        visible = 0
        group_color = -1
        for index, row in enumerate(self.resolved):
            if index in groups:
                group_color = (group_color + 1) % 4
                group, end = groups[index]
                collapsed = group.anchor in self.collapsed_groups and not filtering
                count = sum(matches[index:end])
                if count or not filtering:
                    item = QListWidgetItem(f"{group.title}   ·   {count}")
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                    item.setData(ROLE, {
                        "group": group.anchor, "name": item.text(),
                        "value": index, "collapsed": collapsed,
                        "group_color": group_color,
                    })
                    item.setToolTip("点击展开或折叠分组")
                    self.profile_list.addItem(item)
            if collapsed or not matches[index]:
                continue
            visible += 1
            entry, mod = row.entry, row.mod
            temporary = ModLocation("profile", entry.id) == self._temporary_target
            if mod:
                self.add_mod_item(
                    self.profile_list, mod, value=index, entry_id=entry.id,
                    number=index + 1, selected=entry.id in selected, temporary=temporary,
                )
            else:
                name = self.display_name(entry.reference, entry.name)
                data = {
                    "name": name, "classification": "待定位", "source": entry.source.value,
                    "value": index, "entry_id": entry.id, "number": index + 1, "missing": True,
                    "lock": self.lock_by_entry.get(entry.id), "temporary": temporary,
                    "problem": self.problem_levels.get(entry.id),
                    "mark_color": self.settings.mod_colors.get(entry.reference),
                    "group_colors": tuple(color for _, color in self.group_marks[entry.reference]),
                }
                item = QListWidgetItem(f"{index + 1}. {name} · 待定位")
                item.setData(ROLE, data)
                item.setToolTip(f"{row.problem}\n{entry.path}")
                self.profile_list.addItem(item)
                item.setSelected(entry.id in selected)
        return visible

    def add_mod_item(
        self, widget, mod, *, value, entry_id=None, number=None, selected=False, temporary=False,
    ):
        classification = self.classifications[mod.key]
        name = self.display_name(mod.reference, mod.name)
        groups = self.group_marks[mod.reference]
        item = QListWidgetItem(name)
        item.setData(
            ROLE,
            {
                "name": name,
                "version": mod.version,
                "category": classification.category.value if classification.category else "",
                "classification": classification.label,
                "value": value,
                "entry_id": entry_id,
                "number": number,
                "source": mod.source.value,
                "preview": mod.preview_path,
                "warning": bool(mod.warnings) and widget.side == "library",
                "problem": self.problem_levels.get(entry_id),
                "mark_color": self.settings.mod_colors.get(mod.reference),
                "group_colors": tuple(color for _, color in groups),
                "lock": self.lock_by_entry.get(entry_id),
                "temporary": temporary,
            },
        )
        tooltip = f"{mod.name}\n{classification.label} · {classification.reason}\n{mod.path}"
        if name != mod.name:
            tooltip = f"{name} · 别名\n{tooltip}"
        if groups:
            tooltip += "\n分组 · " + "、".join(title for title, _ in groups)
        if lock := self.lock_by_entry.get(entry_id):
            tooltip += f"\n联锁 · {lock.title} · {len(lock.members)} 项"
        item.setToolTip(tooltip)
        widget.addItem(item)
        item.setSelected(selected)

    def clear_detail(self):
        self._detail_mod = None
        self._detail_target = None
        self._relation_anchor = None
        self._relation_origin = None
        self._detail_workshop_id = None
        self.detail_title.setText("选择一个 Mod")
        self.detail_original.hide()
        self.detail_art.set_art(None)
        for value in self.detail_facts.values():
            value.setText("—")
            value.setToolTip("")
        self.detail_tags.set_tags(())
        self.update_detail_marks()
        self.detail_description.clear()
        self.detail_warning.hide()
        self.open_folder_button.setEnabled(False)
        self.open_folder_button.setToolTip("")
        self.tags_button.setEnabled(False)
        self.classification_button.setEnabled(False)
        self.shop_links.hide()
        self.library_list.detail_mark = self.profile_list.detail_mark = None
        self.update_relations()

    def refresh_detail(self):
        target = self._detail_target
        if target and self.location_exists(target):
            if not self._relation_anchor or not self.location_exists(self._relation_anchor):
                self._relation_anchor = target
                self._relation_origin = None
            self.show_detail(target)
            return
        for widget in (self.profile_list, self.library_list):
            if selected := widget.selectedItems():
                target = self.item_location(widget.side, selected[0])
                self._relation_anchor = target
                self._relation_origin = None
                self.show_detail(target)
                return
        self.clear_detail()

    @staticmethod
    def item_location(side, item):
        data = item.data(ROLE)
        return ModLocation(side, data["value"] if side == "library" else data["entry_id"])

    def show_selected_detail(self, side, item=None):
        widget = self.library_list if side == "library" else self.profile_list
        selected = widget.selectedItems()
        self.update_selection_totals()
        if not selected and item is None:
            if self._detail_target and self._detail_target.side == side and not self._relation_origin:
                self._detail_target = None
                self.refresh_detail()
            return
        current = widget.currentItem()
        target = self.item_location(side, item or (current if current in selected else selected[0]))
        if target == self._detail_target == self._relation_anchor and not self._relation_origin:
            return
        self.inspect_selection(target)

    def inspect_selection(self, target):
        self._detail_target = self._relation_anchor = target
        self._relation_origin = None
        if self._temporary_target and self._temporary_target != target:
            self._temporary_target = None
            self.render_lists()
        else:
            self.show_detail(target)

    def location_exists(self, target):
        if target.side == "library":
            return target.identity in self.available_keys
        positions = self.plan_positions if target.side == "profile" else self.stage_positions
        return target.identity in positions

    def row_value(self, target):
        if target.side == "library":
            return target.identity
        positions = self.plan_positions if target.side == "profile" else self.stage_positions
        return positions[target.identity]

    def location_details(self, target):
        value = self.row_value(target)
        if target.side == "library":
            return self.mods_by_key[value], None
        row = (self.resolved if target.side == "profile" else self.staged_resolved)[value]
        return row.mod, row

    def show_detail(self, target):
        mod, resolved = self.location_details(target)
        self._detail_mod = mod
        self._detail_target = target
        self.library_list.detail_mark = target.identity if target.side == "library" else None
        self.profile_list.detail_mark = self.row_value(target) if target.side == "profile" else None
        identifier = workshop_id(mod.workshop_id if mod else None) or workshop_id(
            resolved.entry.workshop_id if resolved else None
        )
        self._detail_workshop_id = identifier
        self.shop_links.setVisible(bool(identifier))
        self.open_folder_button.setEnabled(mod is not None)
        self.open_folder_button.setToolTip(mod.path if mod else "")
        self.tags_button.setEnabled(mod is not None and self.tags is not None)
        self.classification_button.setEnabled(mod is not None)
        original = mod.name if mod else resolved.entry.name
        reference = mod.reference if mod else resolved.entry.reference
        name = self.display_name(reference, original)
        self.detail_title.setText(name)
        self.detail_original.setText(f"原名 · {original}")
        self.detail_original.setVisible(name != original)
        self.update_detail_marks(reference)
        self.detail_art.set_art(mod.preview_path if mod else None)
        if mod:
            classification = self.classifications[mod.key]
            facts = (mod.source.label, classification.label, mod.version, identifier or "—")
            self.detail_facts["分类"].setToolTip(classification.reason)
            self.detail_facts["版本"].setToolTip(f"目标构建 {mod.target_build}" if mod.target_build else "")
            tags = self.mod_tags(mod)
            self.detail_tags.set_tags(tags)
            self.detail_description.setPlainText(
                self.settings.mod_translations.get(reference) or mod.description or "此 Mod 未提供简介。"
            )
            warning = "\n".join(mod.warnings)
        else:
            facts = (resolved.entry.source.label, "待定位", "—", identifier or "—")
            self.detail_facts["分类"].setToolTip("")
            self.detail_facts["版本"].setToolTip("")
            self.detail_tags.set_tags(())
            hint = (
                "加入计划后可用「匹配」选择对应安装。"
                if target.side == "staging" else "选中计划条目，再用「匹配」选择对应安装。"
            )
            self.detail_description.setPlainText(hint)
            warning = resolved.problem
        for widget, value in zip(self.detail_facts.values(), facts, strict=True):
            widget.setText(value)
        self.detail_warning.setText(warning)
        self.detail_warning.setVisible(bool(warning))
        self.update_relations()

    def update_relations(self):
        anchor = self._relation_anchor
        title = ""
        relations = []
        if anchor and self.location_exists(anchor):
            mod, row = self.location_details(anchor)
            title = mod.name if mod else row.entry.name
            relations = related_mods(
                mod, row.entry if row else None, self.profile, self.mods,
                self.resolved, self.staged_resolved, self.classifications,
            )
        self.library_list.relation_marks = {
            relation.target.identity for relation in relations
            if relation.target and relation.target.side == "library"
        }
        plan_relations = {
            (relation.target.identity, self.row_value(relation.target), relation.edge_kind)
            for relation in relations if relation.target and relation.target.side == "profile"
        }
        self.profile_list.relation_marks = {index for _, index, _ in plan_relations}
        anchor_id = anchor.identity if title and anchor.side == "profile" else None
        self.profile_list.set_relationships(anchor_id, plan_relations)
        self.utility.set_relations(
            title, relations, self.detail_title.text(), bool(self._relation_origin),
            show_legend=bool(anchor_id and plan_relations),
        )
        self.library_list.viewport().update()

    def reveal_relation(self, relation: RelatedMod):
        if relation.target:
            self.navigate(relation.target, keep_anchor=True)
        elif relation.choices:
            self.choose_relation(relation)
        else:
            self.statusBar().showMessage(f"{relation.location} · {relation.evidence}", 5000)

    def choose_relation(self, relation):
        dialog = QDialog(self)
        dialog.setWindowTitle("选择关联目标")
        dialog.resize(535, 315)
        layout = QVBoxLayout(dialog)
        text = label(f"{relation.name} · 找到 {len(relation.choices)} 个可能目标", "section")
        text.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(text)
        choices = QListWidget(objectName="globalResults")
        choices.setAccessibleName("关联候选")
        for candidate in relation.choices:
            item = QListWidgetItem(f"{candidate.name}\n{candidate.caption} · {candidate.path}")
            item.setToolTip(candidate.path)
            item.setData(ROLE, candidate.target)
            choices.addItem(item)
        choices.itemActivated.connect(dialog.accept)
        layout.addWidget(choices)
        buttons = QDialogButtonBox()
        jump = buttons.addButton("跳转", QDialogButtonBox.ButtonRole.AcceptRole)
        jump.setEnabled(False)
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        choices.currentItemChanged.connect(lambda item, _: jump.setEnabled(item is not None))
        layout.addWidget(buttons)
        jump.setAutoDefault(False)
        if dialog.exec() == QDialog.DialogCode.Accepted and choices.currentItem():
            self.navigate(choices.currentItem().data(ROLE), keep_anchor=True)

    def reveal_plan_relation(self, entry_id):
        self.navigate(ModLocation("profile", entry_id), keep_anchor=True)

    def reveal_entry(self, index):
        if 0 <= index < len(self.resolved):
            self.navigate(ModLocation("profile", self.resolved[index].entry.id), select=True)

    def navigate(self, target, *, keep_anchor=False, focus=True, select=False):
        if not self.location_exists(target):
            self.statusBar().showMessage("目标已移出当前列表。", 4000)
            return
        if keep_anchor and target == self._relation_anchor and self._relation_origin:
            self.return_to_relation_origin()
            return
        if keep_anchor and self._relation_anchor:
            if self._relation_origin is None and target != self._relation_anchor:
                self._relation_origin = RelationOrigin(
                    self._relation_anchor, tuple(view.verticalScrollBar().value() for view in self.navigation_lists()),
                    frozenset(self.collapsed_groups), self._temporary_target, self.utility.tabs.currentIndex(),
                )
        else:
            self._relation_anchor = target
            self._relation_origin = None
        previous = self._temporary_target
        self._temporary_target = (
            target if target.side in self.matching_targets and target not in self.matching_targets[target.side]
            else None
        )
        expanded = False
        if target.side == "profile":
            index = self.row_value(target)
            for start, (group, end) in self.groups_by_index().items():
                if start <= index < end and group.anchor in self.collapsed_groups:
                    self.collapsed_groups.discard(group.anchor)
                    expanded = True
                    break
        self._detail_target = target
        if previous != self._temporary_target or expanded:
            self.render_lists()
        if target.side == "staging":
            self.show_utility_page(self.utility.stage_page)
        elif keep_anchor and self._relation_origin:
            self.show_utility_page(self.utility.relation_page)
        self.show_detail(target)
        self.point_to_location(target, focus=focus, select=select)
        # Qt may resize the viewport after switching tabs or revealing a group.
        self._scroll_target = target
        self._finish_jump.start(0)

    def settle_jump(self):
        target = self._scroll_target
        self._scroll_target = None
        if target and target == self._detail_target and self.location_exists(target):
            self.point_to_location(target, focus=False)

    def navigation_lists(self):
        return self.library_list, self.profile_list, self.utility.staging

    def point_to_location(self, target, *, focus=True, select=False):
        widget = {
            "library": self.library_list, "profile": self.profile_list, "staging": self.utility.staging,
        }[target.side]
        for index in range(widget.count()):
            item = widget.item(index)
            data = item.data(ROLE)
            if target.side == "staging":
                matches = target.identity in data
            else:
                matches = "group" not in data and self.item_location(target.side, item) == target
            if matches:
                flags = QItemSelectionModel.SelectionFlag
                with QSignalBlocker(widget):
                    widget.setCurrentItem(item, flags.ClearAndSelect if select else flags.NoUpdate)
                widget.scrollToItem(item, QAbstractItemView.ScrollHint.EnsureVisible)
                if focus:
                    widget.setFocus()
                if select:
                    self.update_selection_totals()
                return

    def return_to_relation_origin(self):
        origin = self._relation_origin
        if not origin:
            return
        self._finish_jump.stop()
        self._scroll_target = None
        self._relation_origin = None
        self._detail_target = self._relation_anchor = origin.target
        self._temporary_target = origin.temporary
        self.collapsed_groups = set(origin.collapsed_groups)
        self.utility.tabs.setCurrentIndex(origin.utility_tab)
        self.render_lists()
        if self.location_exists(origin.target):
            self.point_to_location(origin.target)
        for widget, value in zip(self.navigation_lists(), origin.scroll_values, strict=True):
            widget.verticalScrollBar().setValue(value)

    def follow_current_detail(self):
        self._relation_origin = None
        self._relation_anchor = self._detail_target
        self.show_utility_page(self.utility.relation_page)
        self.update_relations()

    def click_staged_item(self, item):
        members = item.data(ROLE)
        if len(members) == 1:
            target = ModLocation("staging", members[0])
            if target != self._detail_target:
                self.inspect_selection(target)

    def preview_cover(self):
        if self._detail_mod and self._detail_mod.preview_path:
            show_artwork(self, self._detail_mod.preview_path, self._detail_mod.name)

    def open_mod_folder(self, *, mod=None):
        if (target := mod or self._detail_mod) and not QDesktopServices.openUrl(QUrl.fromLocalFile(target.path)):
            self.statusBar().showMessage(f"无法打开目录 · {target.path}", 5000)

    def open_workshop(self, *, identifier=None, browser=False):
        identifier = identifier or self._detail_workshop_id
        if not workshop_page(identifier):
            return
        destination = open_workshop_page(identifier, browser=browser)
        if destination:
            self.statusBar().showMessage(f"已请求在 {destination} 打开工坊页面。", 5000)
            return
        box = QMessageBox(QMessageBox.Icon.Warning, "无法打开工坊", "可以复制链接，再粘贴到浏览器打开。", parent=self)
        copy_link = box.addButton("复制工坊链接", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() == copy_link:
            self.copy_workshop_link(identifier=identifier)

    def add_workshop_menu_actions(self, menu, *, identifier=None):
        menu.addAction("在浏览器打开工坊", lambda: self.open_workshop(identifier=identifier, browser=True))
        menu.addAction("复制工坊链接", lambda: self.copy_workshop_link(identifier=identifier))

    def copy_workshop_link(self, *, identifier=None):
        if page := workshop_page(identifier or self._detail_workshop_id):
            QApplication.clipboard().setText(page)
            self.statusBar().showMessage("已复制工坊链接。", 3500)

    def copy_workshop_id(self):
        if self._detail_workshop_id:
            QApplication.clipboard().setText(self._detail_workshop_id)
            self.statusBar().showMessage("已复制 Workshop ID。", 3500)

    def open_data_folder(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.data_dir)))

    def toggle_details(self):
        self.toggle_pane(self.detail_panel)

    def toggle_utility(self):
        self.toggle_pane(self.utility)

    def toggle_pane(self, pane):
        opening = pane.isHidden()
        pane.setVisible(opening)
        if opening:
            sizes = self.panes.sizes()
            sizes[self.panes.indexOf(pane)] = 247
            sizes[2] = max(324, sizes[2] - 247)
            self.panes.setSizes(sizes)

    def toggle_dock(self):
        if self.dock.isVisible() and self.workspace_split.sizes()[1] > 0:
            self.dock.hide()
        else:
            self.dock.show()
            self.workspace_split.setSizes([max(400, self.workspace_split.height() - 190), 190])

    def show_dock(self, index):
        if not self.dock.isVisible() or not self.workspace_split.sizes()[1]:
            self.dock.show()
            self.workspace_split.setSizes([max(400, self.workspace_split.height() - 190), 190])
        self.dock.setCurrentIndex(index)

    def set_theme(self, key: str):
        app = QApplication.instance()
        apply_theme(app, key)
        self.setWindowIcon(app.windowIcon())
        self.brand_icon.setPixmap(app.windowIcon().pixmap(22, 22))
        for widget in self.findChildren(QAbstractButton):
            if glyph := widget.property("glyph"):
                level = widget.property("problemLevel")
                widget.setIcon(icon(glyph, problem_color(level) if level else None))
        self.dock.hide_button.setIcon(icon("close"))
        self.library_list.viewport().update()
        self.profile_list.viewport().update()
        self.detail_art.update()
        self.detail_tags.update()
        self.detail_groups.update()
        self.utility.conflict_panel.render()
        self.utility.group_shelf.set_groups(self.profile.id, self.mod_group_rows())
        if self.assistant:
            self.assistant.render_chat()
        self.refresh_detail()

    def edit_appearance(self):
        original = self.settings.theme if self.settings.theme in THEMES else DEFAULT_THEME
        dialog = AppearanceDialog(original, self)
        dialog.theme_changed.connect(self.set_theme)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self.set_theme(original)
            return
        try:
            updated = replace(self.settings, theme=dialog.key)
            updated.save(self.data_dir)
            self.settings = updated
        except OSError as exc:
            self.set_theme(original)
            self.error("主题保存失败", str(exc))

    def refresh_classifications(self):
        overrides = self.settings.mod_categories
        self.classifications = {
            mod.key: classify_mod(mod, overrides.get(mod.reference, "")) for mod in self.mods
        }
        self._classification_revision += 1

    def edit_classification(self):
        if self._detail_mod:
            self.change_classification((self._detail_mod,))

    def change_classification(self, mods):
        categories = list(Category)
        choices = ["自动识别", *(category.label for category in categories)]
        selected = self.settings.mod_categories.get(mods[0].reference) if len(mods) == 1 else None
        caption = mods[0].name if len(mods) == 1 else f"为所选 {len(mods)} 个 Mod 设置分类"
        initial = next((i for i, category in enumerate(categories, 1) if category.value == selected), 0)
        name, accepted = QInputDialog.getItem(
            self, "设置 Mod 分类", caption, choices, initial, False
        )
        if not accepted:
            return
        overrides = self.settings.mod_categories.copy()
        index = choices.index(name)
        for mod in mods:
            if index:
                overrides[mod.reference] = categories[index - 1].value
            else:
                overrides.pop(mod.reference, None)
        try:
            updated = replace(self.settings, mod_categories=overrides)
            updated.save(self.data_dir)
            self.settings = updated
            self.refresh_classifications()
            self.render_lists()
        except OSError as exc:
            self.error("分类保存失败", str(exc))

    def edit_tags(self):
        if self._detail_mod and self.tags:
            self.change_tags((self._detail_mod,))

    def change_tags(self, mods, *, append=False):
        references = tuple(dict.fromkeys(mod.reference for mod in mods))
        current = "" if append else ", ".join(self.tags.tags.get(references[0], ()))
        title = f"给所选 {len(mods)} 个 Mod 添加标签" if append else "自定义标签"
        text, ok = QInputDialog.getText(self, title, "多个标签用逗号分隔", text=current)
        if ok:
            try:
                self.tags.set_many(references, text, append=append)
                self._order_marks_revision += 1
                self.refresh_tag_options()
                self.render_lists()
            except (OSError, ValueError) as exc:
                self.error("标签保存失败", str(exc))

    def mark_mods(self, mods, color):
        colors = self.settings.mod_colors.copy()
        for mod in mods:
            if color:
                colors[mod.reference] = color
            else:
                colors.pop(mod.reference, None)
        try:
            updated = replace(self.settings, mod_colors=colors)
            updated.save(self.data_dir)
            self.settings = updated
            self._order_marks_revision += 1
            self.render_lists()
        except OSError as exc:
            self.error("颜色保存失败", str(exc))

    def update_detail_marks(self, reference=""):
        groups = self.group_marks.get(reference, ())
        self.detail_groups.set_tags([title for title, _ in groups], [color for _, color in groups])
        self.detail_group_button.setEnabled(bool(reference))
        self.detail_alias_button.setEnabled(self._detail_mod is not None)
        self.detail_generation_button.setEnabled(self._detail_mod is not None)
        current = self.settings.mod_colors.get(reference, "")
        for color, control in self.detail_colors.items():
            control.setEnabled(bool(reference and (color or current)))
            control.setChecked(bool(color and current == color))

    def mark_detail(self, color):
        if self._detail_target:
            mod, row = self.location_details(self._detail_target)
            self.mark_mods((mod or row.entry,), color)

    def edit_mod_text(self, mod=None, *, generate=False):
        mod = mod or self._detail_mod
        if not mod:
            return
        reference = mod.reference
        original = (self.settings.mod_aliases.get(reference, ""), self.settings.mod_translations.get(reference, ""))
        dialog = ModTextDialog(self, mod, *original)
        if generate:
            QTimer.singleShot(0, dialog, dialog.generate)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            dialog.deleteLater()
            return
        changes = {
            field: value for field, value, before in zip(MOD_TEXT_FIELDS, dialog.text_values(), original, strict=True)
            if value != before or dialog.clear_requested
        }
        dialog.deleteLater()
        if not changes:
            return
        aliases = self.settings.mod_aliases.copy()
        translations = self.settings.mod_translations.copy()
        maps = {"alias": aliases, "translation": translations}
        for field, text in changes.items():
            if text:
                maps[field][reference] = text
            else:
                maps[field].pop(reference, None)
        try:
            self.save_mod_display(aliases, translations)
        except (OSError, ValueError) as exc:
            self.error("无法保存别名和简介", str(exc))
            return
        self.mod_text_batch.keep_manual_fields(reference, changes)
        self.render_lists()

    def save_mod_display(self, aliases, translations):
        updated = replace(self.settings, mod_aliases=aliases, mod_translations=translations)
        updated.save(self.data_dir)
        self.settings = updated

    def mod_group_rows(self):
        groups = self.profile.mod_groups
        if not groups:
            return []
        occupied = {}
        for side, rows in (("profile", self.resolved), ("staging", self.staged_resolved)):
            for number, row in enumerate(rows, 1):
                status = f"已加入计划 #{number}" if side == "profile" else "暂存中"
                if not row.mod:
                    status += " · 待匹配"
                occupied.setdefault(row.entry.reference, (ModLocation(side, row.entry.id), status, row.mod))
        entries = list(dict.fromkeys(member for group in groups for member in group.members))
        installed = {row.entry: row for row in resolve_entries(entries, self.mods)}
        output = []
        for group in groups:
            members = []
            for member in group.members:
                reference = member.reference
                target, status, mod = occupied.get(reference, (None, "", None))
                if target is None:
                    resolved = installed[member]
                    mod = resolved.mod
                    if mod and mod.key in self.available_keys:
                        target, status = ModLocation("library", mod.key), "可用 · 尚未加入计划"
                    else:
                        status = resolved.problem or "当前安装已被其他计划条目使用"
                members.append(GroupMemberRow(
                    member, self.display_name(reference, mod.name if mod else member.name), target, status,
                    self.mod_tags(mod) if mod else (),
                ))
            output.append((group, tuple(members)))
        return output

    def find_mod_group(self, identity):
        return next((group for group in self.profile.mod_groups if group.id == identity), None)

    def export_group_template(self, identity):
        group = self.find_mod_group(identity)
        if not group:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "保存分组模板", export_filename(group.title, ".darksort-group.json"),
            "DarkSort 分组模板 (*.json)",
        )
        if path:
            try:
                write_group_template(group, self.export_destination(path, ".json"))
                self.statusBar().showMessage(f"已保存「{group.title}」的分组模板。", 5000)
            except (OSError, ValueError, TypeError) as exc:
                self.error("无法保存分组模板", str(exc))

    def import_group_template(self):
        path, _ = QFileDialog.getOpenFileName(self, "载入分组模板", "", "DarkSort 分组模板 (*.json)")
        if not path:
            return
        try:
            group = group_from_template(self.profile, read_group_template(Path(path)))
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            self.error("无法载入分组模板", str(exc))
            return
        matches = transfer_matches(self.mods, [("组员顺序", group.members)], self.discovery is not None,
                                   show_all=True)
        if confirmation_dialog(
            self, "载入分组模板",
            f"将为「{self.profile.name}」新增分组「{group.title}」。\n"
            f"保存方案后保留。需要启用时，可从分组头将整组加入计划。\n\n{matches}", "加入分组",
        ) and self.save_mod_group(group, action="载入分组模板"):
            self.show_utility_page(self.utility.groups_page)
            self.statusBar().showMessage(f"已载入「{group.title}」。可撤销本次操作。", 5000)

    def relink_group_member(self, identity, reference):
        group = self.find_mod_group(identity)
        member = next((entry for entry in group.members if entry.reference == reference), None) if group else None
        if not member or not (mod := self.choose_installed_mod(member)):
            return
        replacement = replace(ProfileEntry.from_mod(mod, pinned=True), id=member.id)
        if any(entry.reference == replacement.reference for entry in group.members if entry.id != member.id):
            self.error("无法匹配组员", "所选 Mod 已经在这个分组内。请选择对应的另一项。")
            return
        updated = replace(group, members=tuple(replacement if entry.id == member.id else entry for entry in group.members))
        self.save_mod_group(updated, action="手动匹配组员")

    def save_mod_group(self, group, *, action=None):
        groups = [group if existing.id == group.id else existing for existing in self.profile.mod_groups]
        if group not in groups:
            groups.append(group)
        updated = replace(self.profile, mod_groups=groups)
        try:
            parse_profile(profile_dict(updated))
        except (TypeError, ValueError) as exc:
            self.error("无法保留分组", str(exc))
            return False
        self.edit_profile(action or ("编辑分组" if self.find_mod_group(group.id) else "新建分组"), updated)
        return True

    def edit_mod_group(self, identity=None, *, members=(), name=""):
        group = self.find_mod_group(identity) if identity else ModGroup(
            name, MARK_COLORS[len(self.profile.mod_groups) % len(MARK_COLORS)][1], tuple(members),
        )
        if group is None:
            return
        dialog = ModGroupDialog(group, self.display_name, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and self.save_mod_group(dialog.result_group()):
            self.show_utility_page(self.utility.groups_page)
        dialog.deleteLater()

    def group_selected(self):
        indices = expand_selection(self.plan_units, self.selected_profile_indices())
        members = [self.profile.entries[index] for index in indices]
        self.edit_mod_group(members=self.unique_group_members(members))

    @staticmethod
    def unique_group_members(members):
        unique = {}
        for member in members:
            unique.setdefault(member.reference, member)
        return tuple(unique.values())

    def fill_group_menu(self, menu, members):
        members = self.unique_group_members(members)
        menu.addAction("新建分组…", lambda: self.edit_mod_group(members=members))
        if self.profile.mod_groups:
            menu.addSeparator()
        for group in self.profile.mod_groups:
            contains = {member.reference for member in group.members}
            action = menu.addAction(
                icon("dot", group.color), f"加入 {group.title}",
                lambda _checked=False, identity=group.id: self.add_group_members(identity, members),
            )
            action.setEnabled(any(member.reference not in contains for member in members))

    def add_group_members(self, identity, members):
        group = self.find_mod_group(identity)
        if group and self.save_mod_group(
            replace(group, members=self.unique_group_members((*group.members, *members))), action="加入分组"
        ):
            self.show_utility_page(self.utility.groups_page)

    def open_detail_group_menu(self):
        if not self._detail_target:
            return
        mod, row = self.location_details(self._detail_target)
        member = row.entry if row else ProfileEntry.from_mod(mod, pinned=True)
        button = self.detail_group_button
        menu = QMenu(self)
        menu.aboutToHide.connect(menu.deleteLater)
        self.fill_group_menu(menu, (member,))
        menu.popup(button.mapToGlobal(button.rect().bottomLeft()))

    def remove_group_member(self, identity, reference):
        group = self.find_mod_group(identity)
        if group:
            self.save_mod_group(replace(group, members=tuple(
                member for member in group.members if member.reference != reference
            )), action="移出分组")

    def delete_mod_group(self, identity):
        group = self.find_mod_group(identity)
        if not group:
            return
        selector = f"group:{group.id}"
        def uses_group(rule):
            return selector in (rule.first, rule.second)

        question = f"删除「{group.title}」？组员会保留在各自列表里。"
        if any(rule.enabled and uses_group(rule) for rule in self.profile.rules):
            question += "\n使用此分组的排序规则会停用。"
        if QMessageBox.question(self, "删除 Mod 分组", question) != QMessageBox.StandardButton.Yes:
            return
        self.edit_profile("删除分组", replace(
            self.profile, mod_groups=[existing for existing in self.profile.mod_groups if existing.id != identity],
            rules=[replace(rule, enabled=False) if uses_group(rule) else rule for rule in self.profile.rules],
        ))

    def apply_mod_group(self, identity, side, boundary=None):
        group = self.find_mod_group(identity)
        if not group:
            return
        try:
            if side == "profile":
                if boundary is not None and not self.can_reorder_manually():
                    return
                updated, skipped = enable_group(self.profile, group, resolve_entries(list(group.members), self.mods), boundary)
                if len(updated.entries) + len(updated.staged) > MAX_ENTRIES:
                    raise ValueError(f"计划和暂存合计最多 {MAX_ENTRIES} 项。")
                self.edit_profile("整组加入计划", updated)
                message = f"「{group.title}」已按分组顺序加入计划 · {len(group.members) - skipped} 项。"
                if skipped:
                    message += f" {skipped} 项未匹配本机安装，已跳过。"
            elif side == "library":
                members = members_in_plan(group, self.profile.entries)
                check_complete_locks(self.profile, members.values())
                staged_before = len(self.profile.staged)
                self.remove_indices([self.plan_positions[entry.id] for entry in members.values()],
                                    reveal_staged=False, action="整组移出计划")
                message = f"「{group.title}」已移出计划 · {len(members)} 项。"
                if len(self.profile.staged) > staged_before:
                    message += " 联锁成员已一起放入暂存。"
            else:
                return
            self.statusBar().showMessage(message, 5500)
        except ValueError as exc:
            self.statusBar().showMessage(str(exc), 6500)

    def toggle_group(self, item):
        data = item.data(ROLE)
        anchor = data.get("group")
        if anchor:
            if self.plan_is_filtered():
                self.statusBar().showMessage("筛选时会展开匹配分组。", 3500)
                return
            self.collapsed_groups.symmetric_difference_update({anchor})
            self.render_lists()
        else:
            self.show_selected_detail("profile", item)
            if self.problem_filter and data.get("problem"):
                self.inspect_problem(data["entry_id"])

    def edit_group(self):
        indices = self.selected_profile_indices()
        if not indices:
            self.statusBar().showMessage("选中一条计划条目作为分组起点。", 4500)
            return
        self.edit_group_at(indices[0])

    def edit_group_at(self, index):
        entry = self.profile.entries[index]
        title = next((group.title for group in self.profile.groups if group.anchor == entry.id), "")
        text, ok = QInputDialog.getText(
            self, "计划分组", "分组名称（留空移除标题）", text=title
        )
        if not ok:
            return
        if len(text.strip()) > 120:
            self.error("分组名称过长", "请输入 120 字以内的组名。")
            return
        if text.strip() == title:
            return
        groups = [group for group in self.profile.groups if group.anchor != entry.id]
        self.collapsed_groups.discard(entry.id)
        if text.strip():
            groups.append(ProfileGroup(entry.id, text.strip()))
        self.edit_profile("修改计划分组标题", replace(self.profile, groups=groups))

    def open_mod_menu(self, side, data, point):
        plan = side == "profile"
        view = self.profile_list if plan else self.library_list
        menu = QMenu(view)
        menu.setObjectName("modContextMenu")
        menu.aboutToHide.connect(menu.deleteLater)
        if "group" in data:
            menu.addAction("编辑分组标题…", lambda: self.edit_group_at(data["value"]))
            group, end = self.groups_by_index()[data["value"]]
            members = self.unique_group_members(self.profile.entries[data["value"]:end])
            menu.addAction("存为可拖动分组…", lambda: self.edit_mod_group(members=members, name=group.title))
        else:
            location = ModLocation(side, data["entry_id"] if plan else data["value"])
            self.inspect_selection(location)
            clicked_mod, clicked_row = self.location_details(location)
            selected = self.selected_profile_indices() if plan else []
            indices = expand_selection(self.plan_units, selected) if plan else []
            mods = (
                tuple(dict.fromkeys(self.resolved[i].mod for i in selected if self.resolved[i].mod))
                if plan else tuple(self.mods_by_key[key] for key in self.selected_library_keys())
            )
            if plan and len(selected) == 1:
                mods = (clicked_mod,) if clicked_mod else ()
            members = ([self.profile.entries[index] for index in indices] if plan
                       else [ProfileEntry.from_mod(mod, pinned=True) for mod in mods])
            if members:
                self.fill_group_menu(menu.addMenu("分组管理"), members)
            if mods:
                if self.tags:
                    menu.addAction(
                        "编辑标签…" if len(mods) == 1 else "批量添加标签…",
                        lambda: self.change_tags(mods, append=len(mods) > 1),
                    )
                colors = menu.addMenu("标记颜色")
                for title, color in MARK_COLORS:
                    colors.addAction(icon("dot", color), title, lambda _checked=False, color=color: self.mark_mods(mods, color))
                colors.addAction("清除颜色", lambda: self.mark_mods(mods, ""))
                menu.addAction("修改类型…", lambda: self.change_classification(mods))
            if plan:
                self.add_plan_menu_actions(menu, data, indices)
            else:
                menu.addSeparator()
                menu.addAction("加入计划", self.add_selected)
                menu.addAction("移入暂存篮", self.stage_available)
            identifier = workshop_id(clicked_mod.workshop_id if clicked_mod else None) or workshop_id(
                clicked_row.entry.workshop_id if clicked_row else None
            )
            menu.addSeparator()
            if clicked_mod:
                menu.addAction("别名与中文简介…", lambda: self.edit_mod_text(clicked_mod))
                menu.addAction("打开 Mod 文件夹", lambda: self.open_mod_folder(mod=clicked_mod))
            if plan:
                menu.addAction("匹配此 Mod…", lambda: self.relink_plan_entry(location.identity))
            if identifier:
                menu.addAction("打开创意工坊", lambda: self.open_workshop(identifier=identifier))
                self.add_workshop_menu_actions(menu, identifier=identifier)
        menu.popup(point)

    def add_plan_menu_actions(self, menu, data, indices):
        menu.addAction("添加或编辑分组标题…", lambda: self.edit_group_at(indices[0]))
        reference = self.profile.entries[data["value"]].reference
        menu.addAction("添加排序规则…", lambda: self.edit_rules(first=reference))
        menu.addSeparator()
        locking = menu.addAction(icon("link"), "联锁选中项", lambda: self.lock_selected(quick=True))
        members = {self.profile.entries[i].id for i in indices}
        locked = bool(members.intersection(self.lock_by_entry))
        locking.setEnabled(
            len(indices) >= 2 and not locked
            and (self.plan_is_filtered() or not self.profile_list.reorder_blocked)
        )
        unlocking = menu.addAction("解除联锁", self.unlock_selected)
        unlocking.setEnabled(locked)
        if data["entry_id"] in self.problem_levels:
            menu.addSeparator()
            menu.addAction("看看这个 Mod 有什么问题", lambda: self.inspect_problem(data["entry_id"]))
        menu.addSeparator()
        menu.addAction("移入暂存篮", self.stage_plan)
        menu.addAction("移出计划", self.remove_selected)

    def add_selected(self):
        self.add_keys(self.selected_library_keys())

    def selected_library_keys(self):
        return [
            item.data(ROLE)["value"]
            for item in sorted(self.library_list.selectedItems(), key=self.library_list.row)
        ]

    def add_visible(self):
        self.add_keys([
            record.target.identity for record in self.search_records["library"]
            if record.target in self.matching_targets["library"]
        ])

    def add_keys(self, keys, target=None):
        entries = self.new_entries(keys)
        if entries:
            target = len(self.profile.entries) if target is None else snap_boundary(self.plan_units, target)
            current = self.profile.entries
            self.edit_profile("加入 Mod", replace(self.profile, entries=current[:target] + entries + current[target:]))

    def new_entries(self, keys):
        used = {entry.mod.key for entry in self.resolved if entry.mod} | self.staged_keys.keys()
        entries = []
        for key in dict.fromkeys(key for key in keys if isinstance(key, str)):
            if key in self.mods_by_key and key not in used:
                entries.append(ProfileEntry.from_mod(self.mods_by_key[key]))
                used.add(key)
        if len(self.profile.entries) + len(self.profile.staged) + len(entries) > MAX_ENTRIES:
            self.statusBar().showMessage(f"计划和暂存合计最多 {MAX_ENTRIES} 项。", 5000)
            return []
        return entries

    def selected_profile_indices(self):
        return sorted(item.data(ROLE)["value"] for item in self.profile_list.selectedItems())

    def remove_selected(self):
        self.remove_indices(self.selected_profile_indices())

    def remove_indices(self, indices, *, stage=False, reveal_staged=True, action=None):
        indices = expand_selection(self.plan_units, indices)
        if not indices:
            return
        entries = self.profile.entries
        staged = [entries[i] for i in indices if stage or entries[i].id in self.lock_by_entry]
        selected = set(indices)
        groups = []
        for index, (group, end) in self.groups_by_index().items():
            retained = next((i for i in range(index, end) if i not in selected), None)
            if retained is not None:
                groups.append(ProfileGroup(entries[retained].id, group.title))
        self.edit_profile(action or ("暂存 Mod" if stage else "移出计划"), replace(
            self.profile, groups=groups, entries=[entry for i, entry in enumerate(entries) if i not in selected],
            staged=self.profile.staged + staged,
        ))
        if staged and reveal_staged:
            self.show_stage({entry.id for entry in staged})
            self.statusBar().showMessage(f"{len(staged)} 项已移入暂存篮。保存方案后保留。", 4500)

    def stage_available(self):
        selected = self.selected_library_keys()
        if not selected:
            self.statusBar().showMessage("请从可用列表选中要暂存的 Mod。", 4000)
            return
        entries = self.new_entries(selected)
        if entries:
            self.edit_profile("暂存 Mod", replace(self.profile, staged=self.profile.staged + entries))
            self.show_stage({entry.id for entry in entries})

    def stage_plan(self):
        self.remove_indices(self.selected_profile_indices(), stage=True)

    def stage_members(self):
        return {member for item in self.utility.staging.selectedItems() for member in item.data(ROLE)}

    def show_stage(self, selected):
        self.show_utility_page(self.utility.stage_page)
        staging = self.utility.staging
        first = None
        with QSignalBlocker(staging):
            for index in range(staging.count()):
                item = staging.item(index)
                matched = bool(selected.intersection(item.data(ROLE)))
                item.setSelected(matched)
                if matched and first is None:
                    first = item
        if first is not None:
            staging.setCurrentItem(first, QItemSelectionModel.SelectionFlag.NoUpdate)
            staging.scrollToItem(first, QAbstractItemView.ScrollHint.EnsureVisible)
        self.utility.update_stage_buttons()

    def add_staged(self):
        members = self.stage_members()
        if not members:
            return
        entries = [row for row in self.staged_resolved if row.entry.id in members]
        used_keys = {row.mod.key for row in self.resolved if row.mod}
        for row in entries:
            if row.mod and row.mod.key in used_keys:
                self.statusBar().showMessage(f"{row.entry.name} 已在计划中。请先处理重复项。", 5000)
                return
            if row.mod:
                used_keys.add(row.mod.key)
        start = len(self.profile.entries)
        self.edit_profile("从暂存加入计划", replace(
            self.profile, entries=self.profile.entries + [row.entry for row in entries],
            staged=[entry for entry in self.profile.staged if entry.id not in members],
        ))
        self.reveal_entry(start)
        self.statusBar().showMessage(f"已按暂存顺序将 {len(entries)} 项加入计划末尾。", 5000)

    def release_staged(self):
        members = self.stage_members()
        if not members:
            return
        if members.intersection(self.lock_by_entry):
            self.statusBar().showMessage("请先解除选中项的联锁，再移回可用库。", 5000)
            return
        self.edit_profile("从暂存移回可用库", replace(
            self.profile, staged=[entry for entry in self.profile.staged if entry.id not in members],
        ))

    def lock_selected(self, *, quick=False):
        indices = self.selected_profile_indices()
        if len(indices) < 2:
            self.statusBar().showMessage("在计划列表中至少选中两个 Mod。", 4500)
            return
        entries = self.profile.entries
        members = tuple(entries[i].id for i in indices)
        if any(member in self.lock_by_entry for member in members):
            self.statusBar().showMessage("部分条目已联锁。解除原有联锁后可重新组合。", 4500)
            return
        filtered = self.plan_is_filtered()
        if not filtered and not self.can_reorder_manually():
            return
        start = indices[0]
        title = f"{entries[start].name[:102]} · {len(indices)} 项"
        ordered = move_entries(entries, indices, start)
        if (quick or filtered) and ordered != entries:
            previous = {entry.id: i for i, entry in enumerate(entries, 1)}
            changes = [
                f"#{previous[entry.id]:02d} → #{i:02d}  {self.display_name(entry.reference, entry.name)}"
                for i, entry in enumerate(ordered, 1) if previous[entry.id] != i
            ]
            if not confirmation_dialog(
                self, "先看看哪些 Mod 会挪动",
                "这些 Mod 分开在列表里。联锁会把它们移到第一项旁边。\n"
                "以下是完整计划中的位置变化。列表靠上的 Mod 优先使用。\n\n" + "\n".join(changes),
                "移动并联锁",
            ):
                return
        if not quick:
            title, accepted = QInputDialog.getText(
                self, "创建联锁", "输入联锁名称。选中项将按当前顺序相邻放置。", text=title,
            )
            if not accepted or not title.strip():
                return
            if len(title.strip()) > 120:
                self.error("联锁名称过长", "请输入 120 字以内的名称。")
                return
        self.edit_profile("联锁 Mod", replace(
            self.profile, entries=ordered, locks=[*self.profile.locks, ProfileLock(title.strip(), members)],
        ))
        self.profile_list.flash_lock(members[0])
        self.statusBar().showMessage(f"已联锁 {len(members)} 个 Mod。它们现在会一起移动。", 4500)
        if not quick:
            self.show_utility_page(self.utility.groups_page)

    def unlock_selected(self, *, staged=False):
        members = self.stage_members() if staged else {
            self.profile.entries[index].id for index in self.selected_profile_indices()
        }
        remaining = [lock for lock in self.profile.locks if not members.intersection(lock.members)]
        if remaining == self.profile.locks:
            self.statusBar().showMessage("选中一个联锁内的条目即可解除整组联锁。", 4500)
            return
        self.edit_profile("解除联锁", replace(self.profile, locks=remaining))
        self.statusBar().showMessage("已解除联锁。每个 Mod 可以单独移动。", 4500)

    def handle_drop(self, side, payload, boundary):
        if not isinstance(payload, dict):
            return
        if payload.get("side") == "group":
            if payload.get("profile_id") == self.profile.id and isinstance(payload.get("group_id"), str):
                self.apply_mod_group(payload["group_id"], side, boundary if side == "profile" else None)
            return
        if not isinstance(payload.get("values"), list):
            return
        values = payload["values"]
        if side == "library" and payload.get("side") == "profile":
            self.remove_indices([i for i in values if isinstance(i, int)])
        elif side == "profile":
            if not self.can_reorder_manually():
                return
            if payload.get("side") == "library":
                self.add_keys(values, target=boundary)
            elif payload.get("side") == "profile":
                indices = expand_selection(self.plan_units, [i for i in values if isinstance(i, int)])
                ordered = move_entries(
                    self.profile.entries, indices, snap_boundary(self.plan_units, boundary)
                )
                self.edit_profile("移动 Mod", replace(self.profile, entries=ordered))

    def nudge(self, direction):
        if not self.can_reorder_manually():
            return
        indices = self.selected_profile_indices()
        if not indices:
            return
        entries = self.profile.entries
        order = nudge_units(self.plan_units, indices, direction)
        self.edit_profile("移动 Mod", replace(self.profile, entries=[entries[index] for index in order]))

    def can_reorder_manually(self):
        collapsed = self.collapsed_groups.intersection(group.anchor for group in self.profile.groups)
        if self.plan_is_filtered() or collapsed:
            self.statusBar().showMessage("请先清除筛选并展开分组，再调整顺序。", 5000)
            return False
        return True

    def plan_is_filtered(self):
        return bool(self.problem_filter or self.profile_search.filtering)

    def relink_selected(self):
        indices = self.selected_profile_indices()
        if len(indices) != 1:
            self.statusBar().showMessage("请在当前配置中选中一个需要定位的条目。", 5000)
            return
        self.relink_plan_entry(self.profile.entries[indices[0]].id)

    def relink_plan_entry(self, identity):
        index = self.plan_positions.get(identity)
        if index is None:
            return
        previous = self.profile.entries[index]
        if not (mod := self.choose_installed_mod(previous)):
            return
        if mod.key in self.staged_keys:
            self.show_stage({self.staged_keys[mod.key]})
            self.statusBar().showMessage("此 Mod 在暂存篮中。移出暂存后可用于匹配。", 5500)
            return
        updated = replace(ProfileEntry.from_mod(mod, pinned=True), id=previous.id)
        rules, groups, disabled = self.profile.rules, self.profile.mod_groups, 0
        if sum(entry.reference == previous.reference for entry in self.profile.entries + self.profile.staged) == 1:
            rules = []
            for rule in self.profile.rules:
                retargeted = replace(
                    rule,
                    first=updated.reference if rule.first == previous.reference else rule.first,
                    second=updated.reference if rule.second == previous.reference else rule.second,
                )
                if retargeted.first == retargeted.second:
                    rules.append(replace(rule, enabled=False))
                    disabled += 1
                else:
                    rules.append(retargeted)
            groups = [
                replace(group, members=self.unique_group_members(
                    replace(updated, id=member.id) if member.reference == previous.reference else member
                    for member in group.members
                )) for group in self.profile.mod_groups
            ]
        entries = self.profile.entries.copy()
        entries[index] = updated
        self.edit_profile("手动定位 Mod", replace(self.profile, entries=entries, rules=rules, mod_groups=groups))
        if disabled:
            self.statusBar().showMessage(f"已停用 {disabled} 条指向同一 Mod 的规则。", 5000)

    def choose_installed_mod(self, entry: ProfileEntry) -> Mod | None:
        if not self.mods:
            self.statusBar().showMessage("请先配置扫描路径并扫描 Mod。", 5000)
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("手动定位 Mod")
        dialog.resize(650, 480)
        layout = QVBoxLayout(dialog)
        prompt = label(f"为「{entry.name}」选择对应的已扫描 Mod。")
        prompt.setTextFormat(Qt.TextFormat.PlainText)
        prompt.setWordWrap(True)
        layout.addWidget(prompt)
        search = QLineEdit()
        search.setPlaceholderText("搜索名称或目录")
        layout.addWidget(search)
        choices = QListWidget()
        choices.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        for mod in sorted(self.mods, key=lambda m: m.name.casefold()):
            item = QListWidgetItem(f"{mod.name}\n{mod.source.label} · {mod.path}")
            item.setData(ROLE, mod)
            choices.addItem(item)
        layout.addWidget(choices)

        def filter_choices(text):
            for index in range(choices.count()):
                item = choices.item(index)
                item.setHidden(text.casefold() not in item.text().casefold())

        search.textChanged.connect(filter_choices)
        buttons = QDialogButtonBox()
        buttons.addButton("使用所选 Mod", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        selected = None
        if (
            dialog.exec() == QDialog.DialogCode.Accepted
            and choices.currentItem()
            and not choices.currentItem().isHidden()
        ):
            selected = self.mods_by_key.get(choices.currentItem().data(ROLE).key)
        dialog.deleteLater()
        return selected

    def rule_search_targets(self) -> dict[str, SearchRecord]:
        by_reference = defaultdict(list)
        for side in ("profile", "staging", "library"):
            for record in self.search_records[side]:
                mod, row = self.location_details(record.target)
                reference = row.entry.reference if row else mod.reference
                subject = mod or row.entry
                if mod is None:
                    record = replace(record, location=f"{record.location} · 待匹配")
                by_reference[reference].append((record, subject.name, subject.path))

        name_counts = Counter(
            name for variants in by_reference.values() for name in set(variants[0][0].titles)
        )
        targets = {}
        for reference, variants in by_reference.items():
            primary, original, _ = variants[0]
            title = f"{primary.title}（{original}）" if primary.title != original else primary.title
            names = tuple(dict.fromkeys(name for record, _, _ in variants for name in record.titles))
            locations = dict.fromkeys(record.location for record, _, _ in variants)
            paths = dict.fromkeys(path for _, _, path in variants if path)
            caption = " / ".join(locations)
            if paths and (len(paths) > 1 or any(name_counts[name] > 1 for name in names)):
                caption += "\n" + "\n".join(paths)
            targets[reference] = replace(
                primary, target=ModLocation("rule", reference), title=title, location=caption,
                titles=names, details=search_text(" ".join(paths)),
            )

        tags = [tag for mod in self.mods for tag in self.mod_tags(mod)]
        if self.tags:
            tags.extend(tag for values in self.tags.tags.values() for tag in values)
        labels = target_labels(self.profile, tags, self.settings.mod_colors.values())
        for reference, caption in sorted(labels.items(), key=lambda item: search_text(item[1])):
            kind, _, name = caption.partition(" · ")
            targets[reference] = SearchRecord.create(
                ModLocation("rule", reference), caption, kind, (caption, name), None, kind, "",
            )
        return targets

    def edit_rules(self, *, first="", second=""):
        dialog = RuleDialog(self.profile.rules, self.rule_search_targets(), self, first=first, second=second)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.edit_profile("编辑排序规则", replace(self.profile, rules=dialog.result_rules()))

    def edit_conflict_rule(self, first_id, second_id):
        if first_id in self.plan_positions and second_id in self.plan_positions:
            first = self.profile.entries[self.plan_positions[first_id]].reference
            second = self.profile.entries[self.plan_positions[second_id]].reference
            self.edit_rules(first=first, second=second)

    def configure_ai(self):
        previous = self.ai_provider
        dialog = AISettingsDialog(self.ai_store, self.ai_provider, self.ai_warning, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.provider is not None:
            self.ai_provider = dialog.provider
            self.ai_warning = ""
            self.statusBar().showMessage("AI 接入设置已保存。", 3500)
            if self.assistant:
                self.assistant.provider_changed(previous)
        dialog.deleteLater()

    def ai_ready(self):
        if not self.ai_provider.configured:
            self.configure_ai()
        return self.ai_provider.configured

    def organize_logs(self):
        self.open_ai_assistant(mode="log")

    def ai_sort_profile(self):
        self.open_ai_assistant(mode="sort")

    def open_ai_assistant(self, *, mode=None):
        if not self.assistant:
            self.assistant = AssistantPanel(self)
        if mode:
            self.assistant.set_mode(mode)
        self.assistant.show()
        self.assistant.raise_()
        self.assistant.activateWindow()
        return self.assistant

    def ask_about_problem(self, problem):
        assistant = self.open_ai_assistant()
        assistant.attach_problem(problem, self.utility.conflict_panel.entries)

    def assistant_plan_selection(self) -> str:
        target = self._detail_target
        return target.identity if target and target.side == "profile" else ""

    def rule_marks(self):
        return {"tags": self.tags.tags if self.tags else {}, "colors": self.settings.mod_colors}

    def prepare_ai_sort(self, goal):
        if len(self.resolved) < 2:
            raise AIError("计划里至少有两个 Mod 才能排序。")
        local = preview_order(self.profile, self.resolved, self.classifications, **self.rule_marks())
        if local.blockers:
            raise AIError("当前排序规则互相冲突。请先打开「用户规则」处理。")
        context = (profile_dict(self.profile), self.scan_result, self._classification_revision, self._order_marks_revision)
        payload = sort_payload(
            self.profile, self.resolved, self.classifications, local, self.ai_provider.api_key, goal=goal,
            **self.rule_marks(),
        )
        return payload, len(self.resolved), context

    def review_ai_sort(self, proposal, context):
        preview = preview_ai_order(self.profile, self.resolved, self.classifications, proposal, **self.rule_marks())
        return self.review_sort(preview, title="AI 排序预览", snapshot_reason="AI 排序前", context=context)

    def sort_context_matches(self, context):
        return (
            self.scan_result is context[1] and self._classification_revision == context[2]
            and self._order_marks_revision == context[3] and profile_dict(self.profile) == context[0]
        )

    def sort_profile(self, *, rules_only=False):
        preview = preview_order(
            self.profile, self.resolved, None if rules_only else self.classifications, **self.rule_marks(),
        )
        self.review_sort(preview, rules_only=rules_only)

    def review_sort(self, preview, *, rules_only=False, title=None, snapshot_reason="排序前", context=None):
        self.dock.set_sort(preview)
        self.show_dock(self.dock.sort[0])
        dialog = SortPreviewDialog(preview, rules_only=rules_only, title=title, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted or preview.blockers:
            return
        if not any(row.before != row.after for row in preview.decisions):
            return
        if context and not self.sort_context_matches(context):
            self.statusBar().showMessage("方案或扫描结果已变化，请生成新的排序预览。", 5500)
            return
        try:
            self.history.capture(self.profile, snapshot_reason)
        except (OSError, ValueError) as exc:
            self.error("未能保存排序前快照", str(exc))
            return
        self.edit_profile("采用建议顺序", replace(self.profile, entries=list(preview.entries)))
        self.dock.set_sort(preview)
        self.refresh_history_panel()
        self.statusBar().showMessage("已采用建议顺序。保存方案后保留。", 4500)
        return True

    def refresh_history_panel(self):
        snapshots, _ = self.history.list(self.profile.id)
        self.utility.set_history(snapshots)

    def show_history(self, *, snapshot_id=None):
        self.show_utility_page(self.utility.history_page)
        dialog = HistoryDialog(self.history, self.profile, self, snapshot_id=snapshot_id)
        result = dialog.exec()
        self.refresh_history_panel()
        if result != QDialog.DialogCode.Accepted or not dialog.restored:
            return
        if profile_dict(dialog.restored) == profile_dict(self.profile):
            return
        try:
            self.history.capture(self.profile, "恢复快照前")
        except (OSError, ValueError) as exc:
            self.error("无法保存恢复前快照", str(exc))
            return
        self.collapsed_groups.clear()
        self.edit_profile("载入历史快照", dialog.restored)
        self.refresh_history_panel()
        self.statusBar().showMessage("已载入快照。保存方案后保留。", 4500)

    def show_utility_page(self, page):
        if self.utility.isHidden():
            self.toggle_utility()
        self.utility.tabs.setCurrentWidget(page)

    def refresh_plan_problems(self):
        scope = tuple((row.entry.id, row.mod.key if row.mod else "") for row in self.resolved)
        file_scope = (self.profile.id, frozenset(scope))
        if file_scope != self._file_scope:
            self._file_scope = file_scope
            self.queue_conflict_check()
        sort_basis = (
            scope, tuple(self.profile.rules), tuple(self.profile.locks), tuple(self.profile.mod_groups),
            self._classification_revision, self._order_marks_revision,
        )
        if sort_basis != self._sort_basis:
            self._sort_basis = sort_basis
            names = {mod.reference: mod.name for mod in self.mods}
            self.plan_preview = preview_order(
                self.profile, self.resolved, self.classifications, installed_names=names, **self.rule_marks(),
            ) if self.resolved else None
            self.dock.set_sort(self.plan_preview)

        basis = (sort_basis, self._file_version, self.file_report is not None)
        if basis != self._problem_basis:
            self._problem_basis = basis
            self.ordered_report = reorder_conflicts(self.file_report, self.resolved) if self.file_report else None
            files = tuple(file_problems(self.resolved, self.ordered_report)) if self.ordered_report else ()
            problems = files
            if self.discovery is not None:
                extra = self.plan_preview.problems if self.plan_preview else ()
                problems = (*installation_problems(self.resolved, self.ordered_report), *extra, *files)
            self.plan_problems = tuple(sorted(problems, key=lambda problem: problem.level))
            self.file_problems = files
        self.update_problem_controls()

    def update_problem_controls(self):
        ignored = frozenset(self.settings.ignored_problems.get(self.profile.id, ()))
        self.problem_levels = {
            identity: problem.level
            for problem in reversed(self.plan_problems)
            if problem.level != "review" and problem.key not in ignored
            for identity, _ in problem.targets
        }
        self.file_warning_ids = {
            identity for problem in self.file_problems if problem.key not in ignored
            for identity, _ in problem.targets
        }
        totals = Counter(self.problem_levels.values())
        checking = self.file_report is None and any(row.mod for row in self.resolved)
        if self.problem_filter and not checking and not totals[self.problem_filter]:
            self.problem_filter = ""
            if not self.utility.conflict_panel.show_ignored:
                self.utility.conflict_panel.inspect()
        for level, control in self.problem_buttons.items():
            text = "需处理" if level == "error" else "警告"
            control.setText(f"{text} {totals[level]}")
            control.setChecked(self.problem_filter == level)
            control.setEnabled(bool(totals[level]) or self.problem_filter == level)
        self.clear_problem_button.setVisible(bool(self.problem_filter))
        self.priority_label.setVisible(not self.problem_filter)
        total = len(self.problem_levels)
        if not self.resolved:
            status = "把 Mod 加到计划里，就可以检查。"
        elif self.discovery is None:
            status = "扫描本机 Mod 后，就可以检查这份方案。"
        elif checking:
            status = "正在后台检查 Mod 文件…"
        elif self.file_report and self.file_report.failed:
            status = "有些文件没查完。可以重新检查，或打开文件明细。"
        elif total:
            status = f"{totals['error']} 个需处理 · {totals['warning']} 个有警告"
        elif ignored:
            status = "当前没有显示黄标或红标。可在「已忽略」里恢复。"
        elif self.file_report and self.file_report.issues:
            status = "没发现需要标记的问题。部分文件未能完整检查，可查看文件明细。"
        else:
            status = "当前检查没发现需要标记的问题。"
        entries = {row.entry.id: (index, row.entry.name) for index, row in enumerate(self.resolved, 1)}
        self.conflict_button.setText(f"冲突排查 ({total})" if total else "冲突排查")
        self.utility.conflict_panel.set_state(
            self.plan_problems, entries, self.ordered_report, status, ignored, checking=checking,
        )
        tab = self.utility.tabs.indexOf(self.utility.conflict_panel)
        self.utility.tabs.setTabText(tab, f"冲突 {total}" if total else "冲突")

    def change_ignored_problems(self, keys, ignoring):
        ignored = self.settings.ignored_problems.copy()
        previous = set(ignored.get(self.profile.id, ()))
        allowed = {problem.key for problem in self.plan_problems}
        updated_keys = previous | (set(keys) & allowed) if ignoring else previous - set(keys)
        if updated_keys == previous:
            return
        if updated_keys:
            ignored[self.profile.id] = sorted(updated_keys)
        else:
            ignored.pop(self.profile.id, None)
        updated = replace(self.settings, ignored_problems=ignored)
        try:
            updated.save(self.data_dir)
        except OSError as exc:
            self.error("提醒设置保存失败", str(exc))
            return
        self.settings = updated
        self.render_lists()
        action = "已忽略" if ignoring else "已恢复"
        self.statusBar().showMessage(f"{action} {len(previous ^ updated_keys)} 条提醒。", 4000)

    def queue_conflict_check(self):
        self._file_version += 1
        self.file_report = None
        if self.file_worker:
            self.file_worker.requestInterruption()
        self._file_timer.stop()
        if not self._close_pending and any(row.mod for row in self.resolved):
            self._file_timer.start()

    def start_conflict_check(self):
        if self.file_worker or self._close_pending or not any(row.mod for row in self.resolved):
            return
        self.file_worker = worker = ConflictWorker(self.resolved, self)
        worker.version = self._file_version
        worker.completed.connect(self.on_conflicts_completed)
        worker.progress.connect(self.on_conflict_progress)
        worker.finished.connect(self.on_conflict_finished)
        worker.start()

    def on_conflict_progress(self, done, total, _name):
        if self.file_worker and self.file_worker.version == self._file_version and not self._close_pending:
            self.utility.conflict_panel.status.setText(f"正在检查第 {done}/{total} 个 Mod…")

    def on_conflicts_completed(self, report):
        if (
            self.file_worker and self.file_worker.version == self._file_version
            and not report.cancelled and not self._close_pending
        ):
            self.file_report = report
            self.render_lists()

    def on_conflict_finished(self):
        worker, self.file_worker = self.file_worker, None
        if worker:
            worker.deleteLater()
        if self._close_pending:
            QTimer.singleShot(0, self.close)
        elif self.file_report is None:
            if worker and worker.version == self._file_version:
                self.file_report = ConflictReport(issues=("文件检查提前结束。",), failed=True)
                self.render_lists()
            elif not self._file_timer.isActive() and any(row.mod for row in self.resolved):
                self._file_timer.start()

    def recheck_conflicts(self):
        self.queue_conflict_check()
        self.render_lists()

    def open_conflict_panel(self):
        self.show_utility_page(self.utility.conflict_panel)

    def show_conflicts(self):
        self.utility.conflict_panel.inspect(level=self.problem_filter)
        self.open_conflict_panel()

    def inspect_problem(self, identity):
        if identity in self.problem_levels:
            self.navigate(ModLocation("profile", identity), focus=False)
            self.utility.conflict_panel.inspect(entry=identity)
            self.open_conflict_panel()

    def filter_problems(self, level=""):
        self.problem_filter = level if self.problem_filter != level else ""
        if self._temporary_target and self._temporary_target.side == "profile":
            self._temporary_target = None
        self.render_lists()
        self.show_conflicts()

    def jump_to_problem_mod(self, identity):
        self.navigate(ModLocation("profile", identity), focus=False)

    def game_locations(self):
        game_paths = list(self.discovery.games) if self.discovery else []
        if self.settings.game_path:
            game_paths.append(Path(self.settings.game_path))
        return game_paths

    def open_new_campaign(self, parent=None):
        targets = discover_launch_targets(self.game_locations())
        if not targets:
            QMessageBox.information(
                parent or self, "没有找到支持方案启动的游戏",
                "请在「工具 → 扫描路径」确认游戏位置，再扫描支持 Mod 清单启动的 DD1。",
            )
            return
        dialog = CampaignDialog(
            self.data_dir, targets, self.profile.name, self.resolved, self.mods, parent or self,
        )
        started = dialog.process if dialog.exec() == QDialog.DialogCode.Accepted else None
        dialog.deleteLater()
        if started is not None:
            self._launched_game = started
            self.statusBar().showMessage("已启动游戏 · 请在主菜单选择空档位或刚建的新档。", 12000)
            QTimer.singleShot(2500, self, self.check_game_start)

    def check_game_start(self):
        if self._launched_game and (code := self._launched_game.poll()) is not None:
            self._launched_game = None
            if code:
                self.statusBar().showMessage(f"游戏启动后退出（代码 {code}）。可核对游戏目录的 app.log。", 12000)

    def manage_saves(self):
        game_paths = self.game_locations()
        initial_path = None
        while True:
            dialog = SaveDialog(
                self.data_dir, self.settings.steam_paths, game_paths, self.mods, self.resolved, self,
                profile=self.profile, initial_path=initial_path, new_campaign=self.open_new_campaign,
                rule_marks=self.rule_marks(),
            )
            result = dialog.exec()
            state, switch_to, imported_state = dialog.state, dialog.switch_to, dialog.import_state
            dialog.deleteLater()
            self.refresh_history_panel()
            if result != QDialog.DialogCode.Accepted:
                return
            if not switch_to or state is None:
                break
            initial_path = state.path
            if not self.maybe_save():
                continue
            try:
                self.activate_profile(self.store.load(switch_to))
            except (OSError, ValueError, TypeError) as exc:
                self.error("无法切换到绑定方案", str(exc))
                return
        if not imported_state or not self.maybe_save():
            return
        try:
            imported = self.store.new(
                f"档位 · {imported_state.path.parent.name}", entries_from_slot(imported_state, self.mods)
            )
            self.activate_profile(imported)
            self.statusBar().showMessage("已从档位创建独立方案。", 4500)
        except (OSError, ValueError) as exc:
            self.error("无法从档位创建方案", str(exc))

    def save_profile(self):
        try:
            if self.dirty:
                self.history.capture(parse_profile(self._baseline), "保存方案前")
            self.store.save(self.profile)
        except (OSError, ValueError, TypeError) as exc:
            self.error("保存失败", str(exc))
            return False
        self._baseline = copy.deepcopy(profile_dict(self.profile))
        self.update_dirty()
        self.refresh_history_panel()
        self.remember_profile()
        self.statusBar().showMessage("方案已保存。", 3500)
        return True

    def maybe_save(self):
        if not self.dirty:
            return True
        box = QMessageBox(self)
        box.setWindowTitle("尚有未保存的配置")
        box.setText(f"“{self.profile.name}”有未保存的改动。")
        save = box.addButton("保存", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("放弃改动", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() == save:
            return self.save_profile()
        return box.clickedButton() == discard

    def switch_profile(self, index):
        target = self.profile_combo.itemData(index)
        if not target or target == self.profile.id:
            return
        if not self.maybe_save():
            self.populate_profiles()
            return
        try:
            profile = self.store.load(target)
            self.activate_profile(profile)
        except (OSError, ValueError, TypeError) as exc:
            self.error("切换失败", str(exc))
            self.populate_profiles()

    def activate_profile(self, profile):
        self.undo_stack.clear()
        self.profile = profile
        self._baseline = copy.deepcopy(profile_dict(profile))
        self.collapsed_groups.clear()
        self._temporary_target = None
        self.problem_filter = ""
        self.utility.conflict_panel.inspect()
        self.profile_search.clear_view()
        self.clear_detail()
        self.profile_list.clearSelection()
        self.populate_profiles()
        self.render_lists()
        self.refresh_history_panel()
        self.remember_profile()

    def remember_profile(self):
        self.settings.last_profile = self.profile.id
        try:
            self.settings.save(self.data_dir)
        except OSError as exc:
            self.error("无法记住当前方案", str(exc))

    def new_profile(self):
        if not self.maybe_save():
            return
        name, accepted = QInputDialog.getText(self, "新建配置", "配置名称：")
        if not accepted:
            return
        try:
            self.activate_profile(self.store.new(name))
        except (OSError, ValueError) as exc:
            self.error("新建失败", str(exc))

    def duplicate_profile(self):
        name, ok = QInputDialog.getText(
            self, "复制当前方案", "新方案名称", text=f"{self.profile.name} 副本"
        )
        if ok:
            try:
                self.store.duplicate(self.profile, name)
                self.populate_profiles()
                self.statusBar().showMessage("已复制当前编辑，可从方案下拉框切换。", 4000)
            except (OSError, ValueError) as exc:
                self.error("复制方案失败", str(exc))

    def import_profile(self):
        if not self.maybe_save():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "导入 DarkSort 配置", "", "DarkSort 配置 (*.json)"
        )
        if not path:
            return
        try:
            self.activate_profile(self.store.import_file(Path(path)))
            self.statusBar().showMessage("已导入为独立配置；待定位条目将保留原顺序。", 5000)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            self.error("导入失败", str(exc))

    def export_profile(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出当前配置", f"{self.profile.name}.json", "DarkSort 配置 (*.json)"
        )
        if not path:
            return
        try:
            destination = self.export_destination(path, ".json")
            self.store.export_file(self.profile, destination)
            self.statusBar().showMessage(f"已导出当前方案：{destination}", 5000)
        except (OSError, ValueError, TypeError) as exc:
            self.error("导出失败", str(exc))

    def export_destination(self, path: str, suffix: str) -> Path:
        destination = Path(path)
        if destination.suffix.lower() != suffix:
            raise ValueError(f"请使用以 {suffix} 结尾的导出文件名。")
        protected = [self.data_dir, user_documents_path() / "Darkest"]
        protected += [root / "userdata" for root in steam_locations()]
        protected += [Path(path) for path in self.settings.workshop_paths + self.settings.local_paths]
        if self.settings.game_path:
            protected.append(Path(self.settings.game_path))
        if self.discovery:
            protected += self.discovery.games + [root.path for root in self.discovery.roots]
            protected += [library / "userdata" for library in self.discovery.libraries]
        protected += [Path(path) / "userdata" for path in self.settings.steam_paths]
        resolved = destination.resolve()
        if any(resolved.is_relative_to(root.resolve()) for root in protected):
            raise ValueError("请将文件保存到游戏、Workshop、Steam 存档及应用数据目录之外。")
        return destination

    def export_share_package(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "生成方案分享包", export_filename(self.profile.name, ".darksort.zip"), "DarkSort 分享包 (*.zip)",
        )
        if not path:
            return
        try:
            destination = self.export_destination(path, ".zip")
            write_share_package(self.profile, destination)
            self.statusBar().showMessage(f"已生成分享包和创意工坊清单：{destination}", 5000)
        except (OSError, ValueError, TypeError) as exc:
            self.error("无法生成分享包", str(exc))

    def import_share_package(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入方案分享包", "", "DarkSort 分享包 (*.zip)")
        if not path:
            return
        try:
            profile = read_share_package(Path(path))
            existing, _ = self.store.list()
            profile = replace(profile, name=unique_title(profile.name, (plan.name for plan in existing)))
            current_refs = {entry.reference for entry in profile.entries + profile.staged}
            group_only = self.unique_group_members(
                entry for group in profile.mod_groups for entry in group.members if entry.reference not in current_refs
            )
            sections = [("计划", profile.entries), ("暂存", profile.staged), ("仅在分组中", group_only)]
            matches = transfer_matches(self.mods, sections, self.discovery is not None)
            note = ""
            if any(key.startswith(("tag:", "color:")) for rule in profile.rules for key in (rule.first, rule.second)):
                note = "\n标签和标记颜色规则会按本机标记重新计算。"
            if not confirmation_dialog(
                self, "导入方案分享包",
                f"将建立独立方案「{profile.name}」，保留原有顺序。\n"
                f"{len(profile.mod_groups)} 个分组 · {len(profile.locks)} 组联锁 · {len(profile.rules)} 条规则"
                f"{note}\n\n{matches}", "导入为新方案",
            ) or not self.maybe_save():
                return
            self.activate_profile(self.store.import_profile(profile))
            self.statusBar().showMessage("已导入为独立方案。待匹配条目会保留原位置。", 5000)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            self.error("无法导入分享包", str(exc))

    def edit_paths(self):
        dialog = PathDialog(self.settings, self.discovery, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                settings = dialog.result_settings()
                settings.save(self.data_dir)
                self.settings = settings
                self.start_scan()
            except OSError as exc:
                self.error("路径保存失败", str(exc))

    def start_scan(self, checked=False, *, refresh=False):
        if self.worker is not None:
            if self.worker.isRunning():
                self.worker.requestInterruption()
                self.scan_button.setText("正在取消…")
                self.scan_button.setEnabled(False)
            return
        self.worker = ScanWorker(
            copy.deepcopy(self.settings),
            self.data_dir / "scan-cache.sqlite3",
            refresh=refresh,
            parent=self,
        )
        self.worker.progress.connect(self.on_progress)
        self.worker.completed.connect(self.on_scan_completed)
        self.worker.failed.connect(lambda message: self.error("扫描失败", message))
        self.worker.finished.connect(self.on_scan_finished)
        self.scan_button.setText("取消扫描")
        self.path_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.progress_bar.setRange(0, 0)
        self.progress_bar.show()
        self.statusBar().showMessage("正在后台检测并扫描 Mod…")
        self.worker.start()

    def on_progress(self, done, total, name):
        if total:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(done)
        self.statusBar().showMessage(f"扫描 {done}/{total} · {name}")

    def on_scan_completed(self, discovery: Discovery, result: ScanResult):
        if result.cancelled:
            self.statusBar().showMessage("扫描已取消，保留上次完整结果。", 5000)
            return
        self.discovery = discovery
        self.scan_result = result
        self.mods = result.mods
        self._file_scope = None
        self.dock.set_issues(result)
        self.refresh_classifications()
        self.refresh_tag_options()
        self.detail_art.clear_cache()
        self.library_list.itemDelegate().thumbnails.clear()
        self.profile_list.itemDelegate().thumbnails.clear()
        self.render_lists()
        if not self.library_list.selectedItems() and not self.profile_list.selectedItems():
            first = self.profile_list if self.profile_list.count() else self.library_list
            for index in range(first.count()):
                if "group" not in first.item(index).data(ROLE):
                    first.setCurrentRow(index)
                    break
        game = str(discovery.games[0]) if discovery.games else "未找到游戏，可手动指定扫描目录"
        self.path_summary.setToolTip("\n".join([game, *(str(root.path) for root in discovery.roots)]))
        warnings = sum(bool(mod.warnings) for mod in result.mods)
        self.scan_summary.setText(
            f"已扫描 {len(result.mods)} 个 Mod  ·  缓存命中 {result.cache_hits}  ·  元数据提示 {warnings}"
        )
        issue_count = len(result.issues) + warnings
        self.issues_button.setText(f"扫描提示 ({issue_count})")
        self.issues_button.setEnabled(bool(issue_count))
        if not discovery.roots:
            self.path_summary.setText("DD1  ·  未找到 Mod 目录")
            self.path_summary.setToolTip("在「工具」›「扫描路径」指定游戏或 Mod 根目录。")

    def on_scan_finished(self):
        self.scan_button.setText("扫描")
        self.scan_button.setEnabled(True)
        self.path_button.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self.progress_bar.hide()
        if not self._close_pending:
            self.statusBar().showMessage("扫描结束。", 3000)
        worker = self.worker
        self.worker = None
        if worker:
            worker.deleteLater()
        if self._close_pending:
            QTimer.singleShot(0, self.close)

    def show_issues(self):
        self.show_dock(self.dock.issues[0])
        lines = [f"{issue.path}\n{issue.message}" for issue in self.scan_result.issues]
        for mod in self.mods:
            if mod.warnings:
                lines.append(f"{mod.name}\n{mod.path}\n" + "\n".join(mod.warnings))
        self.show_text("扫描提示", "\n\n".join(lines) or "暂无提示。")

    def show_startup_errors(self):
        self.show_text("启动提示", "\n\n".join(self.startup_errors))

    def show_text(self, title, text):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(760, 500)
        layout = QVBoxLayout(dialog)
        view = QPlainTextEdit(text)
        view.setReadOnly(True)
        layout.addWidget(view)
        close = button("关闭", dialog.accept)
        layout.addWidget(close)
        dialog.exec()

    def error(self, title, text):
        QMessageBox.critical(self, title, text)

    def closeEvent(self, event):
        if not self._close_pending and not self.maybe_save():
            event.ignore()
            return
        self._file_timer.stop()
        running = [worker for worker in (self.worker, self.file_worker) if worker and worker.isRunning()]
        if running:
            self._close_pending = True
            for worker in running:
                worker.requestInterruption()
            self.setEnabled(False)
            self.statusBar().showMessage("正在结束扫描后关闭…")
            event.ignore()
            return
        self.settings.last_profile = self.profile.id
        try:
            self.settings.save(self.data_dir)
        except OSError as exc:
            self.error("设置保存失败", str(exc))
            event.ignore()
            self._close_pending = False
            self.setEnabled(True)
            return
        if self.assistant:
            self.assistant.job.stop()
            self.assistant.hide()
        self.mod_text_batch.shutdown()
        event.accept()
