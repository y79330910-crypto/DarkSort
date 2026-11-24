from __future__ import annotations

import json
import sys
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from threading import Event, Thread
from urllib.error import URLError

import pytest
from PySide6.QtWidgets import QDialog

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
    MOD_TEXT_PROMPT,
    SORT_PROMPT,
    load_log,
    log_excerpt,
    parse_ai_order,
    preview_ai_order,
    sort_payload,
)
from darksort.classification import classify_mod
from darksort.mod_files import read_mod_text
from darksort.mod_text_batch import MAX_BATCH_ITEMS, MOD_TEXT_BATCH_PROMPT
from darksort.models import OrderRule, ProfileEntry, ProfileLock, ScanResult, Source
from darksort.profiles import ProfileStore, resolve_entries
from darksort.scanning.content import inspect_resources
from darksort.scanning.discovery import Discovery
from darksort.settings import Settings
from darksort.sorting import preview_order
from darksort.ui.ai_dialogs import AISettingsDialog, ModTextDialog
from darksort.ui.main_window import MainWindow
from darksort.ui.plan_dialogs import SortPreviewDialog


@pytest.fixture
def chat_replies():
    return {}


@pytest.fixture
def batch_replies():
    return {}


def translated_rows(rows):
    return {"mods": [
        {"id": row["id"], **{field: f"{row['title']} {field}" for field in row["fields"]}}
        for row in rows
    ]}


