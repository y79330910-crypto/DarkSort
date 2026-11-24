from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from html import escape
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Thread
from time import monotonic

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from darksort.ai_client import (
    AIError,
    AIProvider,
    AISettingsStore,
    chat_endpoint,
    request_chat,
    request_text,
)
from darksort.ai_tasks import (
    CHAT_PROMPT,
    LOG_PROMPT,
    MAX_ALIAS,
    MAX_MOD_TRANSLATION,
    MOD_TEXT_PROMPT,
    SORT_PROMPT,
    TEST_PROMPT,
    load_log,
    log_excerpt,
    parse_ai_order,
    parse_mod_text,
    redact,
    short_field,
)
from darksort.mod_text_batch import MOD_TEXT_BATCH_PROMPT
from darksort.plan_query import PlanLookup, in_reply_order, lookup_plan
from darksort.ui.plan_dialogs import text_view
from darksort.ui.theme import current_theme

MAX_CHAT_EVIDENCE_BYTES = 48_000


def invoke_ai(results, provider, prompt, payload, purpose):
    try:
        if isinstance(payload, str):
            text = request_text(provider, prompt, payload, purpose=purpose)
        else:
            text = request_chat(provider, prompt, payload, purpose=purpose)
        results.put((True, text))
    except AIError as exc:
        results.put((False, AIError(str(exc), retry_after=exc.retry_after, truncated=exc.truncated)))
    except Exception:
        results.put((False, AIError("处理 AI 请求时发生错误，请检查服务配置后重试。")))


class AIRequest(QObject):
    completed = Signal(str)
    failed = Signal(str)
    busy_changed = Signal(bool)
    elapsed_changed = Signal(int)

    def __init__(self, parent):
        super().__init__(parent)
        self.results = None
        self.error = None
        self.timer = QTimer(self)
        self.timer.setInterval(90)
        self.timer.timeout.connect(self.poll)
        if isinstance(parent, QDialog):
            parent.finished.connect(self.stop)

    def start(self, provider, prompt, payload, *, purpose="chat"):
        if self.results is not None:
            return
        self.error = None
        self.results = SimpleQueue()
        self.started_at = monotonic()
        self.last_elapsed = 0
        Thread(target=invoke_ai, args=(self.results, provider, prompt, payload, purpose), daemon=True).start()
        self.timer.start()
        self.busy_changed.emit(True)

    def stop(self):
        self.timer.stop()
        self.results = None

    def poll(self):
        if self.results is None:
            return
        try:
            success, message = self.results.get_nowait()
        except Empty:
            elapsed = int(monotonic() - self.started_at)
            if elapsed != self.last_elapsed:
                self.last_elapsed = elapsed
                self.elapsed_changed.emit(elapsed)
            return
        self.stop()
        self.busy_changed.emit(False)
        if success:
            self.completed.emit(message)
        else:
            self.error = message
            self.failed.emit(str(message))


def plain_label(text="", name="muted"):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setObjectName(name)
    label.setWordWrap(True)
    return label


def dialog_layout(dialog, title):
    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(15, 13, 15, 13)
    layout.setSpacing(9)
    layout.addWidget(plain_label(title, "section"))
    return layout


def action_buttons(dialog, layout):
    buttons = QDialogButtonBox()
    buttons.addButton("关闭", QDialogButtonBox.ButtonRole.RejectRole)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    return buttons


