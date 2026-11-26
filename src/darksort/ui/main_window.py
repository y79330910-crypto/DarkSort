from __future__ import annotations

import copy
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from pathlib import Path

from platformdirs import user_documents_path
from PySide6.QtCore import QFile, QItemSelectionModel, QSignalBlocker, Qt, QTimer, QUrl
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
from darksort.adapters.saves import entries_from_slot, require_game_stopped
from darksort.ai_client import AIError, AIProvider, AISettingsStore
from darksort.ai_tasks import preview_ai_order, sort_payload
from darksort.classification import Category, Classification, classify_mod
from darksort.conflicts import ConflictReport, reorder_conflicts
from darksort.dependencies import check_removal, enable_requirements, required_edges, required_mods
from darksort.diagnostics import file_problems, installation_problems
from darksort.file_replacements import FilePackStore, ModOrigin
from darksort.history import ProfileHistory
from darksort.local_mods import copy_workshop_to_local, create_blank, local_installation, mod_parent
from darksort.locks import entry_units, expand_selection, lock_starts, nudge_units, snap_boundary
from darksort.mod_groups import (
    check_complete_locks,
    enable_group,
    groups_for_disabled_locks,
    members_in_plan,
)
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
    Source,
)
from darksort.profiles import (
    DEFAULT_PROFILE_NAME,
    MAX_ENTRIES,
    ProfileStore,
    parse_profile,
    profile_dict,
    read_profile_file,
    resolve_entries,
)
from darksort.relations import RelatedMod, related_mods
from darksort.rule_targets import target_labels
from darksort.save_bindings import SaveBindings
from darksort.scanning.discovery import Discovery, discover, steam_locations
from darksort.scanning.metadata import parse_mod
from darksort.search import ModLocation, SearchRecord, search_text
from darksort.settings import DEFAULT_THEME, MARK_COLORS, Settings
from darksort.sharing import (
    export_filename,
    group_from_template,
    read_group_template,
    read_share_package,
    write_group_template,
    write_share_package,
)
from darksort.sorting import browse_key, move_entries, preview_order
from darksort.tags import TagStore
from darksort.ui.ai_dialogs import AISettingsDialog, AssistantPanel, ModTextDialog
from darksort.ui.appearance import AppearanceDialog, show_artwork
from darksort.ui.campaign_dialog import CampaignDialog
from darksort.ui.conflict_dialog import ConflictWorker
from darksort.ui.feature_guide import FeatureGuideDialog
from darksort.ui.file_replacements import FilePackDialog, run_operation
from darksort.ui.group_panel import GroupMemberRow, ModGroupDialog
from darksort.ui.local_mods import NewModDialog, WorkshopCopyDialog
from darksort.ui.mod_list import ROLE
from darksort.ui.mod_menu import MenuCommand, ModMenuDialog, popup_mod_menu
from darksort.ui.path_dialog import PathDialog
from darksort.ui.plan_dialogs import (
    HistoryDialog,
    RuleDialog,
    SortPreviewDialog,
    confirmation_dialog,
    transfer_matches,
)
from darksort.ui.profile_dialogs import ProfileImportDialog, ProfileManagerDialog
from darksort.ui.save_dialog import SaveDialog
from darksort.ui.search import GlobalSearchDialog
from darksort.ui.theme import THEMES, apply_theme, icon, problem_color
from darksort.ui.workbench import build_workbench, button, label
from darksort.ui.worker import ScanWorker
from darksort.ui.workshop import open_workshop_page, workshop_id, workshop_page
from darksort.workshop import workshop_link_id


