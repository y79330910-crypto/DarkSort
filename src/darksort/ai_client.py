from __future__ import annotations

import base64
import binascii
import ctypes
import ipaddress
import json
import logging
import socket
import ssl
import sys
from ctypes import wintypes
from dataclasses import dataclass, field
from http.client import HTTPException
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from darksort.storage import atomic_json, read_json

DEFAULT_ENDPOINT = "https://api.openai.com/v1/chat/completions"
MAX_REQUEST_BYTES = 160_000
MAX_RESPONSE_BYTES = 1_048_576
RequestPurpose = Literal["probe", "chat", "log", "sort"]
REQUEST_TIMEOUTS = {"probe": 20, "chat": 90, "log": 120, "sort": 180}
logger = logging.getLogger(__name__)


class AIError(ValueError):
    def __init__(self, message: str, *, retry_after: float | None = None, truncated: bool = False):
        super().__init__(message)
        self.retry_after = retry_after
        self.truncated = truncated


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def chat_endpoint(address: str) -> str:
    address = address.strip()
    try:
        parts = urlsplit(address)
        port = parts.port
    except ValueError as exc:
        raise AIError("接口地址或端口格式无效。") from exc
    if (
        parts.scheme not in ("http", "https") or not parts.hostname or port == 0
        or parts.username is not None or parts.password is not None or parts.query or parts.fragment
        or any(character.isspace() or ord(character) < 32 for character in address)
        or len(address) > 1800
    ):
        raise AIError("请输入 HTTP 或 HTTPS 的兼容接口地址，密钥请填写在单独的输入框。")
    path = parts.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        path += "/chat/completions"
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, "", ""))


@dataclass(frozen=True)
class AIProvider:
    endpoint: str = DEFAULT_ENDPOINT
    model: str = ""
    api_key: str = field(default="", repr=False)
    remember_key: bool = False

    @classmethod
    def from_input(cls, address: str, model: str, api_key: str, *, remember_key=False) -> AIProvider:
        endpoint, model, api_key = chat_endpoint(address), model.strip(), api_key.strip()
        if not model or len(model) > 240:
            raise AIError("请填写服务商提供的模型 ID。")
        if len(api_key) > 8192 or any(not 32 < ord(character) < 127 for character in api_key):
            raise AIError("API Key 中包含无效字符。")
        parts = urlsplit(endpoint)
        if api_key and parts.scheme != "https" and not is_loopback(parts.hostname):
            raise AIError("带 API Key 的远程接口需要 HTTPS。")
        return cls(endpoint, model, api_key, bool(api_key and remember_key))

    @property
    def destination(self) -> str:
        parts = urlsplit(self.endpoint)
        return parts.netloc

    @property
    def is_deepseek(self) -> bool:
        return urlsplit(self.endpoint).hostname == "api.deepseek.com"

    @property
    def configured(self) -> bool:
        return bool(self.model and (self.api_key or not self.is_deepseek))


class AISettingsStore:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "ai-settings.json"

    def load(self) -> tuple[AIProvider, str]:
        try:
            data = read_json(self.path, max_bytes=65_536)
        except FileNotFoundError:
            return AIProvider(), ""
        endpoint, model = data.get("endpoint"), data.get("model")
        encrypted = data.get("protected_key", "")
        if data.get("version") != 1 or not all(isinstance(x, str) for x in (endpoint, model, encrypted)):
            raise AIError("AI 接入设置格式无效。")
        provider = AIProvider.from_input(endpoint, model, "")
        if not encrypted:
            return provider, ""
        try:
            secure = base64.b64decode(encrypted, validate=True)
            key = protect_key(secure, provider.endpoint, decrypt=True).decode("ascii")
            return AIProvider.from_input(endpoint, model, key, remember_key=True), ""
        except (OSError, AIError, binascii.Error, UnicodeError):
            return provider, "保存的 API Key 无法解密，请重新输入。"

    def save(self, provider: AIProvider) -> None:
        data = {"version": 1, "endpoint": provider.endpoint, "model": provider.model}
        if provider.api_key and provider.remember_key:
            encrypted = protect_key(provider.api_key.encode("ascii"), provider.endpoint)
            data["protected_key"] = base64.b64encode(encrypted).decode("ascii")
        atomic_json(self.path, data)


class DataBlob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.c_void_p)]


def protect_key(data: bytes, endpoint: str, *, decrypt=False) -> bytes:
    """Protect the persisted key for the Windows user and the configured endpoint."""
    if sys.platform != "win32":
        raise AIError("此系统可在本次运行中使用密钥，Windows 支持加密记住密钥。")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    process = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    blob_pointer = ctypes.POINTER(DataBlob)
    process.argtypes = [
        blob_pointer, ctypes.c_void_p, blob_pointer, ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, blob_pointer,
    ]
    process.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    entropy_bytes = ("DarkSort AI " + endpoint).encode("utf-8")
    entropy_buffer = ctypes.create_string_buffer(entropy_bytes)
    input_blob = DataBlob(len(data), ctypes.addressof(buffer))
    entropy_blob = DataBlob(len(entropy_bytes), ctypes.addressof(entropy_buffer))
    output = DataBlob()
    try:
        if not process(ctypes.byref(input_blob), None, ctypes.byref(entropy_blob), None, None, 1, ctypes.byref(output)):
            raise OSError("Windows 无法加密或解密此 API Key。")
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.memset(ctypes.addressof(buffer), 0, ctypes.sizeof(buffer))
        if output.data:
            if decrypt:
                ctypes.memset(output.data, 0, output.size)
            kernel32.LocalFree(output.data)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_text(
    provider: AIProvider, prompt: str, payload: str, *,
    timeout: float | None = None, purpose: RequestPurpose = "chat",
) -> str:
    return request_chat(provider, prompt, (("user", payload),), timeout=timeout, purpose=purpose)