@pytest.fixture
def compatible_server(chat_replies, batch_replies):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, self.headers.get("Authorization"), message))
            if self.path.startswith("/redirect/"):
                self.send_response(307)
                self.send_header("Location", "/wrong/chat/completions")
                self.end_headers()
                return
            if self.path.startswith("/unavailable/"):
                self.send_response(503)
                self.end_headers()
                return
            prompt = message["messages"][0]["content"]
            if prompt == SORT_PROMPT:
                entries = json.loads(message["messages"][1]["content"])["mods"]
                content = json.dumps({"order": [mod["id"] for mod in reversed(entries)]})
            elif prompt == LOG_PROMPT:
                content = "主要错误 · [L1] 无法读取配置"
            elif prompt == MOD_TEXT_PROMPT:
                content = json.dumps({"alias": "营地助手", "translation": "新增营地功能。"}, ensure_ascii=False)
            elif prompt == MOD_TEXT_BATCH_PROMPT:
                rows = json.loads(message["messages"][1]["content"])["mods"]
                code, result = batch_replies["respond"](rows) if "respond" in batch_replies else (200, translated_rows(rows))
                if code != 200:
                    self.send_response(code)
                    self.send_header("Retry-After", "0")
                    self.end_headers()
                    return
                content = json.dumps(result, ensure_ascii=False)
            else:
                question = message["messages"][-1]["content"].partition("\n\n")[0]
                content = chat_replies.get(question, "OK")
            data = json.dumps({"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data.encode())

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", calls
        finally:
            server.shutdown()
            thread.join(timeout=2)


def test_chat_supports_openai_compatible_api_and_disallows_redirects(compatible_server):
    endpoint, calls = compatible_server
    provider = AIProvider.from_input(endpoint + "/v1", "case-model", "test-key")
    assert chat_endpoint(provider.endpoint + "/") == provider.endpoint
    assert request_text(provider, "system rule", "user input") == "OK"
    assert calls == [("/v1/chat/completions", "Bearer test-key", {
        "model": "case-model",
        "messages": [{"role": "system", "content": "system rule"},
                     {"role": "user", "content": "user input"}],
    })]
    with pytest.raises(AIError, match="跳转"):
        request_text(replace(provider, endpoint=endpoint + "/redirect/chat/completions"), "s", "u")
    assert len(calls) == 2
    with pytest.raises(AIError, match="HTTPS"):
        AIProvider.from_input("http://models.example/v1", "model", "test-key")


def test_deepseek_large_sort_requests_fast_json_and_accepts_all_ids(monkeypatch):
    calls = []
    response_order = list(range(349, 0, -1))
    response = {
        "choices": [{"message": {"content": json.dumps({"order": response_order})}, "finish_reason": "stop"}]
    }

    class Opener:
        def open(self, request, *, timeout):
            calls.append((json.loads(request.data), timeout))
            return BytesIO(json.dumps(response).encode())

    monkeypatch.setattr("darksort.ai_client.build_opener", lambda *_: Opener())
    provider = AIProvider.from_input("https://api.deepseek.com", "deepseek-flash", "test-key")
    payload = json.dumps({"mods": [{"id": index} for index in range(1, 350)]})
    reply = request_chat(provider, SORT_PROMPT, (("user", payload),), purpose="sort")
    assert parse_ai_order(reply, 349).order == tuple(response_order)
    body, timeout = calls[0]
    assert timeout >= 120
    assert body["thinking"] == {"type": "disabled"}
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] >= 2048
    with pytest.raises(AIError, match="未加载 DeepSeek 的 API Key"):
        request_text(replace(provider, api_key=""), "s", "u", purpose="probe")
    assert len(calls) == 1


def test_request_explains_timeout_without_echoing_transport_details(monkeypatch, caplog):
    class Opener:
        def open(self, _request, *, timeout):
            assert timeout == 7
            raise URLError(TimeoutError("fake-secret-should-not-appear"))

    monkeypatch.setattr("darksort.ai_client.build_opener", lambda *_: Opener())
    provider = AIProvider.from_input("https://models.example/v1", "demo", "fake-secret-should-not-appear")
    with pytest.raises(AIError, match="等待 AI 回复超时（7 秒）") as failure:
        request_text(provider, "s", "u", timeout=7)
    assert provider.api_key not in str(failure.value) and provider.api_key not in caplog.text


@pytest.mark.skipif(sys.platform != "win32", reason="Windows credential protection")
def test_remembered_key_uses_user_protection_and_is_bound_to_endpoint(tmp_path):
    store = AISettingsStore(tmp_path)
    provider = AIProvider.from_input("https://service.example/v1", "test-model", "phony-test-key", remember_key=True)
    store.save(provider)
    assert "phony-test-key" not in store.path.read_text(encoding="utf-8")
    assert store.load() == (provider, "")
    config = json.loads(store.path.read_bytes())
    config["endpoint"] = "https://different.example/v1/chat/completions"
    store.path.write_text(json.dumps(config), encoding="utf-8")
    loaded, warning = store.load()
    assert not loaded.api_key and warning
    store.save(replace(provider, remember_key=False))
    assert "protected_key" not in json.loads(store.path.read_bytes())


def test_ai_order_validates_all_ids_and_respects_rules_locks_and_unresolved_rows(tmp_path, mods):
    mock_mods = [replace(mod, resource_kinds=("gameplay",)) for mod in mods]
    a, b, c = [ProfileEntry.from_mod(mod) for mod in mock_mods]
    missing = ProfileEntry("未定位", Source.LOCAL, str(tmp_path / "local-path"))
    profile = ProfileStore(tmp_path).new("AI test", [a, b, missing, c])
    profile.locks = [ProfileLock("同组", (a.id, b.id))]
    profile.rules = [OrderRule(b.reference, c.reference, note="不进入 AI 数据")]
    resolved = resolve_entries(profile.entries, mock_mods)
    classification = {mod.key: classify_mod(mod) for mod in mock_mods}
    local = preview_order(profile, resolved, classification)
    payload = sort_payload(profile, resolved, classification, local)
    assert str(tmp_path) not in payload and "不进入 AI 数据" not in payload
    assert json.loads(payload)["locks"] == [[1, 2]]
    assert json.loads(payload)["reserved"] == [3]
    proposal = parse_ai_order('{"order":[4,2,3,1],"notes":["模型说明"]}', 4)
    preview = preview_ai_order(profile, resolved, classification, proposal)
    assert preview.entries == (a, b, missing, c)
    assert "本地规则" in preview.notices[0]
    assert "AI 建议" in preview.decisions[0].reason
    for body in ('{"order":[1,1,3,4]}', '{"order":[true,2,3,4]}', '{"order":[0,2,3,4]}'):
        with pytest.raises(AIError, match="顺序"):
            parse_ai_order(body, 4)


def test_log_excerpt_preserves_evidence_and_redacts_tokens_and_home_directories(tmp_path):
    raw = (
        "ERROR on C:\\Users\\Some Person\\Desktop\\slot.log\n"
        '{"api_key":"another-secret", "token":"test-token"}\n'
        "Cookie: uid=private; session=contains-secrets\n"
        "Authorization: Bearer local-token\n"
        "WARN forwarded plaintext mock-live-key\n"
    )
    log = tmp_path / "中文日志.log"
    log.write_bytes(raw.encode("utf-16"))
    text, clipped = load_log(log)
    assert not clipped
    excerpt = log_excerpt(text, secret="mock-live-key")
    assert excerpt.line_count == 5
    assert "[L1] ERROR on <用户目录>\\Desktop" in excerpt.text
    for private in ("Some Person", "another-secret", "test-token", "contains-secrets", "local-token", "mock-live-key"):
        assert private not in excerpt.text
    later = log_excerpt("INFO repeated\n" * 430 + "ERROR final")
    assert later.clipped and later.line_count == 420 and "[L431] ERROR final" in later.text


def test_client_settings_are_usable_without_saved_prompts(qtbot, tmp_path, compatible_server):
    endpoint, calls = compatible_server
    store = AISettingsStore(tmp_path)
    dialog = AISettingsDialog(store, AIProvider())
    qtbot.addWidget(dialog)
    dialog.endpoint.setText(endpoint + "/v1")
    dialog.model.setText("log-model")
    dialog.api_key.setText("session-test-key")
    dialog.probe.click()
    qtbot.waitUntil(lambda: "连接成功" in dialog.status.text(), timeout=3000)
    dialog.save.click()
    assert dialog.provider.api_key == "session-test-key"
    assert not store.load()[0].api_key
    assert len(calls) == 1 and "Mod" not in calls[0][2]["messages"][1]["content"]


def test_ai_mod_text_uses_shared_provider_and_saves_only_reviewed_display(qtbot, tmp_path, mods, monkeypatch, compatible_server):
    endpoint, calls = compatible_server
    mod = replace(mods[0], name="Camp projects", description="Adds camping options.\napi_key=synthetic-value")
    window = MainWindow(tmp_path / "数据", auto_scan=False)
    qtbot.addWidget(window)
    window.on_scan_completed(Discovery(), ScanResult(mods=[mod]))
    window.library_list.item(0).setSelected(True)
    window.ai_provider = AIProvider.from_input(endpoint + "/v1", "existing-model", "shared-test-key")

    def review(dialog):
        qtbot.waitUntil(lambda: dialog.alias.text() == "营地助手", timeout=3000)
        assert dialog.translation.toPlainText() == "新增营地功能。"
        assert not window.settings.mod_aliases
        dialog.translation.setPlainText("可在营地使用的功能。")
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ModTextDialog, "exec", review)
    window.detail_generation_button.click()
    settings = Settings.load(window.data_dir)
    assert settings.mod_aliases[mod.reference] == "营地助手"
    assert settings.mod_translations[mod.reference] == "可在营地使用的功能。"
    assert window.detail_original.text() == "原名 · Camp projects"
    assert window.detail_description.toPlainText() == "可在营地使用的功能。"
    assert window.search_records["library"][0].rank("camp projects") == 1
    assert window.search_records["library"][0].rank("营地助手") == 1
    assert window.mods[0] == mod
    assert len(calls) == 1 and calls[0][:2] == ("/v1/chat/completions", "Bearer shared-test-key")
    payload = calls[0][2]
    assert payload["model"] == "existing-model" and payload["messages"][0]["content"] == MOD_TEXT_PROMPT
    assert "synthetic-value" not in payload["messages"][1]["content"]


