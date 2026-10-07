"""应用入口与主窗口。

M1 阶段的主窗口只承担三件事：证明打包产物能起来、能读语言包、
能枚举音频设备。正式的首页/练习屏在 M2 实现。
"""

from __future__ import annotations

import argparse
import os
import platform
import sys

from . import __app_name__, __version__
from . import paths
from .core.audio import device as device_mod
from .core.theory import levels as levels_mod
from .data.db import Database, backup_database
from .i18n import LANGUAGE_LABELS, Translator


def _configure_logging() -> None:
    paths.ensure_dirs()
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(paths.logs_dir() / "jitatrainer.log", encoding="utf-8")],
    )


def collect_diagnostics(tr: Translator) -> dict[str, object]:
    """收集自检信息（不启动音频流，不弹窗）。"""
    db = Database()
    version = db.initialize()
    backup = backup_database()

    return {
        "app": __app_name__,
        "version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "frozen": paths.is_frozen(),
        "app_root": str(paths.app_root()),
        "data_dir": str(paths.data_dir()),
        "db_path": str(paths.db_path()),
        "schema_version": version,
        "backup": str(backup) if backup else None,
        "language": tr.language,
        "title": tr("app.title"),
        "sounddevice_available": device_mod.sounddevice_available(),
        "sounddevice_error": device_mod.import_error(),
        "input_devices": [d.name for d in device_mod.list_input_devices()],
        "levels": [
            {"id": lv.id, "name": lv.name_zh if tr.language == "zh_CN" else lv.name_en, "max_fret": lv.max_fret}
            for lv in levels_mod.LEVELS
        ],
    }


def build_main_window(tr: Translator):
    """构造 M1 版主窗口（后续里程碑会替换为完整首页）。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel, QMainWindow, QVBoxLayout, QWidget

    window = QMainWindow()
    window.setWindowTitle(f"{tr('app.title')} · {tr('app.version')} {__version__}")
    window.resize(880, 560)

    central = QWidget()
    layout = QVBoxLayout(central)
    layout.setContentsMargins(32, 28, 32, 28)
    layout.setSpacing(14)

    title = QLabel(tr("app.title"))
    title.setObjectName("title")
    title.setAlignment(Qt.AlignmentFlag.AlignLeft)
    title.setStyleSheet("font-size: 30px; font-weight: 600;")

    subtitle = QLabel(tr("app.subtitle"))
    subtitle.setStyleSheet("font-size: 15px; color: #9aa4b2;")

    diag = collect_diagnostics(tr)
    devices = diag["input_devices"]
    device_text = (
        tr("settings.input_device") + "：" + (devices[0] if devices else tr("error.no_input_device"))
    )
    info_lines = [
        f"{tr('app.version')}: {__version__}",
        f"Python: {diag['python']}",
        device_text,
        f"schema: v{diag['schema_version']}",
        f"data: {diag['data_dir']}",
    ]
    info = QLabel("\n".join(line for line in info_lines if line))
    info.setStyleSheet("font-size: 14px; line-height: 150%; color: #d7dde5;")

    hint = QLabel("M1 地基阶段：主界面将在 M2 里程碑替换为完整首页。")
    hint.setStyleSheet("font-size: 13px; color: #6f7b8a;")

    layout.addWidget(title)
    layout.addWidget(subtitle)
    layout.addSpacing(10)
    layout.addWidget(info)
    layout.addStretch(1)
    layout.addWidget(hint)

    window.setCentralWidget(central)
    window.setStyleSheet("background-color: #14181d; color: #e8edf3;")
    return window


def run_selftest(tr: Translator) -> int:
    """离屏自检：验证 Qt、语言包、数据库、音频设备枚举是否可用。

    打包冒烟测试直接调用它。因为窗口模式产物没有控制台，自检结果同时写入
    ``<程序目录>/logs/selftest.json``，构建脚本读取该文件判断产物是否真的可用。

    退出码：0 = 通过；3 = 音频后端不可用（打包事故）；4 = Qt 启动失败。
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import json
    import traceback

    from PySide6.QtWidgets import QApplication

    try:
        app = QApplication.instance() or QApplication(sys.argv[:1])
        window = build_main_window(tr)
        window.show()
        app.processEvents()
    except Exception:
        traceback.print_exc()
        return 4

    diag = collect_diagnostics(tr)
    diag["input_device_count"] = len(diag["input_devices"])
    diag["input_devices"] = diag["input_devices"][:20]

    print("=== JitaTrainer 自检 ===")
    for key in (
        "app",
        "version",
        "python",
        "platform",
        "frozen",
        "app_root",
        "data_dir",
        "db_path",
        "schema_version",
        "language",
        "title",
        "sounddevice_available",
    ):
        print(f"  {key:22} = {diag[key]}")
    if diag["sounddevice_error"]:
        print(f"  {'sounddevice_error':22} = {diag['sounddevice_error']}")
    print(f"  {'input_device_count':22} = {diag['input_device_count']}")
    for name in diag["input_devices"][:5]:
        print(f"      - {name}")
    for level in diag["levels"]:
        print(f"  级别 {level['id']}: {level['name']}（0-{level['max_fret']} 品）")

    report_path = paths.logs_dir() / "selftest.json"
    try:
        report_path.write_text(json.dumps(diag, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"自检报告已写入：{report_path}")
    except OSError as exc:
        print(f"无法写入自检报告：{exc}")

    window.close()

    if not diag["sounddevice_available"]:
        print("自检失败：音频后端不可用（sounddevice 未能导入）")
        return 3

    print("自检完成：OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="JitaTrainer", description="JitaTrainer 吉他训练器")
    parser.add_argument("--selftest", action="store_true", help="离屏自检后退出（用于打包冒烟测试）")
    parser.add_argument("--lang", choices=("zh_CN", "en_US"), default=None, help="界面语言")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    _configure_logging()
    paths.ensure_dirs()

    tr = Translator(args.lang or "zh_CN")
    if args.selftest:
        return run_selftest(tr)

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(__app_name__)
    app.setApplicationVersion(__version__)

    window = build_main_window(tr)
    window.show()
    return app.exec()


__all__ = ["main", "build_main_window", "collect_diagnostics", "run_selftest", "LANGUAGE_LABELS"]
