"""调音器页面。

复用的东西：``PitchMeter``（音高表）、``AudioSession``（采集+分析）、设置里的
设备与门限。M2 的练习屏会用同一套控件。
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.theory.notes import midi_name
from ...core.theory.tuning import STANDARD, Tuning
from ...data.settings import Settings
from ..audio_bridge import build_session
from ..widgets.pitch_meter import PitchMeter

POLL_MS = 40
AUTO_INDEX = 0


class TunerPage(QWidget):
    """标准调弦调音器。"""

    def __init__(self, settings: Settings, tr, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.tr = tr
        self.tuning: Tuning = STANDARD
        self.session = None
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)

        self._build_ui()

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
            return

        latest = voiced[-1]
        if self._selected_string() is None:
            string_no, _delta = self.tuning.nearest_open_string(latest.midi)
            target_midi = self.tuning.string_target_midi(string_no)
            self.meter.set_target(
                target_midi,
                f"{self.tr('tuner.string', n=string_no)} · {midi_name(target_midi)}",
                self.settings.get_float("tolerance_cents", 25.0),
            )
        self.meter.set_pitch(latest.hz, latest.confidence)

    # ------------------------------------------------------------------ 生命周期
    def leave(self) -> None:
        """离开页面时停止采集（避免占用麦克风）。"""
        if self.session is not None:
            self.stop()
