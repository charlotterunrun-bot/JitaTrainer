"""调音器页面。

复用的东西：``PitchMeter``（音高表）、``AudioSession``（采集+分析）、设置里的
设备与门限。M2 的练习屏会用同一套控件。
"""

from __future__ import annotations

from collections import Counter, deque

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.instrument import resolve_hz, tuning_from_profile
from ...core.theory.notes import midi_name, midi_to_hz
from ...core.theory.tuning import STANDARD, Tuning
from ...data.settings import Settings
from ..audio_bridge import active_profile, build_session
from ..widgets.pitch_meter import PitchMeter

POLL_MS = 40
AUTO_INDEX = 0
#: 显示平滑窗口的帧数。低音弦的基频可能弱于高次谐波，单帧会在八度之间抖动，
#: 用最近若干帧的众数决定显示音名（实测低音弦约一半的帧能锁定真实基频）。
SMOOTHING_FRAMES = 24


class TunerPage(QWidget):
    """标准调弦调音器。"""

    def __init__(self, settings: Settings, tr, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.tr = tr
        #: 目标音高来自乐器配置档案（换调弦/换琴后调音器跟着变，不需要改代码）
        self.profile = active_profile(settings)
        self.tuning: Tuning = tuning_from_profile(self.profile)
        self.session = None
        self._recent: deque[tuple[int, float]] = deque(maxlen=SMOOTHING_FRAMES)
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)

        self._build_ui()

    def reload_profile(self) -> None:
        """重新读取乐器配置档案（在「乐器」菜单里换了档案后调用）。"""
        self.profile = active_profile(self.settings)
        self.tuning = tuning_from_profile(self.profile)
        self.target_combo.clear()
        for string_no in range(6, 0, -1):
            midi = self.tuning.string_target_midi(string_no)
            label = f"{self.tr('tuner.string', n=string_no)} · {midi_name(midi)}"
            self.target_combo.addItem(label, string_no)
        self.target_combo.insertItem(0, self.tr("tuner.auto"), None)
        self.target_combo.setCurrentIndex(0)
        self._on_target_changed()

    # ------------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 22, 28, 22)
        layout.setSpacing(14)

        title = QLabel(self.tr("tuner.title"))
        title.setObjectName("sectionTitle")

        row = QHBoxLayout()
        row.addWidget(QLabel(self.tr("tuner.target")))
        self.target_combo = QComboBox()
        self.target_combo.addItem(self.tr("tuner.auto"), AUTO_INDEX)
        for string_no in range(6, 0, -1):
            midi = self.tuning.string_target_midi(string_no)
            label = f"{self.tr('tuner.string', n=string_no)} · {midi_name(midi)}"
            self.target_combo.addItem(label, string_no)
        self.target_combo.currentIndexChanged.connect(self._on_target_changed)
        row.addWidget(self.target_combo, 1)

        self.toggle_button = QPushButton(self.tr("common.start"))
        self.toggle_button.setObjectName("primary")
        self.toggle_button.clicked.connect(self.toggle)
        row.addWidget(self.toggle_button)
        layout.addWidget(title)
        layout.addLayout(row)

        self.meter = PitchMeter()
        layout.addWidget(self.meter, 1)

        self.status = QLabel(self.tr("practice.waiting"))
        self.status.setObjectName("dim")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self._on_target_changed()

    # ------------------------------------------------------------------ 逻辑
    def _selected_string(self) -> int | None:
        data = self.target_combo.currentData()
        return data if isinstance(data, int) and data != AUTO_INDEX else None

    def _on_target_changed(self) -> None:
        string_no = self._selected_string()
        tolerance = self.settings.get_float("tolerance_cents", 25.0)
        if string_no is None:
            self.meter.set_target(None, self.tr("tuner.auto"), tolerance)
        else:
            midi = self.tuning.string_target_midi(string_no)
            self.meter.set_target(midi, f"{self.tr('tuner.string', n=string_no)} · {midi_name(midi)}", tolerance)

    def toggle(self) -> None:
        if self.session is not None:
            self.stop()
        else:
            self.start()

    def start(self) -> None:
        if self.session is not None:
            return
        try:
            self.session = build_session(self.settings)
            self.session.start()
        except Exception as exc:  # noqa: BLE001 - 设备问题要提示而不是崩溃
            self.session = None
            self.status.setText(self.tr("error.audio_unavailable", reason=str(exc)))
            return

        device = self.target_combo.count() and self.settings.get("input_device_index", "")
        self.status.setText(self.tr("practice.listening") + (f"  (设备 {device})" if device else ""))
        self.toggle_button.setText(self.tr("common.stop"))
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        if self.session is not None:
            self.session.stop()
            self.session = None
        self._recent.clear()
        self.meter.clear()
        self.toggle_button.setText(self.tr("common.start"))
        self.status.setText(self.tr("practice.waiting"))

    def _poll(self) -> None:
        if self.session is None:
            return
        events = self.session.poll()
        voiced = [e for e in events if e.valid]
        if not voiced:
            if events:
                self.meter.set_pitch(None)
                self._recent.clear()
            return

        for event in voiced:
            pc = event.pitch_class
            if pc is not None:
                self._recent.append((pc, event.midi))

        latest = voiced[-1]
        if self._selected_string() is None:
            string_no, _delta = self.tuning.nearest_open_string(latest.midi)
            target_midi = self.tuning.string_target_midi(string_no)
            self.meter.set_target(
                target_midi,
                f"{self.tr('tuner.string', n=string_no)} · {midi_name(target_midi)}",
                self.settings.get_float("tolerance_cents", 25.0),
            )

        display_hz, confidence = self._smoothed_pitch(latest)
        self.meter.set_pitch(display_hz, confidence)

    def _smoothed_pitch(self, latest) -> tuple[float, float]:  # noqa: ANN001
        """用最近若干帧的众数决定显示音名，减少八度跳动。

        选定某根弦时，还用**乐器档案里的预期音高**消解八度：
        若读数与预期弦呈八度/十二度关系，按预期弦显示，避免"明明在调 1 弦，
        却因为锁到二次谐波而显示成高八度"这种显示歧义。
        """
        if not self._recent:
            return latest.hz, latest.confidence

        string_no = self._selected_string()
        if string_no is not None:
            expected = self.tuning.string_target_midi(string_no)
            resolution = resolve_hz(latest.hz, self.profile, expect_midi=expected)
            if resolution.adjusted_from is not None or resolution.status in ("exact", "off_tune"):
                return resolution.hz, latest.confidence

        counts = Counter(pc for pc, _midi in self._recent)
        mode_pc, _count = counts.most_common(1)[0]
        midis = sorted(midi for pc, midi in self._recent if pc == mode_pc)
        median_midi = midis[len(midis) // 2]
        return midi_to_hz(median_midi), latest.confidence

    # ------------------------------------------------------------------ 生命周期
    def leave(self) -> None:
        """离开页面时停止采集（避免占用麦克风）。"""
        if self.session is not None:
            self.stop()
