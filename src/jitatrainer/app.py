"""应用入口。

主窗口在 ``ui/main_window.py``；这里只负责命令行参数、日志、自检与启动。
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
from .i18n import Translator


def _configure_logging() -> None:
    paths.ensure_dirs()
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(paths.logs_dir() / "jitatrainer.log", encoding="utf-8")],
    )


def collect_diagnostics(tr: Translator, *, audio_probe: bool = False) -> dict[str, object]:
    """收集自检信息。``audio_probe=True`` 时会真正打开输入流采集一小段。"""
    db = Database()
    version = db.initialize()
    backup = backup_database()

    devices = device_mod.list_input_devices()
    data: dict[str, object] = {
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
        "input_device_count": len(devices),
        "input_devices": [d.name for d in devices[:20]],
        "levels": [
            {"id": lv.id, "name": lv.name_zh if tr.language == "zh_CN" else lv.name_en, "max_fret": lv.max_fret}
            for lv in levels_mod.LEVELS
        ],
    }

    if audio_probe and data["sounddevice_available"]:
        from .core.audio.capture import probe_input

        try:
            stats = probe_input(device_index=None, samplerate=48000, seconds=1.5)
            data["audio_probe"] = {
                "ok": True,
                "frames": stats.frames,
                "peak_db": round(stats.peak_db, 1),
                "rms_db": round(stats.rms_db, 1),
                "clipped": stats.clipped,
            }
        except Exception as exc:  # noqa: BLE001
            data["audio_probe"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return data


def run_selftest(tr: Translator, *, report: bool = True, audio_probe: bool = False) -> int:
    """离屏自检：验证 Qt、语言包、数据库、音频设备枚举与采集。

    窗口模式产物没有控制台，因此结果同时写入 ``<程序目录>/logs/selftest.json``，
    构建脚本读取该文件判断产物是否真的可用。

    退出码：0 通过；3 音频后端不可用；4 Qt 启动失败；5 音频采集探测失败。
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import json
    import traceback

    from PySide6.QtWidgets import QApplication

    from .ui.main_window import MainWindow

    try:
        app = QApplication.instance() or QApplication(sys.argv[:1])
        window = MainWindow(tr)
        window.show()
        app.processEvents()
    except Exception:
        traceback.print_exc()
        return 4

    diag = collect_diagnostics(tr, audio_probe=audio_probe)

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
        "input_device_count",
    ):
        print(f"  {key:22} = {diag[key]}")
    if diag["sounddevice_error"]:
        print(f"  {'sounddevice_error':22} = {diag['sounddevice_error']}")
    for name in diag["input_devices"][:5]:  # type: ignore[union-attr]
        print(f"      - {name}")
    probe = diag.get("audio_probe")
    if isinstance(probe, dict):
        print(f"  {'audio_probe':22} = {probe}")
    for level in diag["levels"]:  # type: ignore[union-attr]
        print(f"  级别 {level['id']}: {level['name']}（0-{level['max_fret']} 品）")

    if report:
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
    if isinstance(probe, dict) and not probe.get("ok", True):
        print("自检失败：无法打开音频输入流")
        return 5

    print("自检完成：OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="JitaTrainer", description="JitaTrainer 吉他训练器")
    parser.add_argument("--selftest", action="store_true", help="离屏自检后退出（用于打包冒烟测试）")
    parser.add_argument("--audio-probe", action="store_true", help="自检时真正打开麦克风采集一小段")
    parser.add_argument("--lang", choices=("zh_CN", "en_US"), default=None, help="界面语言")
    parser.add_argument("--no-wizard", action="store_true", help="启动时不弹出首次运行向导")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    _configure_logging()
    paths.ensure_dirs()

    tr = Translator(args.lang or "zh_CN")
    if args.selftest:
        return run_selftest(tr, audio_probe=args.audio_probe)

    from PySide6.QtWidgets import QApplication

    from .ui.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(__app_name__)
    app.setApplicationVersion(__version__)

    window = MainWindow(tr)
    window.show()
    if not args.no_wizard:
        window.ensure_first_run()
    return app.exec()


__all__ = ["main", "collect_diagnostics", "run_selftest"]