def request_chat(
    provider: AIProvider, prompt: str, messages: tuple[tuple[str, str], ...], *,
    timeout: float | None = None, purpose: RequestPurpose = "chat",
) -> str:
    timeout = REQUEST_TIMEOUTS[purpose] if timeout is None else timeout
    options = {
        "model": provider.model,
        "messages": [
            {"role": "system", "content": prompt},
            *({"role": role, "content": text} for role, text in messages),
        ],
    }
    if provider.is_deepseek:
        if not provider.api_key:
            raise AIError("本次运行未加载 DeepSeek 的 API Key，请在「接入设置」重新填写。")
        if purpose in ("probe", "sort"):
            options["thinking"] = {"type": "disabled"}
            options["max_tokens"] = 32 if purpose == "probe" else 8192
        if purpose == "sort":
            options["response_format"] = {"type": "json_object"}
    body = json.dumps(options, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_REQUEST_BYTES:
        raise AIError("本次内容超过 AI 请求大小上限，请缩短输入。")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if provider.api_key:
        headers["Authorization"] = "Bearer " + provider.api_key
    request = Request(provider.endpoint, data=body, method="POST", headers=headers)
    handlers = [NoRedirect()]
    if is_loopback(urlsplit(provider.endpoint).hostname):
        handlers.append(ProxyHandler({}))
    try:
        with build_opener(*handlers).open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        exc.close()
        logger.warning("AI %s HTTP status %d", purpose, exc.code)
        if 300 <= exc.code < 400:
            raise AIError("接口返回跳转，请填写实际的 API 地址。") from None
        help_text = {
            400: "检查模型 ID 和服务商的兼容接口。",
            401: "请检查 API Key。" if provider.api_key else "请先在接入设置填写 API Key。",
            402: "账户余额不足，请到服务商控制台核对额度。",
            403: "请检查密钥和模型访问权限。",
            404: "请核对接口地址和模型 ID。",
            408: "请求在服务端超时，请稍后重试。",
            413: "服务商拒绝了过大的请求，可缩短日志或检查方案大小。",
            422: "服务商不能处理这组请求参数，请核对模型和接口类型。",
            429: "请求频率或额度达到限制，请稍后重试。",
            502: "服务商网关暂时不可用，请稍后重试。",
            503: "服务暂时繁忙，请稍后重试。",
            504: "服务商网关等待超时，请稍后重试。",
        }.get(exc.code, "请检查服务状态。")
        retry = None
        if exc.code in (429, 502, 503, 504):
            header = exc.headers.get("Retry-After", "").strip() if exc.headers else ""
            if not header or (header.isascii() and header.isdecimal() and len(header) <= 2):
                delay = int(header) if header else 4
                if delay <= 30:
                    retry = delay
        raise AIError(f"AI 服务返回 HTTP {exc.code}。{help_text}", retry_after=retry) from None
    except (OSError, HTTPException, ValueError) as exc:
        reason = exc.reason if isinstance(exc, URLError) else exc
        logger.warning("AI %s transport failure (%s)", purpose, type(reason).__name__)
        raise AIError(transport_message(reason, timeout)) from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise AIError("AI 返回内容过长。")
    try:
        choice = json.loads(raw)["choices"][0]
        content = choice["message"]["content"]
    except (IndexError, KeyError, TypeError, ValueError, RecursionError):
        raise AIError("服务返回的响应不符合 Chat Completions 文本格式。") from None
    if choice.get("finish_reason") == "length":
        raise AIError("模型的回答被截断。请换用支持更长输出的模型或缩短输入。", truncated=True)
    if not isinstance(content, str) or not content.strip():
        if purpose == "sort":
            raise AIError("模型返回了空的排序结果，可以重试或使用本地智能排序。")
        raise AIError("模型没有返回可用文本，请检查模型配置。")
    return content.strip()


def transport_message(reason, timeout):
    if isinstance(reason, TimeoutError):
        return f"等待 AI 回复超时（{timeout:g} 秒）。可先用「测试连接」核对短请求，再重试。"
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "AI 接口的 HTTPS 证书无法验证，请检查系统时间和代理证书。"
    if isinstance(reason, socket.gaierror):
        return "找不到 AI 接口域名，请核对地址或检查 DNS。"
    if isinstance(reason, ConnectionRefusedError):
        return "AI 接口或代理拒绝连接，请检查服务和代理是否已启动。"
    if isinstance(reason, (ConnectionResetError, HTTPException)):
        return "连接在收到完整回复前中断，请检查网络或代理后重试。"
    return "无法建立 AI 接口连接。请检查网络和系统代理后重试。"
