"""环形缓冲：音频回调（生产者）与分析线程（消费者）之间的无锁交换区。

音频回调必须尽快返回，所以回调里只做一次拷贝；分析线程按帧移取固定长度的窗口。
"""

from __future__ import annotations

import threading

import numpy as np


class RingBuffer:
    """单生产者、单消费者的环形缓冲（float32）。"""

    def __init__(self, capacity: int, channels: int = 1) -> None:
        if capacity <= 0:
            raise ValueError("容量必须为正")
        self._capacity = int(capacity)
        self._channels = int(channels)
        self._data = np.zeros((self._capacity, self._channels), dtype=np.float32)
        self._write = 0
        self._total = 0
        self._lock = threading.Lock()
        self.overrun_count = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def total_written(self) -> int:
        return self._total

    @property
    def available(self) -> int:
        return min(self._total, self._capacity)

    def write(self, block: np.ndarray) -> None:
        """写入一块音频（形状 (frames,) 或 (frames, channels)）。"""
        data = np.asarray(block, dtype=np.float32)
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        frames = data.shape[0]
        if frames == 0:
            return
        if frames > self._capacity:
            data = data[-self._capacity :]
            frames = self._capacity
            self.overrun_count += 1

        with self._lock:
            end = self._write + frames
            if end <= self._capacity:
                self._data[self._write : end] = data
            else:
                split = self._capacity - self._write
                self._data[self._write :] = data[:split]
                self._data[: end - self._capacity] = data[split:]
            self._write = end % self._capacity
            self._total += frames

    def read_latest(self, frames: int) -> np.ndarray | None:
        """读取最近 ``frames`` 帧的副本；数据不足时返回 None。"""
        if frames <= 0 or frames > self._capacity:
            raise ValueError("请求长度必须为正且不超过容量")
        with self._lock:
            if self._total < frames:
                return None
            start = (self._write - frames) % self._capacity
            if start + frames <= self._capacity:
                return self._data[start : start + frames].copy()
            split = self._capacity - start
            out = np.empty((frames, self._channels), dtype=np.float32)
            out[:split] = self._data[start:]
            out[split:] = self._data[: frames - split]
            return out

    def clear(self) -> None:
        with self._lock:
            self._data.fill(0.0)
            self._write = 0
            self._total = 0
