from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QLibraryInfo, QLocale, QLockFile, QTranslator
from PySide6.QtWidgets import QApplication, QMessageBox

from darksort.storage import default_data_dir
from darksort.ui.main_window import MainWindow
from darksort.ui.theme import apply_theme


def main() -> int:
    parser = argparse.ArgumentParser(description="DarkSort · 暗黑地牢 Mod 配置管理")
    parser.add_argument("--data-dir", type=Path, help="使用独立数据目录（测试与便携部署）")
    parser.add_argument("--no-auto-scan", action="store_true", help="启动时不自动扫描")
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("DarkSort")
    app.setOrganizationName("DarkSort")
    QLocale.setDefault(QLocale("zh_CN"))
    translator = QTranslator(app)
    translator.load("qtbase_zh_CN", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))
    app.installTranslator(translator)
    apply_theme(app)
    data_dir = (args.data_dir or default_data_dir()).expanduser().resolve()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            data_dir / "darksort.log", maxBytes=1024 * 1024, backupCount=2, encoding="utf-8"
        )
        logging.basicConfig(
            level=logging.INFO, handlers=[handler], format="%(asctime)s %(levelname)s %(message)s"
        )
        lock = QLockFile(str(data_dir / "darksort.lock"))
        if not lock.tryLock(0):
            QMessageBox.information(
                None, "DarkSort 已在运行", "此数据目录已被另一个 DarkSort 使用。"
            )
            return 1
        window = MainWindow(data_dir, auto_scan=not args.no_auto_scan)
    except (OSError, ValueError) as exc:
        QMessageBox.critical(
            None, "DarkSort 启动失败", f"无法初始化数据目录：\n{data_dir}\n\n{exc}"
        )
        return 1

    def report_exception(kind, value, traceback):
        logging.critical("Unhandled exception", exc_info=(kind, value, traceback))
        QMessageBox.critical(
            window, "操作失败", f"{value}\n\n详细信息已写入数据目录中的 darksort.log。"
        )

    sys.excepthook = report_exception
    window.show()
    logging.info("DarkSort started; data directory: %s", data_dir)
    return app.exec()