class AISettingsDialog(QDialog):
    def __init__(self, store: AISettingsStore, provider: AIProvider, warning="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("AI 接入设置")
        self.resize(780, 536)
        self.store = store
        self.original = provider
        self.provider = None
        layout = dialog_layout(self, "AI 接入设置")
        tabs = QTabWidget()
        setup = QWidget()
        form = QFormLayout(setup)
        form.setContentsMargins(12, 14, 12, 12)
        self.endpoint = QLineEdit(provider.endpoint.removesuffix("/chat/completions"))
        self.endpoint.setPlaceholderText("https://服务商域名/v1")
        self.model = QLineEdit(provider.model)
        self.model.setPlaceholderText("服务商提供的模型 ID")
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setMaxLength(8192)
        self.api_key.setPlaceholderText(
            "已配置 · 同一接口留空沿用" if provider.api_key else "本地免密服务可留空"
        )
        self.remember = QCheckBox("使用 Windows 加密记住密钥")
        self.remember.setChecked(provider.remember_key)
        self.remember.setEnabled(sys.platform == "win32")
        self.clear_key = QCheckBox("清除已有密钥")
        self.clear_key.setVisible(bool(provider.api_key))
        form.addRow("Base URL", self.endpoint)
        form.addRow("模型 ID", self.model)
        form.addRow("API Key", self.api_key)
        form.addRow("", self.remember)
        form.addRow("", self.clear_key)
        form.addRow(plain_label("使用兼容 Chat Completions 的服务。测试连接只发送一句测试消息。"))
        tabs.addTab(setup, "连接")
        prompts = QTabWidget()
        prompts.addTab(text_view(SORT_PROMPT), "排序提示词")
        prompts.addTab(text_view(LOG_PROMPT), "日志提示词")
        prompts.addTab(text_view(CHAT_PROMPT), "问答提示词")
        prompts.addTab(text_view(f"{MOD_TEXT_PROMPT}\n\n批量生成\n\n{MOD_TEXT_BATCH_PROMPT}"), "翻译与别名")
        tabs.addTab(prompts, "内置提示词")
        layout.addWidget(tabs, 1)
        self.status = plain_label(warning)
        layout.addWidget(self.status)
        buttons = action_buttons(self, layout)
        self.probe = buttons.addButton("测试连接", QDialogButtonBox.ButtonRole.ActionRole)
        self.save = buttons.addButton("保存设置", QDialogButtonBox.ButtonRole.ActionRole)
        self.save.setObjectName("primary")
        self.probe.clicked.connect(self.test_connection)
        self.save.clicked.connect(self.save_provider)
        self.job = AIRequest(self)
        self.job.completed.connect(self.show_test_success)
        self.job.failed.connect(self.status.setText)
        self.job.busy_changed.connect(self.set_busy)

    def selected_provider(self):
        endpoint = chat_endpoint(self.endpoint.text())
        key = self.api_key.text().strip()
        if not key and endpoint == self.original.endpoint and not self.clear_key.isChecked():
            key = self.original.api_key
        return AIProvider.from_input(
            endpoint, self.model.text(), key, remember_key=self.remember.isChecked()
        )

    def test_connection(self):
        try:
            provider = self.selected_provider()
        except AIError as exc:
            self.status.setText(str(exc))
            return
        self.status.setText(f"正在测试 {provider.destination} · {provider.model} …")
        self.job.start(provider, TEST_PROMPT, "请回复 OK。", purpose="probe")

    def set_busy(self, busy):
        self.probe.setEnabled(not busy)
        self.save.setEnabled(not busy)

    def show_test_success(self, _):
        self.status.setText("连接成功，模型已返回文本。")

    def save_provider(self):
        try:
            provider = self.selected_provider()
            self.store.save(provider)
        except (OSError, AIError) as exc:
            self.status.setText(str(exc))
            return
        self.provider = provider
        self.accept()


class ModTextDialog(QDialog):
    def __init__(self, owner, mod, alias, translation):
        super().__init__(owner)
        self.owner, self.mod = owner, mod
        self.clear_requested = False
        self.setWindowTitle("Mod 别名与中文简介")
        self.resize(730, 460)
        layout = dialog_layout(self, "Mod 别名与中文简介")
        layout.addWidget(plain_label(f"原名 · {mod.name}"))
        self.alias = QLineEdit(alias, maxLength=MAX_ALIAS)
        self.alias.setPlaceholderText("在列表和详情中显示的短名称")
        form = QFormLayout()
        form.addRow("显示别名", self.alias)
        layout.addLayout(form)
        panes = QHBoxLayout()
        original = QVBoxLayout()
        original.addWidget(plain_label("原文"))
        original.addWidget(text_view(mod.description or "作者没有提供简介。"), 1)
        panes.addLayout(original, 1)
        translated = QVBoxLayout()
        translated.addWidget(plain_label("中文简介"))
        self.translation = QPlainTextEdit(translation)
        self.translation.setPlaceholderText("输入译文，或点击 AI 生成填入建议")
        translated.addWidget(self.translation, 1)
        panes.addLayout(translated, 1)
        layout.addLayout(panes, 1)

        self.status = plain_label("点击 AI 生成会把原名和简介摘录交给当前 AI 接口。")
        layout.addWidget(self.status)
        buttons = QDialogButtonBox()
        self.generate_button = buttons.addButton("AI 生成", QDialogButtonBox.ButtonRole.ActionRole)
        self.generate_button.clicked.connect(self.generate)
        self.save = buttons.addButton("保存显示", QDialogButtonBox.ButtonRole.AcceptRole)
        self.save.setObjectName("primary")
        self.clear = buttons.addButton("清除自定义", QDialogButtonBox.ButtonRole.ResetRole)
        self.clear.clicked.connect(self.clear_text)
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.validate_text)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.job = AIRequest(self)
        self.job.completed.connect(self.show_suggestion)
        self.job.failed.connect(self.status.setText)
        self.job.busy_changed.connect(self.set_busy)
        self.job.elapsed_changed.connect(lambda seconds: self.status.setText(f"正在生成 · 已等待 {seconds} 秒"))

    def generate(self):
        if not self.owner.ai_ready():
            self.status.setText("请先在 AI 接入设置填写模型。")
            return
        provider = self.owner.ai_provider
        payload = json.dumps({
            "title": short_field(self.mod.name, provider.api_key, 260),
            "description": redact(self.mod.description[:7000], provider.api_key),
        }, ensure_ascii=False)
        self.status.setText(f"正在用 {provider.model} 生成…")
        self.job.start(provider, MOD_TEXT_PROMPT, payload)

    def show_suggestion(self, response):
        try:
            alias, translation = parse_mod_text(response, self.owner.ai_provider.api_key)
        except AIError as exc:
            self.status.setText(str(exc))
            return
        self.alias.setText(alias)
        self.translation.setPlainText(translation if self.mod.description.strip() else "")
        self.status.setText("已填入建议，可以编辑后保存。")

    def set_busy(self, busy):
        for control in (self.alias, self.translation, self.generate_button, self.save, self.clear):
            control.setEnabled(not busy)

    def clear_text(self):
        self.clear_requested = True
        self.alias.clear()
        self.translation.clear()

    def text_values(self):
        return self.alias.text().strip(), self.translation.toPlainText().strip()

    def validate_text(self):
        if len(self.text_values()[1]) > MAX_MOD_TRANSLATION:
            self.status.setText(f"中文简介最多 {MAX_MOD_TRANSLATION} 个字符。")
            return
        self.accept()


