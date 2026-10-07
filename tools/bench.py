"""性能与验收测量（M5）。

对照需求文档：
  §4.3  内存 < 300 MB、冷启动 < 3 s
  §10-16 连续练习 2 小时内存稳定（增长 < 10%）
  NFR-06 长时间练习内存不得持续增长

用法::

    python tools\\bench.py            # 全部测量
    python tools\\bench.py --quick    # 缩短模拟时长（约 1 分钟）
"""

from __future__ import annotations

import argparse
import gc
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from jitatrainer.core.audio.events import PitchEvent  # noqa: E402
from jitatrainer.core.audio.ring import RingBuffer  # noqa: E402
from jitatrainer.core.audio.analyzer import AnalyzerThread  # noqa: E402
from jitatrainer.core.theory.notes import midi_to_hz  # noqa: E402
from jitatrainer.data.db import Database  # noqa: E402
from jitatrainer.practice.base import SOURCE_NEW  # noqa: E402
from jitatrainer.practice.session import MODE_FREE, PracticeSession, SessionConfig  # noqa: E402
from jitatrainer.scheduling.scheduler import QuestionScheduler  # noqa: E402


def rss_mb() -> float | None:
    """当前进程常驻内存（MB）。测不到时返回 None（**不要**假装是 0）。"""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            # 关键：GetCurrentProcess 返回的是伪句柄，必须声明为指针类型，
            # 否则会被截断成 32 位，导致调用失败、读数恒为 0。
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            psapi.GetProcessMemoryInfo.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
                wintypes.DWORD,
            ]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(counters)
            ok = psapi.GetProcessMemoryInfo(
                kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
            )
            if not ok:
                return None
            return counters.WorkingSetSize / 1024 / 1024
        except Exception:  # noqa: BLE001
            return None
    try:
        import resource  # type: ignore[import-not-found]

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except Exception:  # noqa: BLE001
        return None


def bench_import_time() -> float:
    """冷启动的一半：导入全部模块的耗时（Qt 由 exe 自检覆盖）。"""
    started = time.perf_counter()
    import importlib

    for name in (
        "jitatrainer.ui.main_window",
        "jitatrainer.ui.pages.practice",
        "jitatrainer.ui.pages.stats",
        "jitatrainer.data.analytics",
        "jitatrainer.scheduling.scheduler",
    ):
        importlib.import_module(name)
    return (time.perf_counter() - started) * 1000


def bench_pitch_detection(seconds: float = 2.0, sample_rate: int = 48000) -> dict[str, float]:
    """音高检测吞吐：这是实时链路上最重的一环。"""
    import numpy as np

    from jitatrainer.core.audio.pitch_yin import PitchDetector, YinConfig

    window = YinConfig().window
    detector = PitchDetector(YinConfig(samplerate=sample_rate))
    phase = 2 * 3.141592653589793 * 196.0
    wave = np.sin(phase * np.arange(window) / sample_rate).astype(np.float32)

    count = int(sample_rate * seconds / window)
    timings = []
    for _ in range(count):
        begin = time.perf_counter()
        detector.detect(wave)
        timings.append((time.perf_counter() - begin) * 1000)
    return {
        "frames": count,
        "mean_ms": statistics.mean(timings),
        "p95_ms": sorted(timings)[int(len(timings) * 0.95)],
        "realtime_budget_ms": window / sample_rate * 1000,
    }