def batch_mods(base, count):
    return [replace(base, workshop_id=str(i + 800), path=f"{base.path}-{i}", name=f"Mod {i}",
                    description="Adds a feature.\napi_key=private-test-value" if i else "") for i in range(count)]


def test_batch_translation_from_ai_menu_runs_when_hidden_and_saves_each_batch(
    qtbot, tmp_path, mods, monkeypatch, compatible_server, batch_replies,
):
    endpoint, calls = compatible_server
    installed = batch_mods(mods[0], MAX_BATCH_ITEMS + 2)
    first_response, second_response = Event(), Event()

    def respond(rows):
        gate = first_response if len(calls) == 1 else second_response
        gate.wait(5)
        return 200, translated_rows(rows)

    batch_replies["respond"] = respond
    duplicate = replace(installed[1], path=str(tmp_path / "duplicate"), name="Duplicate")
    window = MainWindow(tmp_path / "manager", auto_scan=False)
    qtbot.addWidget(window, before_close_func=lambda w: setattr(w, "maybe_save", lambda: True))
    window.on_scan_completed(Discovery(), ScanResult(mods=[*installed, duplicate]))
    window.add_keys([installed[2].key])
    window.ai_provider = AIProvider.from_input(endpoint, "existing", "shared-test-key")
    window.save_mod_display({installed[1].reference: "保留别名"}, {installed[2].reference: "保留译文"})
    panel = window.mod_text_batch
    window.show()
    assert window.batch_text_action.parent().title() == "AI"
    window.batch_text_action.trigger()
    qtbot.waitUntil(lambda: len(calls) == 1)
    assert panel.running and panel.progress.maximum() == len(installed) and panel.isVisible()
    panel.hide_button.click()
    assert panel.isHidden() and panel.running
    window.batch_text_action.trigger()
    assert panel.isVisible() and len(calls) == 1
    panel.hide_button.click()

    def edit_alias(dialog):
        dialog.alias.setText("处理中手动改名")
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(ModTextDialog, "exec", edit_alias)
    window.edit_mod_text(installed[3])
    first_response.set()
    qtbot.waitUntil(lambda: len(calls) == 2, timeout=4000)
    saved = Settings.load(window.data_dir)
    assert saved.mod_aliases[installed[0].reference] == "Mod 0 alias"
    assert saved.mod_aliases[installed[1].reference] == "保留别名"
    assert saved.mod_translations[installed[2].reference] == "保留译文"
    assert saved.mod_aliases[installed[3].reference] == "处理中手动改名"
    assert saved.mod_translations[installed[3].reference] == "Mod 3 translation"
    assert installed[-1].reference not in saved.mod_translations
    assert panel.progress.value() == MAX_BATCH_ITEMS and panel.isHidden()
    second_response.set()
    qtbot.waitUntil(lambda: not panel.running, timeout=4000)
    assert panel.isHidden() and panel.progress.value() == len(installed)
    assert "完成" in window.statusBar().currentMessage()
    window.batch_text_progress_action.trigger()
    assert panel.isVisible()
    saved = Settings.load(window.data_dir)
    assert set(saved.mod_aliases) == {mod.reference for mod in installed}
    assert set(saved.mod_translations) == {mod.reference for mod in installed[1:]}
    for path, bearer, body in calls:
        assert (path, bearer, body["model"]) == ("/chat/completions", "Bearer shared-test-key", "existing")
        assert "private-test-value" not in body["messages"][1]["content"]
    window.batch_text_action.trigger()
    assert not panel.running and len(calls) == 2


