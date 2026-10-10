"""应用入口。

主窗口在 ``ui/main_window.py``；这里只负责命令行参数、日志、自检与启动。
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import sys
from pathlib import Path

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


def run_practice_smoke(window) -> dict[str, object]:  # noqa: ANN001
    """练习屏冒烟测试：不接麦克风，用合成事件走完一局。

    覆盖 M2 的关键路径：构造练习屏 → 渲染六线谱 → 判定 → 会话小结。
    打包后的产物自检也会跑这一项。
    """
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QPixmap

    from .core.audio.events import PitchEvent
    from .core.theory.notes import midi_to_hz
    from .practice import registry
    from .practice.session import MODE_COUNT, SessionConfig
    from .ui.pages.practice import PracticePage

    module = registry.get_module("pitch_find")
    config = SessionConfig(mode=MODE_COUNT, target_count=3, level_id="L1")
    page = PracticePage(window.settings, window.tr, config, module, profile_id=window.profile_id)
    page.resize(960, 720)
    page.show()

    page._apply(page.session.start())  # noqa: SLF001 - 冒烟测试直接驱动
    first = page.session.current_question
    assert first is not None

    # 渲染六线谱，确保 paintEvent 不抛异常
    pixmap = QPixmap(QSize(960, 420))
    page.tab.render(pixmap)

    rendered: list[str] = []
    # 每题的事件流必须长于"宽容期 + 稳定时长"（默认 2s + 0.2s），否则判定不会触发
    frames_per_question = 340  # 3.4 秒
    for index in range(3):
        question = page.session.current_question
        if question is None:
            break
        hz = midi_to_hz(60 + question.target_pc)
        for frame in range(frames_per_question):
            events = page.session.feed(
                PitchEvent(
                    t=index * 4.0 + frame * 0.01,
                    hz=hz,
                    confidence=0.95,
                    is_onset=(frame == 0),
                )
            )
            for event in events:
                rendered.append(event.kind)
        page._apply([])  # noqa: SLF001

    summary = page.session.build_summary()
    page.leave()
    return {
        "ok": summary.asked == 3 and summary.correct_first == 3,
        "asked": summary.asked,
        "correct_first": summary.correct_first,
        "score": summary.score,
        "events": rendered[-4:],
        "tab_rendered": not pixmap.isNull(),
    }


def run_stats_smoke(window) -> dict[str, object]:  # noqa: ANN001
    """统计页冒烟：确认图表控件在打包环境里能正常绘制。"""
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QPixmap

    from .ui.pages.stats import StatsPage

    page = StatsPage(window.settings, window.tr, profile_id=window.profile_id)
    page.resize(1100, 760)
    page.refresh()  # 无数据也不能崩
    pixmap = QPixmap(QSize(1100, 760))
    page.render(pixmap)

    # 三种区间都要能切
    periods = []
    for index in range(page.period_combo.count()):
        page.period_combo.setCurrentIndex(index)
        periods.append(page._days)  # noqa: SLF001
    return {
        "ok": not pixmap.isNull(),
        "rendered": not pixmap.isNull(),
        "periods": periods,
        "weak_text": page.weak_label.text(),
    }


def run_audio_thread_smoke() -> dict[str, object]:
    """音频分析线程冒烟：构造 → 启动 → 检测 → 停止 → join。

    ``probe_input`` 只走采集层，不构造 ``AnalyzerThread``；而这个线程曾经有两个
    致命缺陷（dataclass 让 Thread 不可哈希 → 构造即崩；字段 `_stop` 遮蔽 Thread
    内部方法 → join 崩），合起来就是"插上麦克风一启动就崩"。
    因此这里必须真的把它跑一遍（用合成波形，不需要麦克风）。
    """
    import time

    import numpy as np

    from .core.audio.analyzer import AnalyzerConfig, AnalyzerThread, FrameAnalyzer
    from .core.audio.ring import RingBuffer

    sample_rate = 48000
    ring = RingBuffer(capacity=sample_rate * 2, channels=1)
    events: list = []
    thread = AnalyzerThread(ring, FrameAnalyzer(AnalyzerConfig()), events.append)
    thread.start()
    try:
        wave = np.sin(2 * np.pi * 196.0 * np.arange(sample_rate // 2) / sample_rate) * 0.3
        ring.write(wave.astype(np.float32))
        deadline = time.monotonic() + 3.0
        while not events and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        thread.stop()
        thread.join(timeout=2.0)

    detected = round(float(events[0].hz), 1) if events else None
    expected = 196.0
    ok = (
        not thread.is_alive()
        and detected is not None
        and abs(detected - expected) / expected < 0.02
    )
    return {
        "ok": ok,
        "events": len(events),
        "detected_hz": detected,
        "expected_hz": expected,
        "thread_exited": not thread.is_alive(),
    }


def run_selftest(tr: Translator, *, report: bool = True, audio_probe: bool = False) -> int:
    """离屏自检：验证 Qt、语言包、数据库、音频设备、练习屏。

    窗口模式产物没有控制台，因此结果同时写入 ``<程序目录>/logs/selftest.json``，
    构建脚本读取该文件判断产物是否真的可用。

    退出码：0 通过；3 音频后端不可用；4 Qt 启动失败；5 音频采集探测失败；6 练习屏异常。
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

    try:
        diag["practice_smoke"] = run_practice_smoke(window)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        diag["practice_smoke"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        diag["stats_smoke"] = run_stats_smoke(window)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        diag["stats_smoke"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        diag["audio_thread_smoke"] = run_audio_thread_smoke()
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        diag["audio_thread_smoke"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

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
    print(f"  {'practice_smoke':22} = {diag['practice_smoke']}")
    print(f"  {'stats_smoke':22} = {diag['stats_smoke']}")
    print(f"  {'audio_thread_smoke':22} = {diag['audio_thread_smoke']}")
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
        return _finish_selftest(3)
    if isinstance(probe, dict) and not probe.get("ok", True):
        print("自检失败：无法打开音频输入流")
        return _finish_selftest(5)
    smoke = diag["practice_smoke"]
    if isinstance(smoke, dict) and not smoke.get("ok", False):
        print(f"自检失败：练习屏冒烟未通过 {smoke}")
        return _finish_selftest(6)
    stats_smoke = diag.get("stats_smoke")
    if isinstance(stats_smoke, dict) and not stats_smoke.get("ok", False):
        print(f"自检失败：统计页冒烟未通过 {stats_smoke}")
        return _finish_selftest(7)
    audio_smoke = diag.get("audio_thread_smoke")
    if isinstance(audio_smoke, dict) and not audio_smoke.get("ok", False):
        print(f"自检失败：音频分析线程冒烟未通过 {audio_smoke}")
        return _finish_selftest(8)

    print("自检完成：OK")
    return _finish_selftest(0)


def _finish_selftest(code: int) -> int:
    """结束自检并**强制退出进程**。

    为什么不能只 return：这是给打包脚本用的一次性冒烟入口，而 PortAudio 在 Windows 上
    可能留下原生线程把进程吊住。实测出现过"自检打完了但进程还活着"，结果
    ``dist\\...\\data\\jitatrainer.db`` 被锁住、下一次打包清理旧产物时失败。

    自检不做任何需要优雅收尾的事（报告已写入、窗口已关闭），因此直接 ``os._exit``。
    正常 GUI 启动路径不受影响。
    """
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    os._exit(code)
    return code  # pragma: no cover - 到不了这里


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

    # 需求 FR-1000：每次启动做一次轻量备份
    _startup_backup()

    window = MainWindow(tr)
    window.show()
    if not args.no_wizard:
        window.ensure_first_run()
    return app.exec()


def _startup_backup() -> Path | None:
    """启动备份。失败不能挡住程序启动。"""
    try:
        return backup_database(reason="startup")
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("启动备份失败")
        return None


__all__ = ["main", "collect_diagnostics", "run_selftest"]
