"""Render an isolated preview from the actual scan, without desktop capture or user data."""

import os
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QFontDatabase  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from darksort.classification import Category  # noqa: E402
from darksort.models import OrderRule, ProfileEntry, ProfileGroup  # noqa: E402
from darksort.scanning.discovery import discover  # noqa: E402
from darksort.scanning.scanner import scan  # noqa: E402
from darksort.search import ModLocation  # noqa: E402
from darksort.settings import Settings  # noqa: E402
from darksort.ui.main_window import MainWindow  # noqa: E402
from darksort.ui.search import GlobalSearchDialog  # noqa: E402
from darksort.ui.theme import apply_theme  # noqa: E402


def main():
    output = Path(__file__).resolve().parents[1] / "artifacts"
    output.mkdir(exist_ok=True)
    app = QApplication([])
    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for font_name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf"):
        font = fonts / font_name
        if font.is_file():
            QFontDatabase.addApplicationFont(str(font))
    apply_theme(app)
    discovery = discover(Settings())
    result = scan(discovery.roots)
    with tempfile.TemporaryDirectory(prefix="darksort-preview-") as scratch:
        window = MainWindow(Path(scratch), auto_scan=False)
        window.on_scan_completed(discovery, result)
        featured = next((mod for mod in result.mods if mod.preview_path and "Exquisite UI" in mod.name), None)
        if featured is None:
            featured = next((mod for mod in result.mods if mod.preview_path), None)
        font = next((
            mod for mod in result.mods if window.classifications[mod.key].category == Category.FONT
        ), None)
        planned = [mod for mod in (font, featured) if mod is not None]
        planned = list(dict.fromkeys(planned))
        staged = next((mod for mod in result.mods if mod.name == "女性化人物名"), None)
        available = next((mod for mod in result.mods if mod.name == "个人汉化"), None)
        if staged:
            window.profile.staged.append(ProfileEntry.from_mod(staged))
            window.render_lists()
        planned.extend([mod for mod in result.mods if mod not in planned and mod not in (staged, available)][:5])
        window.add_keys([mod.key for mod in planned])
        entries = window.profile.entries
        if len(entries) >= 3:
            window.profile.groups = [
                ProfileGroup(entries[0].id, "基础界面"),
                ProfileGroup(entries[2].id, "翻译与玩法"),
            ]
        if featured and font and font != featured:
            window.profile.rules.append(OrderRule(font.reference, featured.reference, note="界面演示 · 优先顺序"))
        partner = next((mod for mod in reversed(planned) if mod not in (font, featured)), None)
        if featured and partner:
            window.profile.rules.append(OrderRule(
                featured.reference, partner.reference, "requires", "界面演示 · 启用依赖",
            ))
        window.render_lists()
        window.save_profile()
        window.resize(1510, 895)
        window.show()
        window.library_search.set_condition("category", Category.TRANSLATION.value)
        window.library_search.set_mode("filter")
        window.profile_search.query.setText("中文")
        if featured:
            entry = entries[planned.index(featured)]
            window.navigate(ModLocation("profile", entry.id), focus=False)
        records = window.search_records
        dialog = GlobalSearchDialog(records["profile"] + records["library"] + records["staging"], window)
        dialog.query.setText("汉化")
        for widget, name in ((window, "workbench"), (dialog, "global-search")):
            widget.show()
            app.processEvents()
            destination = output / f"darksort-v070-{name}.png"
            if not widget.grab().save(str(destination)):
                raise RuntimeError(f"Could not save preview {destination}")
            print(destination)
        dialog.close()
        window.close()


if __name__ == "__main__":
    main()