def test_batch_translation_retries_rate_limit_and_resumes_only_unsaved_fields(
    qtbot, tmp_path, mods, compatible_server, batch_replies,
):
    endpoint, calls = compatible_server
    installed = batch_mods(mods[0], 2)

    def respond(rows):
        if len(calls) == 1:
            return 429, None
        if len(calls) == 2:
            return 200, {"mods": [{"id": row["id"], "alias": row["title"]} for row in rows]}
        if len(calls) == 3:
            return 401, None
        return 200, translated_rows(rows)

    batch_replies["respond"] = respond
    window = MainWindow(tmp_path / "manager", auto_scan=False)
    qtbot.addWidget(window)
    window.on_scan_completed(Discovery(), ScanResult(mods=installed))
    window.ai_provider = AIProvider.from_input(endpoint, "model", "test-key")
    window.batch_text_action.trigger()
    panel = window.mod_text_batch
    qtbot.waitUntil(lambda: not panel.running, timeout=4000)
    assert len(calls) == 3 and "HTTP 401" in panel.details.text()
    assert panel.progress.value() == 1
    assert len(Settings.load(window.data_dir).mod_aliases) == 2
    window.batch_text_action.trigger()
    qtbot.waitUntil(lambda: not panel.running, timeout=4000)
    assert len(calls) == 4
    rows = json.loads(calls[-1][2]["messages"][1]["content"])["mods"]
    assert [(row["title"], row["fields"]) for row in rows] == [("Mod 1", ["translation"])]
    assert Settings.load(window.data_dir).mod_translations == {installed[1].reference: "Mod 1 translation"}


def test_assistant_sends_redacted_logs_and_supports_follow_up(qtbot, tmp_path, compatible_server):
    endpoint, calls = compatible_server
    window = MainWindow(tmp_path, auto_scan=False)
    qtbot.addWidget(window)
    window.ai_provider = AIProvider.from_input(endpoint + "/v1", "log-model", "")
    panel = window.open_ai_assistant()
    panel.attach_log("ERROR on C:\\Users\\PrivateName\\file\napi_key=abc1234", "example.log")
    assert panel.mode == "log" and panel.send.isEnabled()
    preview = panel.prepare().messages[-1][1]
    assert calls == []
    panel.send.click()
    qtbot.waitUntil(lambda: "[L1]" in panel.last_answer, timeout=3000)
    sent = calls[0][2]["messages"][1]["content"]
    assert sent == preview
    assert "PrivateName" not in sent and "abc1234" not in sent
    assert panel.mode == "chat" and panel.attachment is None
    panel.editor.setPlainText("下一步怎么办")
    panel.send.click()
    qtbot.waitUntil(lambda: panel.last_answer == "OK", timeout=3000)
    follow_up = calls[1][2]["messages"]
    assert follow_up[0]["content"] == CHAT_PROMPT
    assert follow_up[-1]["content"] == "下一步怎么办"
    assert follow_up[-2]["role"] == "assistant"
    assert all("api_key" not in item["content"] for item in follow_up)
    panel.close()
    assert window.open_ai_assistant() is panel and "下一步怎么办" in panel.chat.toPlainText()


