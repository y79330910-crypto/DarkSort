from __future__ import annotations

import ctypes
import os
import sys
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def external_program_environment(*, inherit_path: bool = False):
    bundled = getattr(sys, "_MEIPASS", None)
    if not bundled or sys.platform != "win32":
        yield None
        return

    # Windows children must resolve their dependencies outside the PyInstaller bundle.
    from ctypes import wintypes

    set_dll_path = ctypes.WinDLL("kernel32", use_last_error=True).SetDllDirectoryW
    set_dll_path.argtypes = (wintypes.LPCWSTR,)
    set_dll_path.restype = wintypes.BOOL
    bundle_dir = Path(bundled).resolve()
    env = os.environ.copy()
    old_path = env.get("PATH")
    env["PATH"] = os.pathsep.join(
        entry for entry in (old_path or "").split(os.pathsep)
        if entry and not Path(entry.strip('"')).resolve().is_relative_to(bundle_dir)
    )
    if not set_dll_path(None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if inherit_path:
            os.environ["PATH"] = env["PATH"]
        yield env
    finally:
        if inherit_path:
            if old_path is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = old_path
        set_dll_path(bundled)
