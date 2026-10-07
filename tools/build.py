"""PyInstaller 打包脚本。

用法：
    python tools/build.py               # 单文件夹绿色版（默认）
    python tools/build.py --clean       # 先清理再打包
    python tools/build.py --no-selftest # 跳过打包后的自检

打包后会**自动运行产物的自检**（--selftest），用退出码判断产物是否可用。
这是 M1 阶段验证风险最高项（sounddevice + PortAudio 打包）的手段。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
VENDOR = ROOT / ".vendor"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
APP_NAME = "JitaTrainer"

# 排除用不到的 Qt 模块，控制体积（技术方案 §12）
EXCLUDES = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtQuick",
    "PySide6.QtQml",
    "PySide6.QtQuick3D",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtDesigner",
    "PySide6.QtTest",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtSerialPort",
    "PySide6.QtSql",
    "PySide6.QtRemoteObjects",
    "PySide6.QtSensors",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "tkinter",
    "matplotlib",
    "scipy",
    "IPython",
    "pytest",
]


def _python() -> str:
    return sys.executable


def clean() -> None:
    for path in (DIST / APP_NAME, BUILD / APP_NAME):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    print("已清理旧产物")


def build(name: str = APP_NAME, *, console: bool = False) -> Path:
    args = [
        _python(),
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name",
        name,
        "--paths",
        str(SRC),
        "--paths",
        str(VENDOR),
        "--add-data",
        f"{SRC / 'jitatrainer' / 'i18n'}{os.pathsep}i18n",
        "--collect-all",
        "sounddevice",
        "--log-level",
        "WARN",
    ]
    for module in EXCLUDES:
        args += ["--exclude-module", module]
    args.append("--console" if console else "--windowed")
    args.append(str(ROOT / "run.py"))

    print("开始打包 …")
    started = time.time()
    proc = subprocess.run(args, cwd=ROOT)
    if proc.returncode != 0:
        raise SystemExit(f"PyInstaller 失败，退出码 {proc.returncode}")
    print(f"打包完成，用时 {time.time() - started:.1f}s")

    target = DIST / name
    for extra in ("THIRD_PARTY_LICENSES.txt", "README.md"):
        source = ROOT / extra
        if source.is_file():
            shutil.copy2(source, target / extra)
    return target


def directory_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return total


def selftest(target: Path, *, probe: bool = False) -> int:
    """运行产物自检，并读取它写出的报告文件（窗口模式产物没有控制台输出）。"""
    exe = target / f"{APP_NAME}.exe"
    if not exe.is_file():
        print(f"未找到可执行文件：{exe}")
        return 2

    report = target / "logs" / "selftest.json"
    if report.exists():
        report.unlink()

    argv = [str(exe), "--selftest"]
    if probe:
        argv.append("--audio-probe")
    print(f"\n运行产物自检：{' '.join(argv)}")
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    proc = subprocess.run(argv, cwd=target, env=env, timeout=300)
    print(f"退出码：{proc.returncode}")

    if report.is_file():
        import json

        data = json.loads(report.read_text(encoding="utf-8"))
        print("自检报告：")
        for key in (
            "version",
            "frozen",
            "app_root",
            "data_dir",
            "schema_version",
            "language",
            "sounddevice_available",
            "sounddevice_error",
            "input_device_count",
        ):
            print(f"  {key:22} = {data.get(key)}")
        if data.get("audio_probe"):
            print(f"  {'audio_probe':22} = {data['audio_probe']}")
        if not data.get("sounddevice_available"):
            print("⚠ 音频后端不可用：打包产物缺少 sounddevice / PortAudio")
    else:
        print(f"⚠ 未找到自检报告 {report}（产物可能未能启动）")

    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 JitaTrainer 绿色版")
    parser.add_argument("--clean", action="store_true", help="打包前清理旧产物")
    parser.add_argument("--console", action="store_true", help="保留控制台窗口（排查用）")
    parser.add_argument("--no-selftest", action="store_true", help="跳过产物自检")
    parser.add_argument("--probe", action="store_true", help="产物自检时真正打开麦克风采集一小段")
    parser.add_argument("--name", default=APP_NAME)
    args = parser.parse_args()

    if args.clean:
        clean()

    target = build(args.name, console=args.console)

    size_mb = directory_size(target) / 1024 / 1024
    print(f"产物目录：{target}")
    print(f"体积：{size_mb:.1f} MB")

    if args.no_selftest:
        return 0

    code = selftest(target, probe=args.probe)
    print(f"产物自检退出码：{code}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