def test_assistant_sends_plan_evidence_by_default_and_follows_the_model_reply_after_reorder(
    qtbot, tmp_path, mods, monkeypatch, compatible_server, chat_replies,
):
    endpoint, calls = compatible_server
    fixture_files = (
        ("修女换装示例", "heroes/vestal/pack_vestal_G/anim/idle.png", "image"),
        ("营地玩法扩充", "campaign/town/events.town_events.events.json", "{}"),
        ("遗迹怪物包", "monsters/sample/sample.info.darkest",
         'monster: .id "practice_hound"\nnote: 练习标记\n'),
    )
    scanned = []
    for mod, (name, relative, text) in zip(mods, fixture_files, strict=True):
        root = Path(mod.path)
        file = root / relative
        file.parent.mkdir(parents=True)
        file.write_text(text, encoding="utf-8")
        (root / "modfiles.txt").write_text(f"{relative} {len(text.encode())}", encoding="utf-8")
        scanned.append(inspect_resources(replace(mod, name=name), lambda: False))
    window = MainWindow(tmp_path / "manager", auto_scan=False)
    qtbot.addWidget(window, before_close_func=lambda w: setattr(w, "maybe_save", lambda: True))
    window.on_scan_completed(Discovery(), ScanResult(mods=scanned))
    window.activate_profile(window.store.new("远征方案"))
    window.add_keys([item.key for item in scanned])
    panel = window.open_ai_assistant()
    missing_setup = []
    monkeypatch.setattr(window, "configure_ai", lambda: missing_setup.append(True))
    panel.editor.setPlainText("我加了哪些皮肤的？")
    panel.send.click()
    assert missing_setup == [True] and calls == [] and panel.transcript == []
    assert "接入设置" in panel.status.text() and panel.editor.toPlainText() == "我加了哪些皮肤的？"

    window.ai_provider = AIProvider.from_input(endpoint + "/v1", "demo", "")
    chat_replies["我加了哪些皮肤的？"] = "查到 1 项皮肤，#01 修女换装示例。"
    chat_replies["我加了哪些扩展或者怪物 Mod？"] = "查到两项。\n- #03 遗迹怪物包\n- #02 营地玩法扩充"

    def ask(question):
        panel.editor.setPlainText(question)
        request_count = len(calls)
        transcript_count = len(panel.transcript)
        panel.send.click()
        qtbot.waitUntil(lambda: len(panel.transcript) == transcript_count + 2, timeout=3000)
        assert len(calls) == request_count + 1
        assert panel.transcript[-1] == ("assistant", chat_replies.get(question, "OK"))
        return calls[-1][2]["messages"][-1]["content"]

    first = ask("我加了哪些皮肤的？")
    assert "#01 修女换装示例 → 修女" in first and "当前方案「远征方案」" in first
    assert "默认配置" not in first and "使用方案「远征方案」" in panel.context.text()
    assert "#02 营地玩法扩充" in ask("整理当前方案")
    ask("我加了哪些皮肤的？")
    coverage = ask("那原版还有哪些没加？")
    assert "游戏本体 · 1/15" in coverage and "官方 DLC 角色 · 0/3" in coverage
    assert "未查到皮肤（14）" in coverage and "修女  #01" in coverage
    groups = ask("我加了哪些扩展或者怪物 Mod？")
    assert "#02 营地玩法扩充" in groups and "#03 遗迹怪物包" in groups
    assert "刚才列出了多个 Mod" in ask("它的文件是什么？")
    panel.close()
    assert window.open_ai_assistant() is panel
    window.handle_drop("profile", {"side": "profile", "values": [2]}, 0)
    evidence = ask("看看第一个的怪物文件")
    assert "#01 遗迹怪物包\nmonsters/sample/sample.info.darkest" in evidence
    assert '第 1 行  monster: .id "practice_hound"' in evidence
    assert "第 2 行  note: 练习标记" in ask("这个文件里有「练习标记」吗？")
    window.activate_profile(window.store.new("空方案"))
    empty_plan = ask("整理当前方案")
    assert "当前方案还没有加入 Mod" in empty_plan and "遗迹怪物包" not in empty_plan
    assert panel.last_lookup is None and len(calls[-1][2]["messages"]) == 2