def bench_long_session(minutes: int = 120, *, audio: bool = True) -> dict[str, object]:
    """模拟长时间练习：内存是否持续增长（§10-16 / NFR-06）。

    时间被压缩：用真实的事件流跑满等效题量，而不是真的等 2 小时。
    """
    frames_per_second = 100  # 分析器 10ms 一帧
    total_frames = minutes * 60 * frames_per_second
    batch = 500

    # 注意：这台机器上 tempfile.mkdtemp() 建出的目录 ACL 异常（见开发记录里的环境约束），
    # 因此基准测试用工作区内的临时目录。
    workdir = ROOT / ".tmp" / "bench"
    workdir.mkdir(parents=True, exist_ok=True)
    for stale in workdir.glob("*.db*"):
        try:
            stale.unlink()
        except OSError:
            pass
    db = Database(workdir / "bench.db")
    db.initialize()
    with db.connect() as conn:
        profile_id = int(db.list_profiles(conn)[0]["id"])

    from jitatrainer.core.judge.pitch_class_judge import JudgeConfig
    from jitatrainer.practice.pitch_find.module import PitchFindModule
    import random

    def judge_factory(question, **_ignored):  # noqa: ANN001
        return JudgeConfig(
            target_pc=question.target_pc,
            stable_ms=200,
            grace_ms=2000,
            mode="grace",
            hop_ms=10.0,
        )

    scheduler = QuestionScheduler(
        level_id="L1",
        pitch_classes=list(range(12)),
        item_key_of=lambda pc: f"note={pc}|level=L1",
        rng=random.Random(4),
    )
    session = PracticeSession(
        PitchFindModule(random.Random(4)),
        SessionConfig(mode=MODE_FREE, level_id="L1"),
        judge_factory,
        profile_id=profile_id,
        scheduler=scheduler,
    )
    session.start()

    analyzer = None
    ring = None
    if audio:
        from jitatrainer.core.audio.analyzer import AnalyzerConfig, FrameAnalyzer

        ring = RingBuffer(capacity=48000 * 2)
        analyzer_config = AnalyzerConfig()
        collected: list[PitchEvent] = []
        analyzer = AnalyzerThread(
            ring,
            FrameAnalyzer(analyzer_config),
            collected.append,
            analyzer_config,
        )
        analyzer.start()

    samples: list[float] = []
    questions = 0
    gc.collect()
    baseline = rss_mb()
    if baseline is None:
        raise RuntimeError("无法读取进程内存，测量结果不可信；请不要据此下结论")
    started = time.perf_counter()

    for frame_index in range(total_frames):
        question = session.current_question
        if question is None:
            break
        hz = midi_to_hz(60 + question.target_pc)
        events = session.feed(
            PitchEvent(
                t=frame_index * 0.01,
                hz=hz,
                confidence=0.95,
                is_onset=(frame_index % 300 == 0),
            )
        )
        for event in events:
            if event.kind == "question":
                questions += 1
        if frame_index % batch == 0:
            reading = rss_mb()
            if reading is not None:
                samples.append(reading)

    elapsed = time.perf_counter() - started
    if analyzer is not None:
        analyzer.stop()
    gc.collect()
    final = rss_mb() or baseline

    # 只比较后半段（前半段有正常的缓存增长）
    tail = samples[len(samples) // 2 :] or [final]
    growth = (max(tail) - tail[0]) / tail[0] * 100 if tail[0] else 0.0

    return {
        "simulated_minutes": minutes,
        "frames": total_frames,
        "questions": questions,
        "wall_seconds": round(elapsed, 1),
        "baseline_mb": round(baseline, 1),
        "final_mb": round(final, 1),
        "peak_mb": round(max(samples or [final]), 1),
        "tail_growth_pct": round(growth, 2),
        "samples": len(samples),
        "measured": bool(samples),
    }


def bench_leak(minutes: int = 30) -> dict[str, object]:
    """用 tracemalloc 判断是否**真的泄漏**（RSS 会被分配器碎片干扰）。

    这是 NFR-06 的权威判据：
      - 存活对象数是否稳定
      - Python 层已分配内存是否随题量持续增长
    """
    import random
    import tracemalloc

    from jitatrainer.core.judge.pitch_class_judge import JudgeConfig
    from jitatrainer.practice.pitch_find.module import PitchFindModule
    from jitatrainer.scheduling.scheduler import QuestionScheduler

    workdir = ROOT / ".tmp" / "bench"
    workdir.mkdir(parents=True, exist_ok=True)
    db = Database(workdir / "leak.db")
    db.initialize()
    with db.connect() as conn:
        profile_id = int(db.list_profiles(conn)[0]["id"])

    def judge_factory(question, **_ignored):  # noqa: ANN001
        return JudgeConfig(
            target_pc=question.target_pc, stable_ms=200, grace_ms=2000, mode="grace", hop_ms=10.0
        )

    def build() -> PracticeSession:
        scheduler = QuestionScheduler(
            level_id="L1",
            pitch_classes=list(range(12)),
            item_key_of=lambda pc: f"note={pc}|level=L1",
            rng=random.Random(9),
        )
        session = PracticeSession(
            PitchFindModule(random.Random(9)),
            SessionConfig(mode=MODE_FREE, level_id="L1"),
            judge_factory,
            profile_id=profile_id,
            scheduler=scheduler,
        )
        session.start()
        return session

    def run(session: PracticeSession, frames: int, start: int) -> int:
        for frame in range(start, start + frames):
            question = session.current_question
            if question is None:
                break
            session.feed(
                PitchEvent(
                    t=frame * 0.01,
                    hz=midi_to_hz(60 + question.target_pc),
                    confidence=0.95,
                    is_onset=(frame % 300 == 0),
                )
            )
        return start + frames

    frames = minutes * 60 * 100
    session = build()
    cursor = run(session, 20_000, 0)  # 预热
    gc.collect()

    tracemalloc.start(10)
    before = tracemalloc.take_snapshot()
    objects_before = len(gc.get_objects())

    cursor = run(session, frames, cursor)
    gc.collect()
    after = tracemalloc.take_snapshot()
    objects_after = len(gc.get_objects())
    tracemalloc.stop()

    traced_bytes = sum(stat.size_diff for stat in after.compare_to(before, "lineno"))
    return {
        "frames": frames,
        "questions": session.stats.asked,
        "objects_before": objects_before,
        "objects_after": objects_after,
        "object_delta": objects_after - objects_before,
        "traced_kb": round(traced_bytes / 1024, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="JitaTrainer 性能测量")
    parser.add_argument("--quick", action="store_true", help="缩短模拟时长")
    parser.add_argument("--session-minutes", type=int, default=None)
    args = parser.parse_args()

    minutes = args.session_minutes or (10 if args.quick else 120)

    print("=== JitaTrainer 性能测量 ===")
    print(f"时间：{datetime.now(timezone.utc).astimezone():%Y-%m-%d %H:%M:%S}")
    print()

    imports = bench_import_time()
    print(f"[导入] 关键模块导入耗时：{imports:.0f} ms")

    detection = bench_pitch_detection()
    print(
        f"[检测] 音高检测帧耗时：均值 {detection['mean_ms']:.2f} ms、"
        f"P95 {detection['p95_ms']:.2f} ms（实时预算 {detection['realtime_budget_ms']:.1f} ms/帧）"
    )
    ratio = detection["mean_ms"] / detection["realtime_budget_ms"]
    print(f"        实时余量：占用 {ratio * 100:.1f}% 的单帧预算")

    print(f"\n[长练] 模拟 {minutes} 分钟练习（含音频分析线程）…")
    result = bench_long_session(minutes)
    print(f"        等效答题 {result['questions']} 题，实际耗时 {result['wall_seconds']} s")
    print(
        f"        常驻内存：{result['baseline_mb']} MB → {result['final_mb']} MB"
        f"（峰值 {result['peak_mb']} MB，采样 {result['samples']} 次）"
    )
    print(f"        后半段增长：{result['tail_growth_pct']}%（验收要求 < 10%）")
    print("        注意：压缩模拟会在几秒内造出上百万个短命事件对象，")
    print("              常驻内存的增长主要来自分配器碎片，不能单独作为泄漏判据。")

    print(f"\n[泄漏] tracemalloc 权威判据（模拟 {max(10, minutes // 4)} 分钟）…")
    leak = bench_leak(max(10, minutes // 4))
    print(
        f"        等效答题 {leak['questions']} 题后：存活对象 "
        f"{leak['objects_before']} → {leak['objects_after']}（差 {leak['object_delta']}）"
    )
    print(f"        Python 层已分配内存变化：{leak['traced_kb']} KB")

    if not result["measured"]:
        print("\n结论：**常驻内存测量无效**，不能据此判断通过与否")
        return 1

    leaked = leak["object_delta"] > 2000 or leak["traced_kb"] > 2048
    memory_ok = result["tail_growth_pct"] < 10
    budget_ok = result["peak_mb"] < 300
    print(f"        内存上限 {result['peak_mb']} MB < 300 MB：{'通过' if budget_ok else '未通过'}")
    print(f"        是否存在泄漏：{'是' if leaked else '否'}")

    verdict = "通过" if (memory_ok and budget_ok and not leaked) else "未通过"
    print(f"\n结论：长时间练习内存稳定性 {verdict}")
    return 0 if (memory_ok and budget_ok and not leaked) else 1


if __name__ == "__main__":
    raise SystemExit(main())