@dataclass(frozen=True)
class RelationOrigin:
    target: ModLocation
    scroll_values: tuple[int, ...]
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
        self.save_bindings = SaveBindings(data_dir)
        self.file_packs = FilePackStore(data_dir)
        packs, errors = self.file_packs.list()
        self.file_tool_keys = {pack.origin.key for pack in packs if pack.origin}
        self.startup_errors.extend(errors)
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
            profiles = [self.store.new(DEFAULT_PROFILE_NAME)]
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
        self._rescan_after_worker = False
        self._launched_game = None
        self._close_pending = False
        self._detail_mod: Mod | None = None
        self._detail_target: ModLocation | None = None
        self._note_reference = ""
        self._pane_widths = [246, 283, 650, 282]
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
        self.setMinimumHeight(515)
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
        # An undo command may predate a saved rename.
        self.profile = replace(profile, name=self.profile.name)
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
        self.mods_by_key = {mod.key: mod for mod in self.mods}
        self.plan_positions = {row.entry.id: i for i, row in enumerate(self.resolved)}
        self.group_marks = defaultdict(list)
        for group in self.profile.mod_groups:
            for member in group.members:
                self.group_marks[member.reference].append((group.title, group.color))
        self.plan_units = entry_units(self.profile.entries, self.profile.locks)
        self.active_locks = lock_starts(self.profile.entries, self.profile.locks)
        self.lock_by_entry = {member: lock for lock in self.profile.locks for member in lock.members}
        self.profile_list.units = self.plan_units if self.active_locks else []
        self.refresh_plan_problems()
        used = {entry.mod.key for entry in self.resolved if entry.mod}
        order = self.library_search.order
        if order == "category":
            mods = sorted(self.mods, key=lambda mod: (
                self.classifications[mod.key].label, self.display_name(mod.reference, mod.name).casefold(),
            ))
        else:
            mods = sorted(self.mods, key=lambda m: browse_key(m, order, self.display_name(m.reference, m.name)),
                          reverse=order == "name_desc")
        available = [mod for mod in mods if mod.key not in used and mod.key not in self.file_tool_keys]
        self.available_keys = {mod.key for mod in available}
        self.group_rows = self.mod_group_rows()
        self.group_entries = {
            row.target.identity: (ResolvedEntry(row.entry, None, row.status), group.title)
            for group, members in self.group_rows for row in members if row.target.side == "group"
        }
        self.search_records = {
            "library": [self.search_record(ModLocation("library", mod.key), mod, None, "可用") for mod in available],
            "profile": [
                self.search_record(ModLocation("profile", row.entry.id), row.mod, row, f"计划 #{i + 1}")
                for i, row in enumerate(self.resolved)
            ],
            "group": [
                self.search_record(ModLocation("group", identity), None, row, f"分组 · {title}")
                for identity, (row, title) in self.group_entries.items()
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
        if self._temporary_target and self._temporary_target in self.matching_targets.get(self._temporary_target.side, ()):
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
        self.utility.set_groups(self.profile.id, self.group_rows, self.groups_by_index(), self.active_locks)
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
        identifier = self.workshop_identifier(mod, entry)
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
        if identifier:
            caption += f" · {identifier}"
        return SearchRecord.create(target, name, caption, (name, original, entry.name if entry else original),
                                   identifier, metadata, details)

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
        dialog = GlobalSearchDialog(records["profile"] + records["library"] + records["group"], self)
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
        self.set_detail_note("")
        self._detail_mod = None
        self._detail_target = None
        self._relation_anchor = None
        self._relation_origin = None
        self._detail_workshop_id = None
        self.detail_title.setText("选择一个 Mod")
        self.detail_title.setToolTip("")
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
        if target.side == "group":
            return target.identity in self.group_entries
        return target.identity in self.plan_positions

    def row_value(self, target):
        if target.side == "library":
            return target.identity
        return self.plan_positions[target.identity]

    def location_details(self, target):
        if target.side == "group":
            return None, self.group_entries[target.identity][0]
        value = self.row_value(target)
        if target.side == "library":
            return self.mods_by_key[value], None
        row = self.resolved[value]
        return row.mod, row

    def show_detail(self, target):
        mod, resolved = self.location_details(target)
        self._detail_mod = mod
        self._detail_target = target
        self.library_list.detail_mark = target.identity if target.side == "library" else None
        self.profile_list.detail_mark = self.row_value(target) if target.side == "profile" else None
        identifier = self.workshop_identifier(mod, resolved.entry if resolved else None)
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
        self.detail_title.setToolTip("双击编辑别名、简介和备注" if mod else "")
        self.set_detail_note(reference)
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
                "右键组员并选择「手动匹配本机 Mod」。"
                if target.side == "group" else "选中计划条目，再用「匹配」选择对应安装。"
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
                self.resolved, self.group_entries, self.classifications,
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
        if target.side == "group":
            self.show_utility_page(self.utility.groups_page)
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
        return self.library_list, self.profile_list, self.utility.group_shelf.tree

    def point_to_location(self, target, *, focus=True, select=False):
        if target.side == "group":
            self.utility.group_shelf.reveal_member(*target.identity.split(":", 1), focus=focus)
            return
        widget = self.library_list if target.side == "library" else self.profile_list
        for index in range(widget.count()):
            item = widget.item(index)
            data = item.data(ROLE)
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

    def preview_cover(self):
        if self._detail_mod and self._detail_mod.preview_path:
            show_artwork(self, self._detail_mod.preview_path, self._detail_mod.name)

    def open_mod_folder(self, *, mod=None):
        if (target := mod or self._detail_mod) and not QDesktopServices.openUrl(QUrl.fromLocalFile(target.path)):
            self.statusBar().showMessage(f"无法打开目录 · {target.path}", 5000)

    def workshop_identifier(self, mod: Mod | None, entry: ProfileEntry | None = None) -> str | None:
        if mod and (native := workshop_id(mod.workshop_id)):
            return native
        subject = mod or entry
        if subject and subject.source == Source.LOCAL:
            if linked := workshop_id(self.settings.mod_workshop_links.get(subject.reference)):
                return linked
        return workshop_id(entry.workshop_id if entry else None)

    def associate_workshop(self, mod: Mod):
        reference = mod.reference
        links = self.settings.mod_workshop_links.copy()
        text, accepted = QInputDialog.getText(
            self, "关联创意工坊", "粘贴创意工坊链接或输入数字 ID。清空可移除关联。",
            text=workshop_page(links.get(reference)),
        )
        if not accepted:
            return
        text = text.strip()
        if text:
            number = workshop_link_id(text) or workshop_id(search_text(text))
            if not number:
                self.error("工坊地址无效", "请输入有效的工坊链接或数字 ID。")
                return
            links[reference] = number
        else:
            links.pop(reference, None)
        try:
            updated = replace(self.settings, mod_workshop_links=links)
            updated.save(self.data_dir)
        except OSError as exc:
            self.error("无法保存工坊关联", str(exc))
            return
        self.settings = updated
        self.render_lists()

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
        sizes = self.panes.sizes()
        index = self.panes.indexOf(pane)
        if sizes[index] and sum(size > 0 for size in sizes) > 1:
            self._pane_widths[index] = sizes[index]
            sizes[index] = 0
        elif not sizes[index]:
            sizes[index] = self._pane_widths[index]
        self.panes.setSizes(sizes)
        self.remember_pane_sizes()

    def remember_pane_sizes(self, *_):
        sizes = self.panes.sizes()
        if any(sizes):
            self.settings.pane_sizes = sizes
            self._pane_widths = [size or old for size, old in zip(sizes, self._pane_widths, strict=True)]

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
        self.save_detail_note()
        previous_note = self.settings.mod_notes.get(reference, "")
        original = (self.settings.mod_aliases.get(reference, ""), self.settings.mod_translations.get(reference, ""))
        dialog = ModTextDialog(self, mod, *original, previous_note)
        if generate:
            QTimer.singleShot(0, dialog, dialog.generate)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            dialog.deleteLater()
            return
        changes = {
            field: value for field, value, before in zip(MOD_TEXT_FIELDS, dialog.text_values(), original, strict=True)
            if value != before or dialog.clear_requested
        }
        note = dialog.note.text().strip()
        dialog.deleteLater()
        if not changes and note == previous_note:
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
            self.save_mod_display(aliases, translations, notes=self.notes_with(reference, note))
        except (OSError, ValueError) as exc:
            self.error("无法保存 Mod 文本", str(exc))
            return
        self.mod_text_batch.keep_manual_fields(reference, changes)
        self.render_lists()

    def save_mod_display(self, aliases, translations, *, notes=None):
        updated = replace(self.settings, mod_aliases=aliases, mod_translations=translations)
        if notes is not None:
            updated.mod_notes = notes
        updated.save(self.data_dir)
        self.settings = updated

    def notes_with(self, reference, note):
        notes = self.settings.mod_notes.copy()
        if note:
            notes[reference] = note
        else:
            notes.pop(reference, None)
        return notes

    def save_detail_note(self):
        reference = self._note_reference
        note = self.detail_note.text().strip()
        if reference and note != self.settings.mod_notes.get(reference, ""):
            updated = replace(self.settings, mod_notes=self.notes_with(reference, note))
            try:
                updated.save(self.data_dir)
            except (OSError, ValueError) as exc:
                self.error("无法保存备注", str(exc))
                return
            self.settings = updated
        self.detail_note.setModified(False)

    def set_detail_note(self, reference):
        if self._note_reference != reference:
            self.save_detail_note()
            self._note_reference = reference
        elif self.detail_note.isModified():
            return
        self.detail_note.setEnabled(bool(reference))
        self.detail_note.setText(self.settings.mod_notes.get(reference, ""))

    def mod_group_rows(self):
        groups = self.profile.mod_groups
        if not groups:
            return []
        by_reference = defaultdict(list)
        by_install = {}
        for row in self.resolved:
            by_reference[row.entry.reference].append(row)
            if row.mod:
                by_install[row.mod.key] = row
        entries = list(dict.fromkeys(member for group in groups for member in group.members))
        installed = {row.entry: row for row in resolve_entries(entries, self.mods)}
        output = []
        for group in groups:
            members = []
            for member in group.members:
                reference = member.reference
                resolved = installed[member]
                mod = resolved.mod
                candidates = by_reference[reference]
                active = by_install.get(mod.key) if mod else None
                if active is None and len(candidates) == 1 and not candidates[0].mod:
                    active = candidates[0]
                if active:
                    mod = active.mod
                    target = ModLocation("profile", active.entry.id)
                    status = f"已加入计划 #{self.plan_positions[active.entry.id] + 1}"
                    if not mod:
                        status += " · 待匹配"
                elif mod and mod.key in self.available_keys:
                    target, status = ModLocation("library", mod.key), "可用 · 尚未加入计划"
                else:
                    target = ModLocation("group", f"{group.id}:{member.id}")
                    status = resolved.problem or "已转入游戏文件替换"
                members.append(GroupMemberRow(
                    member, self.display_name(reference, mod.name if mod else member.name), target, status,
                    self.mod_tags(mod) if mod else (),
                ))
            output.append((group, tuple(members)))
        return output

    def find_mod_group(self, identity):
        return next((group for group in self.profile.mod_groups if group.id == identity), None)

    def move_group_member(self, identity, member_id, target_id, below):
        group = self.find_mod_group(identity)
        if not group or member_id == target_id:
            return
        positions = {member.id: index for index, member in enumerate(group.members)}
        if member_id not in positions or target_id not in positions:
            return
        ordered = move_entries(list(group.members), [positions[member_id]], positions[target_id] + int(below))
        self.save_mod_group(replace(group, members=tuple(ordered)), action="调整组员顺序")

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

    def relink_group_member(self, identity, member_id, *, mod=None):
        group = self.find_mod_group(identity)
        member = next((entry for entry in group.members if entry.id == member_id), None) if group else None
        if not member or not (mod := mod or self.choose_installed_mod(member)):
            return
        replacement = replace(ProfileEntry.from_mod(mod, pinned=True), id=member.id)
        if any(entry.reference == replacement.reference for entry in group.members if entry.id != member.id):
            self.error("无法匹配组员", "所选 Mod 已经在这个分组内。请选择对应的另一项。")
            return
        updated = replace(group, members=tuple(replacement if entry.id == member.id else entry for entry in group.members))
        still_used = any(entry.reference == member.reference for entry in self.profile.entries) or any(
            entry.reference == member.reference
            for other in self.profile.mod_groups for entry in other.members
            if (other.id, entry.id) != (identity, member_id)
        )
        rules = self.profile.rules if still_used else self.retarget_rules(
            self.profile.rules, member.reference, replacement.reference,
        )
        self.save_mod_group(updated, action="手动匹配组员", rules=rules)

    def save_mod_group(self, group, *, action=None, rules=None):
        groups = [group if existing.id == group.id else existing for existing in self.profile.mod_groups]
        if group not in groups:
            groups.append(group)
        updated = replace(self.profile, mod_groups=groups, rules=self.profile.rules if rules is None else rules)
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

    def fill_group_menu(self, menu, members, *, location=None):
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
        if location is None:
            return
        memberships = [
            (group, row.entry.id) for group, rows in self.group_rows for row in rows
            if row.target == location
        ]
        if memberships:
            menu.addSeparator()
            locate = menu.addMenu("在分组中定位")
            remove = menu.addMenu("从分组移出")
            for group, member_id in memberships:
                locate.addAction(
                    group.title,
                    lambda _checked=False, identity=group.id, member=member_id:
                    self.reveal_group_member(identity, member),
                )
                remove.addAction(
                    group.title,
                    lambda _checked=False, identity=group.id, member=member_id:
                    self.remove_group_member(identity, member),
                )

    def reveal_group_member(self, group_id, member_id):
        self.show_utility_page(self.utility.groups_page)
        self.utility.group_shelf.reveal_member(group_id, member_id, focus=True)

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

    def remove_group_member(self, identity, member_id):
        group = self.find_mod_group(identity)
        if group:
            members = tuple(member for member in group.members if member.id != member_id)
            self.save_mod_group(replace(group, members=members, locked=group.locked and len(members) >= 2),
                                action="移出分组")

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
                members = resolve_entries(list(group.members), self.mods)
                if any(row.mod and row.mod.key in self.file_tool_keys for row in members):
                    raise ValueError("分组包含已转入「游戏文件替换」的 Mod。请先在工具中将它移回普通列表。")
                updated, skipped = enable_group(self.profile, group, members, boundary)
                added = self.apply_activation("整组加入计划", updated)
                message = f"「{group.title}」已按分组顺序加入计划 · {len(group.members) - skipped} 项。"
                if skipped:
                    message += f" {skipped} 项未匹配本机安装，已跳过。"
                if added:
                    message += self.requirement_notice(added)
            elif side == "library":
                members = members_in_plan(group, self.profile.entries)
                check_complete_locks(self.profile, members.values())
                if not self.remove_indices([self.plan_positions[entry.id] for entry in members.values()],
                                           action="整组移出计划"):
                    return
                message = f"「{group.title}」已移出计划 · {len(members)} 项。"
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
        if "group" not in data:
            location = ModLocation(side, data["entry_id"] if plan else data["value"])
            selected = [self.item_location(side, item) for item in sorted(view.selectedItems(), key=view.row)]
            self.show_mod_context_menu(view, point, side, location, selected)
            return

        menu = QMenu(view)
        menu.setObjectName("modContextMenu")
        menu.aboutToHide.connect(menu.deleteLater)
        menu.addAction("编辑分组标题…", lambda: self.edit_group_at(data["value"]))
        group, end = self.groups_by_index()[data["value"]]
        members = self.unique_group_members(self.profile.entries[data["value"]:end])
        menu.addAction("存为可拖动分组…", lambda: self.edit_mod_group(members=members, name=group.title))
        menu.popup(point)

    def configure_context_menu(self, scope="library"):
        dialog = ModMenuDialog(self.settings, self, scope=scope)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            updated = replace(
                self.settings, mod_menu_mode=dialog.mode.currentData(), mod_menu_items=dialog.result_items(),
            )
            try:
                updated.save(self.data_dir)
            except (OSError, ValueError) as exc:
                self.error("无法保存右键菜单", str(exc))
            else:
                self.settings = updated
        dialog.deleteLater()

    def show_feature_guide(self):
        dialog = FeatureGuideDialog(self)
        dialog.exec()
        dialog.deleteLater()

    def show_mod_context_menu(self, view, point, scope, location, selected, *, extra=None):
        if not self.location_exists(location):
            return
        self.inspect_selection(location)
        commands = self.mod_menu_commands(location, selected)
        if extra:
            commands.update(extra)
        popup_mod_menu(view, point, scope, commands, self.settings, self.configure_context_menu)

    def mod_menu_commands(self, location, selected):
        clicked_mod, clicked_row = self.location_details(location)
        selected = tuple(dict.fromkeys((*selected, location))) if location in selected else (location,)
        selected = tuple(target for target in selected if target.side == location.side and self.location_exists(target))
        plan = location.side == "profile"
        indices = sorted(self.plan_positions[target.identity] for target in selected) if plan else []
        mods = tuple(dict.fromkeys(mod for target in selected if (mod := self.location_details(target)[0])))
        members = ([self.profile.entries[i] for i in expand_selection(self.plan_units, indices)] if plan
                   else [ProfileEntry.from_mod(mod, pinned=True) for mod in mods])
        reference = clicked_row.entry.reference if clicked_row else clicked_mod.reference
        commands = {
            "rule": MenuCommand(lambda: self.edit_rules(first=reference)),
            "requires": MenuCommand(lambda: self.edit_rules(first=reference, prerequisite=True)),
            "relations": MenuCommand(lambda: self.reveal_mod_relations(location)),
        }
        if members:
            commands["group"] = MenuCommand(
                submenu=lambda menu: self.fill_group_menu(menu, members, location=location),
            )
        if mods:
            if self.tags:
                commands["tags"] = MenuCommand(
                    lambda: self.change_tags(mods, append=len(mods) > 1),
                    title="批量添加标签…" if len(mods) > 1 else "",
                )
            commands["color"] = MenuCommand(submenu=lambda menu: self.fill_color_menu(menu, mods))
            commands["type"] = MenuCommand(lambda: self.change_classification(mods))
        if plan:
            commands.update(self.plan_menu_commands(location.identity, indices))
        elif mods:
            keys = [mod.key for mod in mods]
            commands["add"] = MenuCommand(lambda: self.add_keys(keys))
            commands["add_lock"] = MenuCommand(
                lambda: self.add_keys(keys, arrangement="lock"), enabled=len(keys) > 1, glyph="link",
            )
            commands["add_heading"] = MenuCommand(lambda: self.add_keys(keys, arrangement="heading"))
            if clicked_mod and (any(row.mod is None for row in self.resolved) or self.group_entries):
                commands["match_pending"] = MenuCommand(lambda: self.match_pending_to_mod(clicked_mod))

        identifier = self.workshop_identifier(clicked_mod, clicked_row.entry if clicked_row else None)
        if clicked_mod:
            commands["edit_text"] = MenuCommand(lambda: self.edit_mod_text(clicked_mod))
            commands["folder"] = MenuCommand(lambda: self.open_mod_folder(mod=clicked_mod))
            if clicked_mod.warnings:
                commands["scan_notice"] = MenuCommand(lambda: self.show_mod_scan_notice(clicked_mod))
            if clicked_mod.source == Source.LOCAL:
                if not workshop_id(clicked_mod.workshop_id):
                    commands["workshop_link"] = MenuCommand(lambda: self.associate_workshop(clicked_mod))
                commands["uninstall"] = MenuCommand(lambda: self.uninstall_local_mod(clicked_mod))
            elif clicked_mod.workshop_id:
                commands["unsubscribe"] = MenuCommand(lambda: self.open_workshop(identifier=clicked_mod.workshop_id))
            if clicked_mod.key not in self.file_tool_keys:
                commands["replace_files"] = MenuCommand(lambda: self.open_file_replacements(mod=clicked_mod))
            commands["exclude"] = MenuCommand(lambda: self.exclude_scan_mod(clicked_mod))
        if identifier:
            commands["workshop"] = MenuCommand(lambda: self.open_workshop(identifier=identifier))
            commands["workshop_browser"] = MenuCommand(lambda: self.open_workshop(identifier=identifier, browser=True))
            commands["workshop_copy"] = MenuCommand(lambda: self.copy_workshop_link(identifier=identifier))
        return commands

    def reveal_mod_relations(self, location):
        if self.location_exists(location):
            self.inspect_selection(location)
            self.follow_current_detail()

    def show_mod_scan_notice(self, mod):
        self.show_text("Mod 扫描提示", "\n".join((mod.name, mod.path, "", *mod.warnings)))

    def fill_color_menu(self, menu, mods):
        for title, color in MARK_COLORS:
            menu.addAction(icon("dot", color), title, lambda _checked=False, color=color: self.mark_mods(mods, color))
        menu.addAction("清除颜色", lambda: self.mark_mods(mods, ""))

    def plan_menu_commands(self, entry_id, indices):
        members = {self.profile.entries[i].id for i in indices}
        locked = bool(members.intersection(self.lock_by_entry))
        lockable = (
            len(indices) >= 2 and not locked
            and (self.plan_is_filtered() or not self.profile_list.reorder_blocked)
        )
        commands = {
            "heading": MenuCommand(lambda: self.edit_group_at(self.plan_positions[entry_id])),
            "lock": MenuCommand(lambda: self.lock_selected(quick=True, indices=indices), enabled=lockable, glyph="link"),
            "unlock": MenuCommand(lambda: self.unlock_selected(indices=indices), enabled=locked),
            "relink": MenuCommand(lambda: self.relink_plan_entry(entry_id)),
            "remove": MenuCommand(lambda: self.remove_indices(indices)),
        }
        if entry_id in self.problem_levels:
            commands["problem"] = MenuCommand(lambda: self.inspect_problem(entry_id))
        return commands

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

    def add_keys(self, keys, target=None, *, arrangement=""):
        entries = self.new_entries(keys)
        if not entries:
            return
        target = len(self.profile.entries) if target is None else snap_boundary(self.plan_units, target)
        current = self.profile.entries
        updated = replace(self.profile, entries=current[:target] + entries + current[target:])
        action = "加入 Mod"
        anchor = entries[0]
        if arrangement == "lock":
            if len(entries) < 2:
                self.statusBar().showMessage("选中至少两个可用 Mod 后可一起联锁。", 4500)
                return
            lock = ProfileLock(self.lock_title(anchor, len(entries)), tuple(entry.id for entry in entries))
            updated.locks = [*updated.locks, lock]
            action = "加入计划并联锁"
        elif arrangement == "heading":
            title, accepted = QInputDialog.getText(self, "加入计划并设标题", "计划分组标题", text=anchor.name[:120])
            if not accepted or not title.strip():
                return
            if len(title.strip()) > 120:
                self.error("分组名称过长", "请输入 120 字以内的组名。")
                return
            updated.groups = [*updated.groups, ProfileGroup(anchor.id, title.strip())]
            action = "加入计划并设标题"
        try:
            added = self.apply_activation(action, updated)
        except ValueError as exc:
            self.statusBar().showMessage(str(exc), 6500)
            return
        if arrangement == "lock":
            self.profile_list.flash_lock(anchor.id)
        if arrangement or added:
            self.statusBar().showMessage(f"已{action}。{self.requirement_notice(added) if added else ''}", 5500)

    def apply_activation(self, action, updated, *, roots=()):
        previous = {entry.reference for entry in self.profile.entries}
        added_refs = (entry.reference for entry in updated.entries if entry.reference not in previous)
        updated, added = enable_requirements(updated, (*roots, *added_refs), self.mods, self.file_tool_keys)
        if len(updated.entries) > MAX_ENTRIES:
            raise ValueError(f"计划最多 {MAX_ENTRIES} 项。")
        self.edit_profile(action, updated)
        return added

    @staticmethod
    def requirement_notice(added):
        names = "、".join(entry.name for entry in added[:5])
        more = f"等 {len(added)} 项" if len(added) > 5 else ""
        return f" 已同时启用前置「{names}」{more}。"

    def new_entries(self, keys):
        used = {entry.mod.key for entry in self.resolved if entry.mod}
        entries = []
        for key in dict.fromkeys(key for key in keys if isinstance(key, str)):
            if key in self.mods_by_key and key not in used and key not in self.file_tool_keys:
                entries.append(ProfileEntry.from_mod(self.mods_by_key[key]))
                used.add(key)
        return entries

    def selected_profile_indices(self):
        return sorted(item.data(ROLE)["value"] for item in self.profile_list.selectedItems())

    def remove_selected(self):
        self.remove_indices(self.selected_profile_indices())

    def remove_indices(self, indices, *, action=None):
        indices = expand_selection(self.plan_units, indices)
        if not indices:
            return False
        entries = self.profile.entries
        removed = {entries[i].id for i in indices}
        try:
            check_removal(self.profile, removed)
        except ValueError as exc:
            self.statusBar().showMessage(str(exc), 6500)
            return False
        locks = [lock for lock in self.profile.locks if not removed.isdisjoint(lock.members)]
        selected = set(indices)
        groups = []
        for index, (group, end) in self.groups_by_index().items():
            retained = next((i for i in range(index, end) if i not in selected), None)
            if retained is not None:
                groups.append(ProfileGroup(entries[retained].id, group.title))
        updated = replace(
            self.profile, groups=groups, entries=[entry for i, entry in enumerate(entries) if i not in selected],
            locks=[lock for lock in self.profile.locks if lock not in locks],
            mod_groups=groups_for_disabled_locks(self.profile, locks, entries),
        )
        self.edit_profile(action or "移出计划", updated)
        if locks:
            self.show_utility_page(self.utility.groups_page)
            self.statusBar().showMessage("联锁组已停用，可以从分组重新启用。", 4500)
        return True

    def lock_selected(self, *, quick=False, indices=None):
        indices = self.selected_profile_indices() if indices is None else indices
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
        title = self.lock_title(entries[start], len(indices))
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

    @staticmethod
    def lock_title(entry, count):
        return f"{entry.name[:102]} · {count} 项"

    def unlock_selected(self, *, indices=None):
        members = {
            self.profile.entries[index].id
            for index in (self.selected_profile_indices() if indices is None else indices)
        }
        remaining = [lock for lock in self.profile.locks if not members.intersection(lock.members)]
        if remaining == self.profile.locks:
            self.statusBar().showMessage("选中一个联锁内的条目即可解除整组联锁。", 4500)
            return
        unlocked = {identity for lock in self.profile.locks if lock not in remaining for identity in lock.members}
        groups = [
            replace(group, locked=False) if group.locked and
            all(member.id in unlocked for member in group.members) and
            len({member.reference for member in group.members}) == len(group.members) else group
            for group in self.profile.mod_groups
        ]
        self.edit_profile("解除联锁", replace(self.profile, locks=remaining, mod_groups=groups))
        self.statusBar().showMessage("已解除联锁。每个 Mod 可以单独移动。", 4500)

    def handle_drop(self, side, payload, boundary):
        if not isinstance(payload, dict):
            return
        if payload.get("side") == "group":
            if payload.get("profile_id") == self.profile.id and isinstance(payload.get("group_id"), str):
                self.apply_mod_group(payload["group_id"], side, boundary if side == "profile" else None)
            return
        if payload.get("side") == "group-member":
            if payload.get("profile_id") == self.profile.id:
                self.drop_group_member(side, payload.get("group_id"), payload.get("member_id"), boundary)
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
                self.move_plan_entries([i for i in values if isinstance(i, int)], boundary)

    def move_plan_entries(self, indices, boundary):
        indices = expand_selection(self.plan_units, indices)
        ordered = move_entries(self.profile.entries, indices, snap_boundary(self.plan_units, boundary))
        self.edit_profile("移动 Mod", replace(self.profile, entries=ordered))

    def drop_group_member(self, side, group_id, member_id, boundary):
        for group, rows in self.group_rows:
            if group.id != group_id:
                continue
            for row in rows:
                if row.entry.id != member_id:
                    continue
                target = row.target
                if side == "profile" and self.can_reorder_manually():
                    if target.side == "profile":
                        self.move_plan_entries([self.plan_positions[target.identity]], boundary)
                    elif target.side == "library":
                        if group.locked:
                            self.apply_mod_group(group.id, side, boundary)
                        else:
                            self.add_keys([target.identity], target=boundary)
                    else:
                        self.statusBar().showMessage("请先匹配组员，再把它拖入计划。", 5000)
                elif side == "library" and target.side == "profile":
                    self.remove_indices([self.plan_positions[target.identity]])
                return
            return

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
        if mod.key in self.file_tool_keys:
            self.statusBar().showMessage("此 Mod 已转入「游戏文件替换」。", 5500)
            return
        if any(row.mod and row.mod.key == mod.key and row.entry.id != previous.id for row in self.resolved):
            self.statusBar().showMessage("此安装已在计划中，请选择另一份。", 5500)
            return
        try:
            self.replace_plan_mod(previous.id, mod, "手动定位 Mod")
        except ValueError as exc:
            self.statusBar().showMessage(str(exc), 6500)

    def match_pending_to_mod(self, mod):
        if mod.key not in self.available_keys:
            return
        records = [record for record in self.search_records["profile"]
                   if self.resolved[self.plan_positions[record.target.identity]].mod is None]
        records += self.search_records["group"]
        if not records:
            return
        picker = GlobalSearchDialog(records, self, pick_mod=True)
        picker.setWindowTitle(f"用「{mod.name}」匹配待定位条目")
        accepted = picker.exec() == QDialog.DialogCode.Accepted
        target = picker.target
        picker.deleteLater()
        if not accepted or not target or not self.location_exists(target):
            return
        if target.side == "group":
            group_id, member_id = target.identity.split(":", 1)
            self.relink_group_member(group_id, member_id, mod=mod)
            return
        try:
            self.replace_plan_mod(target.identity, mod, "匹配待定位条目")
        except ValueError as exc:
            self.statusBar().showMessage(str(exc), 6500)
        else:
            self.navigate(target, focus=False)

    def replace_plan_mod(self, identity, mod, action):
        index = self.plan_positions[identity]
        original = self.profile.entries[index]
        updated = replace(ProfileEntry.from_mod(mod, pinned=True), id=identity)
        rules, groups = self.profile.rules, self.profile.mod_groups
        if sum(entry.reference == original.reference for entry in self.profile.entries) == 1:
            rules = self.retarget_rules(rules, original.reference, updated.reference)
            groups = []
            for group in self.profile.mod_groups:
                members = tuple(
                    replace(updated, id=member.id) if member.reference == original.reference else member
                    for member in group.members
                )
                if not group.locked:
                    members = self.unique_group_members(members)
                groups.append(replace(group, members=members))
        entries = self.profile.entries.copy()
        entries[index] = updated
        return self.apply_activation(action, replace(self.profile, entries=entries, rules=rules,
                                                     mod_groups=groups))

    @staticmethod
    def retarget_rules(rules, before, after):
        output = []
        for rule in rules:
            first = after if rule.first == before else rule.first
            second = after if rule.second == before else rule.second
            if first != second:
                output.append(replace(rule, first=first, second=second))
            elif rule.kind != "requires":
                output.append(replace(rule, enabled=False))
        return list(dict.fromkeys(output))

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
        for side in ("profile", "library", "group"):
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

    def edit_rules(self, *, first="", second="", prerequisite=False):
        targets = self.rule_search_targets()
        dialog = RuleDialog(
            self.profile.rules, targets, self, first=first, second=second, prerequisite=prerequisite,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            rules = dialog.result_rules()
            old = required_edges(self.profile.rules)
            changed = dict.fromkeys(rule.first for rule in rules if rule.kind == "requires" and rule.enabled
                                    and rule.second not in old.get(rule.first, ()))
            active = {entry.reference for entry in self.profile.entries}
            try:
                required_mods(rules, changed, {key: target.title for key, target in targets.items()})
                added = self.apply_activation(
                    "编辑排序规则", replace(self.profile, rules=rules),
                    roots=tuple(reference for reference in changed if reference in active),
                )
                if added:
                    self.statusBar().showMessage(self.requirement_notice(added), 5500)
            except ValueError as exc:
                self.statusBar().showMessage(str(exc), 7500)

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
        restored = replace(dialog.restored, name=self.profile.name)
        if profile_dict(restored) == profile_dict(self.profile):
            return
        try:
            self.history.capture(self.profile, "恢复快照前")
        except (OSError, ValueError) as exc:
            self.error("无法保存恢复前快照", str(exc))
            return
        self.collapsed_groups.clear()
        self.edit_profile("载入历史快照", restored)
        self.refresh_history_panel()
        self.statusBar().showMessage("已载入快照。保存方案后保留。", 4500)

    def show_utility_page(self, page):
        if not self.panes.sizes()[3]:
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
            rules=self.profile.rules, launch_preferences=self.settings.campaign_launch_options,
            steam_roots=self.settings.steam_paths,
        )
        started = dialog.process if dialog.exec() == QDialog.DialogCode.Accepted else None
        preference = dialog.launch_preference
        dialog.deleteLater()
        if started is not None:
            self._launched_game = started
            message = "已启动游戏 · 请在主菜单选择空档位或刚建的新档。"
            if preference:
                path, options = preference
                if self.settings.campaign_launch_options.get(path) != options:
                    updated = replace(self.settings, campaign_launch_options={
                        **self.settings.campaign_launch_options, path: options,
                    })
                    try:
                        updated.save(self.data_dir)
                    except (OSError, ValueError) as exc:
                        message += f" 启动项未能保存：{exc}"
                    else:
                        self.settings = updated
            self.statusBar().showMessage(message, 12000)
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

    def manage_profiles(self):
        dialog = ProfileManagerDialog(self)
        dialog.exec()
        dialog.deleteLater()

    def rename_profile(self, profile_id: str, name: str):
        renamed = self.store.rename(profile_id, name)
        self.populate_profiles()
        if self.profile.id == profile_id:
            self.profile = replace(self.profile, name=renamed.name)
            self._baseline["name"] = renamed.name
            self.render_lists()
        self.statusBar().showMessage(f"方案名称已保存为「{renamed.name}」。", 4000)

    def delete_profile(self, profile_id: str):
        deleting_current = self.profile.id == profile_id
        replacement = None
        if deleting_current:
            profiles, _ = self.store.list()
            replacement = next((profile for profile in profiles if profile.id != profile_id), None)
        archived = self.store.archive(profile_id)
        created = None
        try:
            if deleting_current and replacement is None:
                replacement = created = self.store.new(DEFAULT_PROFILE_NAME)
            self.save_bindings.remove_profile(profile_id)
        except (OSError, TypeError, ValueError):
            archived.rename(self.store.path(profile_id))
            if created:
                self.store.path(created.id).unlink()
            raise
        if deleting_current:
            self.activate_profile(replacement)
        else:
            self.populate_profiles()
        self.statusBar().showMessage("方案已删除，已保存版本留在数据目录的 deleted-profiles 中。", 5500)

    def import_profile(self, *, share=False):
        title = "导入方案分享包" if share else "导入 DarkSort 方案"
        file_filter = "DarkSort 分享包 (*.zip)" if share else "DarkSort 方案 (*.json)"
        path, _ = QFileDialog.getOpenFileName(self, title, "", file_filter)
        if not path:
            return
        try:
            source = Path(path)
            profile = read_share_package(source) if share else read_profile_file(source)
            existing, _ = self.store.list()
            current_refs = {entry.reference for entry in profile.entries}
            group_only = self.unique_group_members(
                entry for group in profile.mod_groups for entry in group.members if entry.reference not in current_refs
            )
            sections = [("计划", profile.entries), ("仅在分组中", group_only)]
            matches = transfer_matches(self.mods, sections, self.discovery is not None)
            summary = [f"{len(profile.entries)} 个计划 Mod"]
            if profile.mod_groups or profile.locks or profile.rules:
                summary.append(f"{len(profile.mod_groups)} 个分组 · {len(profile.locks)} 组联锁 · {len(profile.rules)} 条规则")
            lead = (profile.entries or group_only)[:5]
            if lead:
                heading = "计划开头" if profile.entries else "分组中的 Mod"
                lines = [
                    f"{index:02d}  {self.display_name(entry.reference, entry.name)}"
                    for index, entry in enumerate(lead, 1)
                ]
                summary.append("\n".join([heading, *lines]))
            summary.append(matches)
            if share and any(
                key.startswith(("tag:", "color:")) for rule in profile.rules for key in (rule.first, rule.second)
            ):
                summary.append("标签和标记颜色规则会按本机标记重新计算。")
            dialog = ProfileImportDialog(source, profile, [item.name for item in existing], "\n\n".join(summary), self)
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            name = dialog.name.text().strip()
            dialog.deleteLater()
            if accepted and self.maybe_save():
                self.activate_profile(self.store.import_profile(replace(profile, name=name)))
                self.statusBar().showMessage(f"已导入「{name}」。待匹配条目会保留原位置。", 5000)
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
        self.import_profile(share=True)

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

    def install_parents(self):
        games = self.game_locations() or discover(self.settings).games
        parents = []
        for game in games:
            try:
                parent = mod_parent(game)
            except (OSError, ValueError):
                continue
            if parent not in parents:
                parents.append(parent)
        return parents

    def local_scan_settings(self, parent):
        excluded = {Path(path).expanduser().resolve() for path in self.settings.excluded_paths if path}
        if parent in excluded or not excluded.isdisjoint(parent.parents):
            raise ValueError("此目录已排除扫描。请在「工具 → 扫描路径」恢复后再创建本地 Mod。")
        custom = self.settings.local_paths
        if custom and parent not in {Path(path).expanduser().resolve() for path in custom if path}:
            return replace(self.settings, local_paths=[*custom, str(parent)])
        return self.settings

    def create_empty_mod(self):
        manual = self.data_dir.resolve() / "manual-tools"
        dialog = NewModDialog(self.install_parents(), manual, self)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            parent = dialog.destination.currentData()
            manual_tool = parent == manual
            settings = self.settings if manual_tool else self.local_scan_settings(parent)
            folder = create_blank(parent, dialog.title.text(), dialog.description.toPlainText(), manual=manual_tool)
            if not manual_tool:
                self.record_local_mod(folder, settings)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
            self.statusBar().showMessage(f"已创建「{folder.name}」。", 5000)
        except (OSError, RuntimeError, ValueError) as exc:
            self.error("无法新建空模组", str(exc))
        finally:
            dialog.deleteLater()

    def convert_workshop_mod(self):
        parents = self.install_parents()
        if not parents:
            QMessageBox.information(self, "请选择游戏安装", "在「工具 → 扫描路径」设置游戏目录，再转换工坊 Mod。")
            return
        candidates = {mod.key: mod for mod in self.mods if mod.source == Source.WORKSHOP and mod.workshop_id}
        if not candidates:
            self.statusBar().showMessage("扫描后可以选择要复制的工坊 Mod。", 5000)
            return
        mod = self._detail_mod
        if not mod or mod.key not in candidates:
            records = [self.search_record(ModLocation("workshop", item.key), item, None, "工坊")
                       for item in candidates.values()]
            picker = GlobalSearchDialog(records, self, pick_mod=True)
            picker.setWindowTitle("选择要转为本地的工坊 Mod")
            if picker.exec() != QDialog.DialogCode.Accepted or not picker.target:
                picker.deleteLater()
                return
            mod = candidates[picker.target.identity]
            picker.deleteLater()
        identity = next((row.entry.id for row in self.resolved if row.mod == mod), None)
        dialog = WorkshopCopyDialog(mod, parents, bool(identity), self)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            parent = dialog.destination.currentData()
            settings = self.local_scan_settings(parent)
            folder = run_operation(self, "正在复制并校验工坊文件…", lambda: copy_workshop_to_local(mod, parent),
                                   title="工坊 Mod 转为本地")
            local = self.record_local_mod(folder, settings)
            notice = f"已将「{mod.name}」复制到本地。"
            if identity and dialog.replace_plan.isChecked():
                try:
                    added = self.replace_plan_mod(identity, local, "切换到本地副本")
                    notice += " 当前计划已切换到本地副本。"
                    if added:
                        notice += self.requirement_notice(added)
                except ValueError as exc:
                    notice += f" 当前计划需手动匹配。{exc}"
            self.statusBar().showMessage(notice, 7500)
        except (OSError, RuntimeError, ValueError) as exc:
            self.error("无法完成工坊转本地", str(exc))
        finally:
            dialog.deleteLater()

    def record_local_mod(self, folder, settings):
        if settings is not self.settings:
            settings.save(self.data_dir)
            self.settings = settings
        mod = parse_mod(folder, Source.LOCAL)
        self.mods = [*self.mods, mod]
        self.refresh_classifications()
        self.refresh_tag_options()
        self.render_lists()
        self.request_rescan()
        return mod

    def uninstall_local_mod(self, mod):
        roots = (self.discovery or discover(self.settings)).roots
        try:
            folder = local_installation(mod, roots)
            original = folder.stat()
            check_removal(self.profile, {row.entry.id for row in self.resolved if row.mod == mod})
        except (OSError, ValueError) as exc:
            self.error("无法卸载本地 Mod", str(exc))
            return
        profiles = [self.profile, *(profile for profile in self.store.list()[0] if profile.id != self.profile.id)]
        names = [
            profile.name for profile in profiles if any(
                entry.reference == mod.reference
                for entry in (*profile.entries, *(member for group in profile.mod_groups for member in group.members))
            )
        ]
        detail = f"将「{mod.name}」移到 Windows 回收站。\n\n{folder}"
        if names:
            detail += "\n\n以下方案引用了这个 Mod，条目将等待重新匹配。\n" + "\n".join(names)
        if not confirmation_dialog(self, "卸载本地 Mod", detail, "移到回收站"):
            return
        try:
            require_game_stopped()
            if original.st_ino != local_installation(mod, roots).stat().st_ino:
                raise ValueError("目录已变化，请重新扫描后再操作。")
            file = QFile(str(folder))
            if not file.moveToTrash():
                raise OSError(file.errorString())
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            self.error("无法移到回收站", str(exc))
            return
        self.mods = [item for item in self.mods if item.key != mod.key]
        self.refresh_classifications()
        self.refresh_tag_options()
        self.render_lists()
        self.request_rescan()
        self.statusBar().showMessage(f"已将「{mod.name}」移到回收站。", 5000)

    def open_file_replacements(self, *, mod=None):
        dialog = FilePackDialog(self, mod=mod)
        dialog.exec()
        dialog.deleteLater()

    def refresh_file_packs(self, updated=None):
        packs, _ = self.file_packs.list()
        self.file_tool_keys = {pack.origin.key for pack in packs if pack.origin}
        if updated is None or not self.edit_profile("转入游戏文件替换", updated):
            self.render_lists()

    def plan_for_file_import(self, origin: ModOrigin) -> tuple[Profile | None, str]:
        def matches(row):
            return row.mod.key == origin.key if row.mod else row.entry.reference == origin.reference

        removed = {row.entry.id for row in self.resolved if matches(row)}
        check_removal(self.profile, removed)
        profiles, _ = self.store.list()
        other_names = [
            profile.name for profile in profiles if profile.id != self.profile.id
            and any(matches(row) for row in resolve_entries(profile.entries, self.mods))
        ]
        notes = []
        updated = None
        if removed:
            entries = self.profile.entries
            kept_groups = []
            for start, (group, end) in self.groups_by_index().items():
                anchor = next((entry.id for entry in entries[start:end] if entry.id not in removed), None)
                if anchor:
                    kept_groups.append(replace(group, anchor=anchor))
            kept_locks = [lock for lock in self.profile.locks if removed.isdisjoint(lock.members)]
            updated = replace(
                self.profile, entries=[entry for entry in entries if entry.id not in removed],
                groups=kept_groups, locks=kept_locks,
            )
            notes.append(f"导入后将从当前方案草稿移出 {len(removed)} 项。可撤销这次方案编辑。")
            if len(kept_locks) != len(self.profile.locks):
                notes.append("涉及该 Mod 的联锁会解除，其余条目保留原顺序。")
        if other_names:
            notes.append("另有方案引用此 Mod。启用资源包后请避免重复加载。\n" + "、".join(other_names))
        return updated, "\n".join(notes)

    def exclude_scan_mod(self, mod: Mod):
        if any(row.mod and row.mod.key == mod.key for row in self.resolved) and not confirmation_dialog(
            self, "从扫描中排除",
            f"「{mod.name}」在当前计划中。排除后，它会显示为待定位。\n"
            "可在「工具 → 扫描路径」取消排除。",
            "从扫描中排除",
        ):
            return
        try:
            path = Path(mod.path).resolve()
            excluded = self.settings.excluded_paths.copy()
            if path not in map(Path, excluded):
                excluded.append(str(path))
            settings = replace(self.settings, excluded_paths=excluded)
            settings.save(self.data_dir)
        except (OSError, RuntimeError, ValueError) as exc:
            self.error("无法排除扫描目录", str(exc))
            return
        self.settings = settings
        self.request_rescan()

    def request_rescan(self):
        if self.worker is not None:
            self._rescan_after_worker = True
            self.worker.requestInterruption()
        else:
            self.start_scan()

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
        elif self._rescan_after_worker:
            self._rescan_after_worker = False
            QTimer.singleShot(0, self.start_scan)

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
        self.save_detail_note()
        self.remember_pane_sizes()
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