def test_assistant_sends_bounded_mod_file_evidence_and_keeps_failed_question_for_retry(
    qtbot, tmp_path, mods, compatible_server,
):
    endpoint, calls = compatible_server
    mod = replace(mods[0], name="演示模组")
    root = Path(mod.path)
    root.mkdir(parents=True)
    (root / "project.xml").write_text(
        '<project><Title>演示模组</Title>\napi_key=synthetic-test-secret\n</project>', encoding="utf-8",
    )
    (root.parent / "outside.txt").write_text("outside scope", encoding="utf-8")
    with pytest.raises(ValueError, match="Mod 文本范围"):
        read_mod_text(root, "../outside.txt")

    window = MainWindow(tmp_path / "manager", auto_scan=False)
    qtbot.addWidget(window, before_close_func=lambda w: setattr(w, "maybe_save", lambda: True))
    window.on_scan_completed(Discovery(), ScanResult(mods=[mod]))
    window.add_keys([mod.key])
    window.ai_provider = AIProvider.from_input(endpoint + "/v1", "demo", "")
    panel = window.open_ai_assistant()
    panel.editor.setPlainText("查 #1 的 project.xml")
    prepared = panel.prepare().messages[-1][1]
    assert "第 1 行" in prepared and "synthetic-test-secret" not in prepared and not calls
    panel.send.click()
    qtbot.waitUntil(lambda: panel.last_answer == "OK", timeout=3000)
    assert calls[0][2]["messages"][-1]["content"] == prepared
    panel.editor.setPlainText("解释一下这个文件")
    prepared = panel.prepare().messages[-1][1]
    assert "本机查到的当前方案资料" in prepared and "project.xml" in prepared
    assert str(tmp_path) not in prepared and "synthetic-test-secret" not in prepared
    window.ai_provider = AIProvider.from_input(endpoint + "/unavailable/v1", "demo", "")
    panel.send.click()
    qtbot.waitUntil(lambda: "HTTP 503" in panel.status.text(), timeout=3000)
    assert panel.transcript[-1][0] == "local" and "第 1 行" in panel.last_answer
    assert panel.editor.toPlainText() == "解释一下这个文件"
    assert calls[1][2]["messages"][-1]["content"] == prepared


def test_ai_sort_workbench_applies_only_after_review_and_takes_snapshot(
    qtbot, tmp_path, mods, monkeypatch, compatible_server,
):
    endpoint, calls = compatible_server
    window = MainWindow(tmp_path, auto_scan=False)
    qtbot.addWidget(window, before_close_func=lambda w: setattr(w, "maybe_save", lambda: True))
    classified = [replace(mod, resource_kinds=("gameplay",)) for mod in mods]
    window.on_scan_completed(Discovery(), ScanResult(mods=classified))
    window.add_keys([mod.key for mod in classified])
    window.save_profile()
    window.ai_provider = AIProvider.from_input(endpoint + "/v1", "sort-model", "")
    original = list(window.profile.entries)

    def send_order(panel):
        assert window.profile.entries == original
        panel.editor.setPlainText("把补丁放在对应本体上面")
        panel.send.click()
        assert "已等待" in panel.status.text()
        qtbot.waitUntil(lambda: panel.proposal is not None, timeout=3000)
        assert window.profile.entries == original

    window.ai_sort_profile()
    panel = window.assistant
    send_order(panel)
    monkeypatch.setattr(SortPreviewDialog, "exec", lambda _: QDialog.DialogCode.Rejected)
    panel.review.click()
    assert window.profile.entries == original
    window.refresh_classifications()
    panel.review.click()
    assert "方案已变化" in panel.status.text() and panel.proposal is None
    send_order(panel)
    monkeypatch.setattr(SortPreviewDialog, "exec", lambda _: QDialog.DialogCode.Accepted)
    panel.review.click()
    assert window.profile.entries == original[::-1]
    assert window.store.load(window.profile.id).entries == original
    assert window.history.list(window.profile.id)[0][0].reason == "AI 排序前"
    assert len(calls) == 2
    assert "把补丁放在对应本体上面" in calls[1][2]["messages"][1]["content"]
