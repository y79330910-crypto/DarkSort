from collections import deque
from dataclasses import replace

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QProgressBar,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
)

from darksort.ai_client import AIError
from darksort.mod_text_batch import (
    MOD_TEXT_BATCH_PROMPT,
    collect_mod_text_tasks,
    mod_text_payload,
    parse_mod_text_batch,
    split_mod_text_tasks,
)
from darksort.ui.ai_dialogs import AIRequest, plain_label


class ModTextBatchPanel(QFrame):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setObjectName("paneFooter")
        self.running = False
        self.stopping = False
        self.provider = None
        self.queue = deque()
        self.current = ()
        self.manual_fields = {}
        self.saved = set()
        self.retried = set()
        self.total = self.processed = self.incomplete = self.consecutive_empty = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(9, 4, 7, 4)
        layout.setSpacing(3)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.title = plain_label("AI 批量翻译", "section")
        row.addWidget(self.title)
        self.progress = QProgressBar()
        self.progress.setAccessibleName("AI 批量翻译进度")
        self.progress.setTextVisible(False)
        self.progress.setMinimumWidth(48)
        row.addWidget(self.progress, 1)
        self.count = plain_label()
        row.addWidget(self.count)
        self.stop_button = QPushButton("停止", objectName="quiet")
        self.stop_button.setToolTip("保存当前请求的结果后停止")
        self.stop_button.clicked.connect(self.request_stop)
        row.addWidget(self.stop_button)
        self.hide_button = QPushButton("隐藏", objectName="quiet")
        self.hide_button.setToolTip("隐藏进度，继续后台处理")
        self.hide_button.clicked.connect(self.hide)
        row.addWidget(self.hide_button)
        layout.addLayout(row)
        self.details = plain_label()
        layout.addWidget(self.details)
        self.job = AIRequest(self)
        self.job.completed.connect(self.receive)
        self.job.failed.connect(self.fail)
        self.job.elapsed_changed.connect(self.update_wait)
        self.next_batch = QTimer(self, singleShot=True)
        self.next_batch.timeout.connect(self.advance)
        self.tray = QSystemTrayIcon(owner.windowIcon(), self)
        self.notification_timer = QTimer(self, singleShot=True)
        self.notification_timer.timeout.connect(self.tray.hide)
        self.hide()

    def start(self):
        if self.running:
            self.show()
            return
        if not self.owner.mods:
            self.owner.statusBar().showMessage("扫描到 Mod 后即可批量翻译。", 5500)
            return
        settings = self.owner.settings
        tasks = collect_mod_text_tasks(self.owner.mods, settings.mod_aliases, settings.mod_translations)
        if not tasks:
            self.owner.statusBar().showMessage("已扫描的 Mod 都有别名和可生成的译文。", 5500)
            return
        if not self.owner.ai_ready():
            return
        self.provider = self.owner.ai_provider
        self.queue = deque(split_mod_text_tasks(tasks))
        self.current = ()
        self.manual_fields.clear()
        self.saved.clear()
        self.retried.clear()
        self.total, self.processed, self.incomplete, self.consecutive_empty = len(tasks), 0, 0, 0
        self.running, self.stopping = True, False
        self.owner.batch_text_progress_action.setEnabled(True)
        self.title.setText("AI 批量翻译")
        self.details.hide()
        self.stop_button.setEnabled(True)
        self.stop_button.show()
        self.hide_button.setText("隐藏")
        self.hide_button.setToolTip("隐藏进度，继续后台处理")
        self.progress.setRange(0, self.total)
        self.update_count()
        self.show()
        self.advance()

    def keep_manual_fields(self, reference, fields):
        if self.running:
            self.manual_fields.setdefault(reference, set()).update(fields)

    def remaining_fields(self, task):
        settings = self.owner.settings
        maps = {"alias": settings.mod_aliases, "translation": settings.mod_translations}
        manual = self.manual_fields.get(task.reference, ())
        return tuple(field for field in task.fields if field not in manual and not maps[field].get(task.reference))

    def update_count(self, wait=0):
        self.progress.setValue(self.processed)
        text = f"{self.processed}/{self.total} · 已保存 {len(self.saved)} 项"
        if wait >= 2:
            text += f" · 等待 {wait} 秒"
        self.count.setText(text)

    def update_wait(self, seconds):
        if not self.stopping:
            self.update_count(seconds)

    def advance(self):
        if not self.running:
            return
        if self.stopping:
            self.finish("已停止")
            return
        if self.owner.ai_provider != self.provider:
            self.finish("已暂停", "AI 接入设置已更新，再次点击批量生成即可继续。")
            return
        while self.queue:
            batch = self.queue.popleft()
            ready = []
            for task in batch:
                if fields := self.remaining_fields(task):
                    ready.append(replace(task, fields=fields))
            self.processed += len(batch) - len(ready)
            if ready:
                self.current = tuple(ready)
                self.title.setText("AI 批量翻译")
                self.update_count()
                self.job.start(self.provider, MOD_TEXT_BATCH_PROMPT, mod_text_payload(self.current, self.provider.api_key))
                return
        if self.incomplete:
            self.finish("已结束", "模型未返回部分条目的完整内容，再次运行会补齐剩余字段。")
        else:
            self.finish("完成")

    def receive(self, text):
        issue = "模型未返回完整的别名或译文。"
        try:
            rows = parse_mod_text_batch(text, self.current, self.provider.api_key)
        except AIError as exc:
            rows, issue = {}, str(exc)
        self.apply_reply(rows, issue)

    def apply_reply(self, rows, issue):
        batch, self.current = self.current, ()
        settings = self.owner.settings
        aliases, translations = settings.mod_aliases.copy(), settings.mod_translations.copy()
        maps = {"alias": aliases, "translation": translations}
        changed = set()
        for task in batch:
            fields = rows.get(task.number, {})
            for field in self.remaining_fields(task):
                if value := fields.get(field):
                    maps[field][task.reference] = value
                    changed.add(task.reference)
        if changed:
            try:
                self.owner.save_mod_display(aliases, translations)
            except (OSError, ValueError) as exc:
                self.finish("已暂停", f"批量结果保存失败。{exc}")
                return
            self.saved.update(changed)

        retry = []
        for task in batch:
            fields = self.remaining_fields(task)
            if not fields:
                self.processed += 1
                self.consecutive_empty = 0
            elif self.stopping:
                continue
            elif len(batch) > 1:
                retry.append((replace(task, fields=fields),))
            else:
                self.processed += 1
                self.incomplete += 1
                self.consecutive_empty = 0 if changed else self.consecutive_empty + 1
        if self.consecutive_empty >= 3 and self.queue:
            self.finish("已暂停", f"连续三项未取得可用结果。{issue}")
            return
        self.queue.extendleft(reversed(retry))
        self.update_count()
        self.next_batch.start(0)

    def request_stop(self):
        if not self.running:
            return
        self.stopping = True
        self.stop_button.setEnabled(False)
        if self.job.results is None:
            self.finish("已停止")
        else:
            self.title.setText("结束当前请求后停止")

    def fail(self, message):
        error = self.job.error
        if self.stopping:
            self.finish("已停止", message)
        elif error and error.truncated:
            self.apply_reply({}, message)
        elif error and error.retry_after is not None:
            ids = tuple(task.number for task in self.current)
            if ids in self.retried:
                self.finish("已暂停", message)
                return
            self.retried.add(ids)
            self.queue.appendleft(self.current)
            self.current = ()
            self.title.setText("等待接口重试")
            self.next_batch.start(round(error.retry_after * 1000))
        else:
            self.finish("已暂停", message)

    def finish(self, outcome, reason=""):
        self.running, self.stopping = False, False
        self.next_batch.stop()
        self.job.stop()
        self.provider = None
        self.queue.clear()
        self.current = ()
        self.stop_button.hide()
        self.hide_button.setText("关闭")
        self.hide_button.setToolTip("关闭进度提示")
        self.title.setText(f"批量翻译{outcome}")
        self.update_count()
        remaining = self.total - self.processed + self.incomplete
        detail = reason or ("再次点击批量生成可以继续。" if remaining else "")
        if remaining:
            detail = f"{remaining} 项待补齐。{detail}"
        self.details.setText(detail)
        self.details.setVisible(bool(detail))
        if self.saved:
            self.owner.render_lists()
        message = f"批量翻译{outcome} · 保存了 {len(self.saved)} 个 Mod。{detail}"
        self.owner.statusBar().showMessage(message, 12000)
        if not self.owner.isActiveWindow():
            QApplication.alert(self.owner, 7000)
            if self.tray.isSystemTrayAvailable() and self.tray.supportsMessages():
                self.tray.show()
                self.tray.showMessage("DarkSort · AI 批量翻译", message, QSystemTrayIcon.MessageIcon.Information, 9000)
                self.notification_timer.start(12000)

    def shutdown(self):
        self.running = False
        self.provider = None
        self.next_batch.stop()
        self.notification_timer.stop()
        self.tray.hide()
        self.job.stop()