@dataclass(frozen=True)
class AssistantAttachment:
    kind: str
    label: str
    text: str


@dataclass(frozen=True)
class AssistantTurn:
    prompt: str
    messages: tuple[tuple[str, str], ...]
    display: str
    history_label: str
    sort_context: tuple | None = None
    sort_count: int = 0
    lookup: PlanLookup | None = None
    lookup_scope: str = ""
    evidence: str = ""
    plan_ids: tuple[str, ...] = ()


class AssistantPanel(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle("DarkSort · AI 助手")
        self.resize(700, 655)
        self.setMinimumSize(475, 475)
        self.mode = "chat"
        self.attachment = None
        self.transcript = []
        self.history = []
        self.pending = None
        self.proposal = None
        self.last_answer = ""
        self.last_lookup: PlanLookup | None = None
        self.lookup_profile = owner.profile.id
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        bar = QHBoxLayout()
        heading = QVBoxLayout()
        heading.setSpacing(2)
        heading.addWidget(plain_label("AI 助手", "section"))
        self.destination = plain_label()
        heading.addWidget(self.destination)
        bar.addLayout(heading, 1)
        self.new_chat = self.control("新对话", self.reset_chat)
        self.settings = self.control("接入设置", owner.configure_ai)
        bar.addWidget(self.new_chat)
        bar.addWidget(self.settings)
        layout.addLayout(bar)

        self.chat = QTextBrowser(objectName="assistantChat")
        self.chat.setOpenLinks(False)
        self.chat.setAccessibleName("AI 对话")
        self.chat.setPlaceholderText(
            "可以问「我加了哪些皮肤？」「哪些原版角色还缺皮肤？」或「查 #12 的技能文件」。"
        )
        layout.addWidget(self.chat, 1)
        response_actions = QHBoxLayout()
        self.status = plain_label()
        response_actions.addWidget(self.status, 1)
        self.copy = self.control("复制回复", lambda: QApplication.clipboard().setText(self.last_answer))
        self.review = self.control("核对排序…", self.review_order, primary=True)
        response_actions.addWidget(self.copy)
        response_actions.addWidget(self.review)
        layout.addLayout(response_actions)

        actions = QHBoxLayout()
        self.modes = QButtonGroup(self)
        self.mode_buttons = {}
        for mode, name in (("chat", "提问"), ("sort", "AI 排序"), ("log", "整理日志")):
            button = self.control(name, lambda _checked=False, mode=mode: self.set_mode(mode))
            button.setObjectName("assistantMode")
            button.setCheckable(True)
            self.modes.addButton(button)
            self.mode_buttons[mode] = button
            actions.addWidget(button)
        actions.addStretch()
        self.attach = self.control("附日志", None)
        menu = QMenu(self.attach)
        menu.addAction("选择日志文件…", self.browse_log)
        menu.addAction("粘贴日志…", self.paste_log)
        self.local_log = owner.data_dir / "darksort.log"
        menu.addAction("载入 DarkSort 日志", lambda: self.read_log(self.local_log))
        self.attach.setMenu(menu)
        actions.addWidget(self.attach)
        layout.addLayout(actions)

        references = QHBoxLayout()
        self.context = plain_label("", "assistantContext")
        references.addWidget(self.context, 1)
        self.detach = self.control("移除", self.detach_context)
        references.addWidget(self.detach)
        layout.addLayout(references)
        self.editor = QPlainTextEdit(objectName="assistantInput")
        self.editor.setAccessibleName("输入问题")
        self.editor.setFixedHeight(84)
        self.editor.textChanged.connect(self.update_controls)
        layout.addWidget(self.editor)
        bottom = QHBoxLayout()
        self.preview = self.control("发送预览…", self.preview_request)
        self.stop_wait = self.control("停止等待", self.stop_request)
        self.send = self.control("发送", self.send_request, primary=True)
        self.send.setToolTip("Ctrl+Enter 发送")
        bottom.addWidget(self.preview)
        bottom.addStretch()
        bottom.addWidget(self.stop_wait)
        bottom.addWidget(self.send)
        layout.addLayout(bottom)
        send_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self.editor)
        send_shortcut.activated.connect(self.send.click)

        self.job = AIRequest(self)
        self.job.completed.connect(self.receive)
        self.job.failed.connect(self.fail)
        self.job.busy_changed.connect(self.update_controls)
        self.job.elapsed_changed.connect(self.show_wait_time)
        self.set_mode("chat")

    @staticmethod
    def control(label, action, *, primary=False):
        button = QPushButton(label, objectName="primary" if primary else "quiet")
        if action:
            button.clicked.connect(action)
        return button

    def set_mode(self, mode):
        if self.job.results is not None:
            return
        self.mode = mode
        self.mode_buttons[mode].setChecked(True)
        self.editor.setPlaceholderText({
            "chat": "例如「我加了哪些扩展或者怪物 Mod？」「看看第二个的文件」",
            "sort": "可补充排序偏好，例如「补丁和汉化放在各自的本体上方」",
            "log": "可补充想排查的现象，例如「进档时闪退」",
        }[mode])
        self.status.clear()
        self.update_controls()

    def update_controls(self, *_):
        if self.lookup_profile != self.owner.profile.id:
            self.lookup_profile = self.owner.profile.id
            self.last_lookup = None
            self.history.clear()
        provider = self.owner.ai_provider
        destination = f"{provider.model}  ·  {provider.destination}" if provider.model else "尚未接入 AI"
        if provider.model and not provider.configured:
            destination += "  ·  本次尚未填写 API Key"
        self.destination.setText(destination)
        busy = self.job.results is not None
        for widget in (self.new_chat, self.settings, self.attach, self.editor, *self.mode_buttons.values()):
            widget.setEnabled(not busy)
        if self.mode == "sort":
            label = f"附带当前方案 · {len(self.owner.resolved)} 个 Mod"
        elif self.attachment:
            label = self.attachment.label
        elif self.mode == "log":
            label = "先附一份日志"
        else:
            name = redact(self.owner.profile.name, provider.api_key)
            label = f"使用方案「{name}」 · {len(self.owner.resolved)} 个 Mod"
            if self.last_lookup and self.last_lookup.focus in self.owner.plan_positions:
                index = self.owner.plan_positions[self.last_lookup.focus]
                label += f" · 可追问 #{index + 1:02d}"
        self.context.setText(label)
        self.context.setVisible(bool(label))
        self.detach.setVisible(bool(self.attachment and self.mode != "sort"))
        self.detach.setEnabled(not busy)
        content = self.editor.toPlainText().strip()
        if self.mode == "sort":
            sendable = len(self.owner.resolved) >= 2
        elif self.mode == "log":
            sendable = bool(self.attachment and self.attachment.kind == "log")
        else:
            sendable = bool(content or self.attachment)
        self.preview.setEnabled(sendable and not busy)
        self.send.setEnabled(sendable and not busy)
        self.stop_wait.setVisible(busy)
        self.copy.setVisible(bool(self.last_answer))
        self.review.setVisible(self.proposal is not None)
        self.review.setEnabled(not busy)

    def reset_chat(self):
        self.transcript.clear()
        self.history.clear()
        self.last_lookup = None
        self.attachment = None
        self.proposal = None
        self.last_answer = ""
        self.editor.clear()
        self.render_chat()
        self.set_mode("chat")

    def provider_changed(self, old):
        if old.endpoint != self.owner.ai_provider.endpoint:
            self.history.clear()
            self.status.setText("已切换接口。后续提问从新的上下文开始。")
        self.update_controls()

    def render_chat(self):
        theme = current_theme()
        rows = []
        for role, text in self.transcript:
            author = {"user": "你", "local": "本机查询", "assistant": "AI 助手"}[role]
            color = theme.muted if role == "user" else theme.accent
            content = escape(text).replace("\n", "<br>")
            rows.append(
                f'<p style="margin: 13px 8px 3px; color: {color};"><b>{author}</b></p>'
                f'<p style="margin: 0 8px 11px; color: {theme.text};">{content}</p>'
            )
        self.chat.setHtml("".join(rows))
        self.chat.verticalScrollBar().setValue(self.chat.verticalScrollBar().maximum())

    def add_message(self, role, text):
        self.transcript.append((role, text))
        self.render_chat()

    def detach_context(self):
        self.attachment = None
        self.update_controls()

    def attach_problem(self, problem, entries):
        if self.job.results is not None:
            self.status.setText("回复结束后可以附上这个问题。")
            return
        parties = [
            f"{role} #{entries[identity][0]} {entries[identity][1]}"
            for identity, role in problem.targets if identity in entries
        ]
        text = "\n".join((problem.title, problem.message, *parties, problem.advice))
        self.attachment = AssistantAttachment("problem", f"附带问题 · {problem.title}", text)
        self.set_mode("chat")

    def browse_log(self):
        file, _ = QFileDialog.getOpenFileName(self, "选择日志", "", "日志和文本 (*.log *.txt);;所有文件 (*)")
        if file:
            self.read_log(Path(file))

    def read_log(self, path):
        try:
            original, clipped = load_log(path)
        except (OSError, ValueError):
            self.status.setText("无法读取这份日志。")
            return
        self.attach_log(original, path.name, clipped=clipped)

    def paste_log(self):
        text, accepted = QInputDialog.getMultiLineText(self, "粘贴日志", "粘贴要交给 AI 的日志")
        if accepted:
            self.attach_log(text, "粘贴的日志")

    def attach_log(self, original, name, *, clipped=False):
        secret = self.owner.ai_provider.api_key
        log = log_excerpt(original, secret=secret)
        if not log.line_count or not original.strip():
            self.status.setText("这份日志是空的。")
            return
        size = f"{log.line_count} 行" + (" · 末尾摘录" if log.clipped or clipped else "")
        self.attachment = AssistantAttachment("log", f"{redact(name, secret)[:80]} · {size}", log.text)
        self.set_mode("log")

    def prepare(self):
        provider = self.owner.ai_provider
        question = self.editor.toPlainText().strip()
        if len(question) > 2500:
            raise AIError("问题最多 2500 字。较长的日志可用「附日志」添加。")
        clean = redact(question, provider.api_key)
        if self.mode == "sort":
            payload, count, context = self.owner.prepare_ai_sort(clean)
            display = clean or "请检查当前方案，给我一个排序建议。"
            return AssistantTurn(
                SORT_PROMPT, (("user", payload),), display + f"\n附带当前方案 · {count} 个 Mod",
                display, sort_context=context, sort_count=count,
            )
        attachment = self.attachment
        if self.mode == "log" and (not attachment or attachment.kind != "log"):
            raise AIError("请先附上日志。")
        if not clean and not attachment:
            raise AIError("请先输入问题。")
        display = clean or ("请整理这份日志。" if self.mode == "log" else "请解释这条提醒。")
        payload = display
        lookup = None
        evidence = ""
        if self.mode == "chat" and clean and not attachment:
            lookup = lookup_plan(
                question, self.owner.resolved, self.owner.classifications,
                previous=self.last_lookup, selected=self.owner.assistant_plan_selection(),
            )
            if lookup:
                evidence = self.lookup_text(lookup, provider.api_key)
                excerpt = evidence
                evidence_bytes = evidence.encode("utf-8")
                if len(evidence_bytes) > MAX_CHAT_EVIDENCE_BYTES:
                    excerpt = evidence_bytes[:MAX_CHAT_EVIDENCE_BYTES].decode("utf-8", "ignore").rsplit("\n", 1)[0]
                    excerpt += "\n（还有条目因长度限制未附带。）"
                payload += "\n\n本机查到的当前方案资料\n" + excerpt
                display += "\n附带本机查询资料"
        if attachment:
            payload += "\n\n附带资料\n" + redact(attachment.text, provider.api_key)
        prompt = LOG_PROMPT if self.mode == "log" else CHAT_PROMPT
        context = tuple((role, redact(content, provider.api_key)) for role, content in self.history)
        messages = (("user", payload),) if self.mode == "log" else (*context, ("user", payload))
        history_label = payload[:3200] if lookup or (attachment and attachment.kind == "problem") else display
        if attachment:
            display += "\n" + attachment.label
        return AssistantTurn(
            prompt, messages, display, history_label, lookup=lookup,
            lookup_scope=self.owner.profile.id if self.mode == "chat" else "", evidence=evidence,
            plan_ids=tuple(row.entry.id for row in self.owner.resolved) if lookup and lookup.matches else (),
        )

    def lookup_text(self, lookup, secret):
        count = len(self.owner.resolved)
        matched = sum(bool(row.mod) for row in self.owner.resolved)
        return (
            f"当前方案「{redact(self.owner.profile.name, secret)}」 · {count} 个 Mod，有 {matched} 个已定位\n\n"
            + redact(lookup.text, secret)
        )

    def preview_request(self):
        try:
            turn = self.prepare()
        except AIError as exc:
            self.status.setText(str(exc))
            return
        provider = self.owner.ai_provider
        dialog = QDialog(self, windowTitle="发送预览")
        dialog.resize(690, 480)
        layout = QVBoxLayout(dialog)
        label = f"将发送给 {provider.destination}" if provider.configured else "保存 AI 接入设置后可以发送。"
        layout.addWidget(plain_label(label))
        content = "\n\n".join(
            f"{'你的信息' if role == 'user' else '此前的 AI 回复'}\n{body}"
            for role, body in turn.messages
        )
        layout.addWidget(text_view(content), 1)
        action_buttons(dialog, layout)
        dialog.exec()
        dialog.deleteLater()

    def send_request(self):
        try:
            provider = self.owner.ai_provider
            turn = self.prepare()
            if not self.owner.ai_ready():
                self.status.setText("请在「接入设置」配置模型，再发送问题。")
                self.update_controls()
                return
            if provider != self.owner.ai_provider:
                turn = self.prepare()
        except AIError as exc:
            self.status.setText(str(exc))
            return
        self.pending = turn
        if turn.sort_context:
            self.proposal = None
        self.add_message("user", turn.display)
        self.show_wait_time(0)
        self.job.start(self.owner.ai_provider, turn.prompt, turn.messages, purpose=self.mode)

    def remember_lookup(self, lookup):
        if lookup and (lookup.focus or lookup.matches or lookup.topics):
            self.last_lookup = lookup

    def show_wait_time(self, seconds):
        action = f"AI 正在排序 {self.pending.sort_count} 个 Mod" if self.pending and self.pending.sort_count else "AI 正在回复"
        self.status.setText(f"{action} · 已等待 {seconds} 秒")

    def receive(self, result):
        turn, self.pending = self.pending, None
        if turn.lookup_scope and turn.lookup_scope != self.owner.profile.id:
            self.status.setText("已切换方案。再次发送可查询当前方案。")
            return
        secret = self.owner.ai_provider.api_key
        if turn.sort_context:
            try:
                proposal = parse_ai_order(result, turn.sort_count, secret=secret)
            except AIError as exc:
                self.status.setText(str(exc))
                return
            self.proposal = (proposal, turn.sort_context)
            answer = "\n".join(proposal.notes) or "已收到一份完整的排序建议。"
            answer += "\n点「核对排序」看哪些 Mod 会移动。"
        else:
            answer = redact(result, secret)
            if turn.lookup:
                self.remember_lookup(in_reply_order(turn.lookup, answer, turn.plan_ids))
            self.history = [*self.history, ("user", turn.history_label[:3200]), ("assistant", answer[:3200])][-8:]
            self.detach_context()
            self.set_mode("chat")
        self.editor.clear()
        self.last_answer = answer
        self.add_message("assistant", answer)
        self.status.clear()
        self.update_controls()

    def fail(self, message):
        turn, self.pending = self.pending, None
        if turn and turn.lookup and turn.lookup_scope == self.owner.profile.id:
            self.remember_lookup(turn.lookup)
            self.last_answer = turn.evidence
            self.add_message("local", turn.evidence)
            message += " 已显示本机资料，可以重新发送。"
        self.status.setText(message)
        self.update_controls()

    def stop_request(self):
        self.job.stop()
        self.pending = None
        self.status.setText("已停止等待。可以改写消息后再发送。")
        self.update_controls()

    def review_order(self):
        proposal, context = self.proposal
        if not self.owner.sort_context_matches(context):
            self.status.setText("方案已变化，请按当前顺序重新请求 AI 排序。")
            self.proposal = None
        elif self.owner.review_ai_sort(proposal, context):
            self.proposal = None
            self.status.setText("排序已放入方案草稿。")
        self.update_controls()

    def reject(self):
        self.hide()


