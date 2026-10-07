"""环境自检：换机器/隔一段时间后回来，先跑这个确认一切就绪。

用法：

    python tools/doctor.py            # 只检查环境
    python tools/doctor.py --audio    # 额外打开麦克风采集 1.5 秒（验证采集链路）
    python tools/doctor.py --tests    # 额外跑一遍单元测试

检查项：Python 版本、依赖、.vendor、运行期目录可写性、本机临时目录陷阱、
git 状态、音频设备。
"""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for extra in (ROOT / ".vendor", ROOT / "src"):
    if extra.is_dir() and str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

OK = "✅"
WARN = "⚠️ "
BAD = "❌"


def check_python() -> bool:
    version = sys.version.split()[0]
    good = sys.version_info[:2] >= (3, 11)
    print(f"  {OK if good else BAD} Python {version} ({platform.machine()})")
    return good


def check_dependencies() -> bool:
    from jitatrainer import compat

    compat.install()

    results = {}
    for name in ("numpy", "PySide6", "sounddevice", "pytest", "PyInstaller"):
        try:
            module = __import__(name)
            results[name] = getattr(module, "__version__", "?")
        except Exception:  # noqa: BLE001
            results[name] = None

    all_good = True
    for name, version in results.items():
        if version is None:
            hint = "（.vendor 里应该有，见 README 的环境说明）" if name == "sounddevice" else ""
            print(f"  {BAD} {name:12} 缺失 {hint}")
            all_good = False
        else:
            print(f"  {OK} {name:12} {version}")
    return all_good


def check_vendor() -> bool:
    vendor = ROOT / ".vendor"
    if vendor.is_dir() and any(vendor.iterdir()):
        print(f"  {OK} .vendor 存在")
        return True
    print(f"  {WARN}.vendor 不存在或为空——sounddevice 需要它（见 README 的安装说明）")
    return True  # 非致命：系统 site-packages 里可能已有


def check_temp_quirk() -> bool:
    """本机的已知陷阱：0o700 目录不可写，mkdtemp 会失败（compat 已修补）。"""
    try:
        path = tempfile.mkdtemp(prefix="jita-doctor-")
        (Path(path) / "probe").write_text("ok", encoding="utf-8")
        Path(path, "probe").unlink()
        os.rmdir(path)
        print(f"  {OK} 临时目录可用（compat 修补生效或本机无此问题）")
        return True
    except OSError as exc:
        print(f"  {BAD} 临时目录不可用：{exc}")
        return False


def check_runtime_dirs() -> bool:
    from jitatrainer import paths

    try:
        paths.ensure_dirs()
        probe = paths.data_dir() / ".doctor-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as exc:  # noqa: BLE001
        print(f"  {BAD} 运行期目录不可写：{exc}")
        return False
    print(f"  {OK} 运行期目录可写：{paths.data_dir()}")
    return True


def check_git() -> bool:
    def run(*args: str) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        return (proc.stdout or proc.stderr or "").strip()

    branch = run("rev-parse", "--abbrev-ref", "HEAD")
    head = run("log", "-1", "--pretty=%h %s")
    dirty = run("status", "--porcelain")
    if not branch:
        print(f"  {WARN}不是 git 仓库或 git 不可用")
        return True

    print(f"  {OK} 分支 {branch}")
    print(f"     最近提交：{head}")
    if dirty:
        print(f"  {WARN}有未提交改动：\n      " + "\n      ".join(dirty.splitlines()[:6]))
    else:
        print(f"  {OK} 工作树干净")
    return True


def check_audio(probe: bool) -> bool:
    from jitatrainer.core.audio import device as device_mod

    if not device_mod.sounddevice_available():
        print(f"  {BAD} sounddevice 不可用：{device_mod.import_error()}")
        return False

    devices = device_mod.list_input_devices()
    default = device_mod.default_input_device()
    print(f"  {OK} 输入设备 {len(devices)} 个，默认：{default.name if default else '无'}")

    if not probe:
        print("     （加 --audio 可以真正打开麦克风采集 1.5 秒做验证）")
        return True

    from jitatrainer.core.audio.capture import probe_input

    try:
        stats = probe_input(device_index=None, samplerate=48000, seconds=1.5)
    except Exception as exc:  # noqa: BLE001
        print(f"  {BAD} 采集失败：{type(exc).__name__}: {exc}")
        return False
    print(f"  {OK} 采集正常：{stats.frames} 帧，峰值 {stats.peak_db:.1f} dB，RMS {stats.rms_db:.1f} dB")
    return True


def check_tests() -> bool:
    print("     正在运行单元测试 …")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-1:] or [""]
    good = proc.returncode == 0
    print(f"  {OK if good else BAD} 测试：{tail[0]}")
    return good


def main() -> int:
    parser = argparse.ArgumentParser(description="JitaTrainer 环境自检")
    parser.add_argument("--audio", action="store_true", help="真正打开麦克风采集一小段")
    parser.add_argument("--tests", action="store_true", help="额外运行单元测试")
    args = parser.parse_args()

    print("=" * 60)
    print("JitaTrainer 环境自检")
    print("=" * 60)

    results: list[tuple[str, bool]] = []
    print("\n[Python]")
    results.append(("python", check_python()))
    print("\n[依赖]")
    results.append(("deps", check_dependencies()))
    results.append(("vendor", check_vendor()))
    print("\n[本机环境]")
    results.append(("temp", check_temp_quirk()))
    results.append(("dirs", check_runtime_dirs()))
    print("\n[Git]")
    results.append(("git", check_git()))
    print("\n[音频]")
    results.append(("audio", check_audio(args.audio)))
    if args.tests:
        print("\n[测试]")
        results.append(("tests", check_tests()))

    failed = [name for name, ok in results if not ok]
    print("\n" + "=" * 60)
    if failed:
        print(f"结果：{len(failed)} 项异常 → {', '.join(failed)}")
        return 1
    print("结果：全部正常，可以开始工作。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
