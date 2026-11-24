import os
import sys

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

from darksort.adapters.windows_launch import external_program_environment
from darksort.workshop import workshop_id, workshop_page


def steam_protocol_registered() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"steam\shell\open\command") as key:
            command, _ = winreg.QueryValueEx(key, "")
    except OSError:
        return False
    return isinstance(command, str) and bool(command.strip())


def dispatch_url(url: str) -> bool:
    if sys.platform != "win32":
        return QDesktopServices.openUrl(QUrl(url))
    try:
        with external_program_environment(inherit_path=True):
            os.startfile(url, "open")
    except (OSError, NotImplementedError):
        return False
    return True


def open_workshop_page(identifier: str | None, *, browser: bool = False) -> str | None:
    number = workshop_id(identifier)
    if number is None:
        return None
    if not browser and steam_protocol_registered():
        if dispatch_url(f"steam://url/CommunityFilePage/{number}"):
            return "Steam"
    if dispatch_url(workshop_page(number)):
        return "浏览器"
    return None
