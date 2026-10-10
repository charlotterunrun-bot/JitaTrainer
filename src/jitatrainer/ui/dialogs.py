"""会话设置与会话小结对话框。"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..core.theory.levels import LEVELS, get_level
from ..practice.session import (
    COUNT_PRESETS,
    DURATION_PRESETS,
    MODE_COUNT,
    MODE_DURATION,
    MODE_FREE,
    TIMEOUT_AUTO,
    TIMEOUT_MANUAL,
    SessionConfig,
    SessionSummary,
)


class SessionSetupDialog(QDialog):
    """开始练习前的设置：难度、模式、时长/题量。"""

    def __init__(self, base: SessionConfig, tr, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tr = tr
        self.setWindowTitle(tr("practice.setup_title"))
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(10)

        self.level_combo = QComboBox()
        for level in LEVELS:
            name = level.name_zh if tr.language == "zh_CN" else level.name_en
            self.level_combo.addItem(f"{name}（0–{level.max_fret} 品）", level.id)
        index = self.level_combo.findData(base.level_id)
        self.level_combo.setCurrentIndex(max(0, index))
        form.addRow(tr("practice.level"), self.level_combo)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem(tr("practice.mode.count"), MODE_COUNT)
        self.mode_combo.addItem(tr("practice.mode.duration"), MODE_DURATION)
        self.mode_combo.addItem(tr("practice.mode.free"), MODE_FREE)
        mode_index = self.mode_combo.findData(base.mode)
        self.mode_combo.setCurrentIndex(max(0, mode_index))
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        form.addRow(tr("practice.mode"), self.mode_combo)

        self.target_combo = QComboBox()
        form.addRow(tr("practice.target"), self.target_combo)

        self.accidental_check = QCheckBox(tr("settings.include_accidentals"))
        self.accidental_check.setChecked(base.include_accidentals)
        form.addRow("", self.accidental_check)

        self.note_hint_check = QCheckBox(tr("settings.show_note_name"))
        self.note_hint_check.setChecked(base.show_note_name)
        form.addRow("", self.note_hint_check)

        # 超时处理：自动换题（显示倒计时）或人工推进
        self.timeout_combo = QComboBox()
        self.timeout_combo.addItem(tr("practice.timeout.auto"), TIMEOUT_AUTO)
        self.timeout_combo.addItem(tr("practice.timeout.manual"), TIMEOUT_MANUAL)
        timeout_index = self.timeout_combo.findData(base.timeout_mode)
        self.timeout_combo.setCurrentIndex(max(0, timeout_index))
        self.timeout_combo.currentIndexChanged.connect(self._on_timeout_changed)
        form.addRow(tr("practice.timeout"), self.timeout_combo)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(2, 60)
        self.timeout_spin.setSuffix(" " + tr("settings.seconds"))
        self.timeout_spin.setValue(max(2, int(round(base.timeout_seconds))))
        form.addRow(tr("practice.timeout_seconds"), self.timeout_spin)
        self._on_timeout_changed()

        self.scoring_check = QCheckBox(tr("settings.scoring"))
        self.scoring_check.setChecked(base.scoring_enabled)
        form.addRow("", self.scoring_check)

        layout.addLayout(form)

        hint = QLabel(tr("practice.setup_hint"))
        hint.setObjectName("faint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("practice.start"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("common.cancel"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._base = base
        self._fill_targets(base)

    # ------------------------------------------------------------------ 内部
    def _fill_targets(self, base: SessionConfig) -> None:
        mode = self.mode_combo.currentData()
        self.target_combo.clear()
        if mode == MODE_DURATION:
            for minutes in DURATION_PRESETS:
                self.target_combo.addItem(f"{minutes} 分钟", minutes)
            index = self.target_combo.findData(base.target_minutes)
        elif mode == MODE_COUNT:
            for count in COUNT_PRESETS:
                self.target_combo.addItem(f"{count} 题", count)
            index = self.target_combo.findData(base.target_count)
        else:
            self.target_combo.addItem("不限", 0)
            index = 0
        self.target_combo.setCurrentIndex(max(0, index))
        self.target_combo.setEnabled(mode != MODE_FREE)

    def _on_mode_changed(self) -> None:
        self._fill_targets(self._base)

    def _on_timeout_changed(self) -> None:
        """只有自动模式需要设置超时秒数。"""
        auto = self.timeout_combo.currentData() == TIMEOUT_AUTO
        self.timeout_spin.setEnabled(auto)

    # ------------------------------------------------------------------ 结果
    def config(self) -> SessionConfig:
        mode = self.mode_combo.currentData()
        value = self.target_combo.currentData() or 0
        return SessionConfig(
            module_id=self._base.module_id,
            mode=mode,
            target_minutes=value if mode == MODE_DURATION else self._base.target_minutes,
            target_count=value if mode == MODE_COUNT else self._base.target_count,
            level_id=self.level_combo.currentData(),
            include_accidentals=self.accidental_check.isChecked(),
            scoring_enabled=self.scoring_check.isChecked(),
            show_note_name=self.note_hint_check.isChecked(),
            timeout_mode=self.timeout_combo.currentData(),
            timeout_seconds=float(self.timeout_spin.value()),
        )


class SessionSummaryDialog(QDialog):
    """会话小结（需求 FR-820）。"""

    def __init__(self, summary: SessionSummary, tr, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tr = tr
        self.setWindowTitle(tr("stats.session_summary"))
        self.setMinimumWidth(420)
        self.retry_requested = False

        layout = QVBoxLayout(self)
        heading = QLabel(tr("stats.session_summary"))
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)

        level = get_level(summary.level_id)
        level_name = level.name_zh if tr.language == "zh_CN" else level.name_en
        layout.addWidget(QLabel(f"{tr('practice.level')}：{level_name}（0–{level.max_fret} 品）"))

        box = QGroupBox(tr("stats.title"))
        grid = QGridLayout(box)
        rows = [
            (tr("practice.progress", answered=summary.asked, total=summary.asked), ""),
            (tr("stats.accuracy"), f"{summary.accuracy_first * 100:.0f}%"),
            (tr("stats.avg_rt"), f"{summary.avg_rt_ms} ms" if summary.avg_rt_ms else "—"),
            (tr("stats.best_combo"), str(summary.best_combo)),
            (tr("practice.score"), str(summary.score)),
            (tr("practice.timeout"), str(summary.timeouts)),
            (tr("practice.skip"), str(summary.skipped)),
        ]
        for row, (key, value) in enumerate(rows):
            key_label = QLabel(key)
            key_label.setObjectName("faint")
            grid.addWidget(key_label, row, 0)
            grid.addWidget(QLabel(value), row, 1)
        layout.addWidget(box)

        if summary.weakest:
            weak = QLabel(
                tr("stats.weakest")
                + "："
                + "、".join(f"{key.split('|')[0].split('=')[1]}（{count} 次）" for key, count in summary.weakest)
            )
            weak.setWordWrap(True)
            weak.setObjectName("dim")
            layout.addWidget(weak)

        buttons = QHBoxLayout()
        again = QPushButton(tr("practice.again"))
        again.setObjectName("primary")
        again.clicked.connect(self._on_again)
        close = QPushButton(tr("common.close"))
        close.clicked.connect(self.reject)
        buttons.addStretch(1)
        buttons.addWidget(again)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    def _on_again(self) -> None:
        self.retry_requested = True
        self.accept()


__all__ = ["SessionSetupDialog", "SessionSummaryDialog"]
