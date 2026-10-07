"""音频采集：sounddevice InputStream 封装。

回调里只做一次拷贝入环形缓冲，绝不做任何分析——音频回调必须微秒级返回，
否则会丢帧（技术方案 §3.2）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from .device import _sounddevice  # noqa: PLC2701 - 复用延迟导入逻辑
from .gate import CaptureStats, NoiseFloorMeter, frame_rms_db, summarize_capture
from .ring import RingBuffer

DEFAULT_BLOCKSIZE = 480  # 10ms @48kHz


@dataclass(frozen=True, slots=True)
class CaptureConfig:
    device_index: int | None = None
    samplerate: int = 48000
    blocksize: int = DEFAULT_BLOCKSIZE
    channels: int = 1
    dtype: str = "float32"


class AudioCapture:
    """把麦克风数据写入环形缓冲。"""

    def __init__(
        self,
        ring: RingBuffer,
        config: CaptureConfig | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.ring = ring
        self.config = config or CaptureConfig()
        self.on_error = on_error
        self._stream = None
        self.overflow_count = 0

    @property
    def is_running(self) -> bool:
        return self._stream is not None

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        if status:
            self.overflow_count += 1
            if self.on_error is not None:
                try:
                    self.on_error(str(status))
                except Exception:  # noqa: BLE001
                    pass
        self.ring.write(indata)

    def start(self) -> None:
        if self.is_running:
            return
        sd = _sounddevice()
        if sd is None:
            raise RuntimeError("sounddevice 不可用，无法采集音频")
        stream = sd.InputStream(
            device=self.config.device_index,
            samplerate=self.config.samplerate,
            blocksize=self.config.blocksize,
            channels=self.config.channels,
            dtype=self.config.dtype,
            callback=self._callback,
        )
        stream.start()
        self._stream = stream

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass


def probe_input(
    device_index: int | None = None,
    samplerate: int = 48000,
    seconds: float = 1.5,
    blocksize: int = DEFAULT_BLOCKSIZE,
    gate_db: float = -50.0,
) -> CaptureStats:
    """打开输入流采集一小段并返回统计结果（阻塞式，供自检使用）。

    这是**唯一**能证明"打包产物真的能采集声音"的手段：设备枚举只调用
    PortAudio 的查询接口，而这里真正打开了数据流并读到了样本。
    """
    sd = _sounddevice()
    if sd is None:
        raise RuntimeError("sounddevice 不可用")

    levels: list[float] = []
    total_frames = 0
    with sd.InputStream(
        device=device_index,
        samplerate=samplerate,
        blocksize=blocksize,
        channels=1,
        dtype="float32",
    ) as stream:
        needed = int(seconds * samplerate / blocksize)
        for _ in range(max(1, needed)):
            block, _overflowed = stream.read(blocksize)
            data = np.asarray(block)[:, 0]
            total_frames += len(data)
            levels.append(frame_rms_db(data))

    stats = summarize_capture(levels, gate_db)
    return CaptureStats(
        frames=total_frames,
        peak_db=stats.peak_db,
        rms_db=stats.rms_db,
        clipped=stats.clipped,
        gate_db=gate_db,
    )


def measure_noise_floor(
    device_index: int | None = None,
    samplerate: int = 48000,
    seconds: float = 3.0,
    blocksize: int = DEFAULT_BLOCKSIZE,
) -> NoiseFloorMeter:
    """测量环境噪声地板（阻塞式）。"""
    sd = _sounddevice()
    if sd is None:
        raise RuntimeError("sounddevice 不可用")

    meter = NoiseFloorMeter()
    with sd.InputStream(
        device=device_index,
        samplerate=samplerate,
        blocksize=blocksize,
        channels=1,
        dtype="float32",
    ) as stream:
        needed = int(seconds * samplerate / blocksize)
        for _ in range(max(1, needed)):
            block, _ = stream.read(blocksize)
            meter.add(np.asarray(block)[:, 0])
    return meter
