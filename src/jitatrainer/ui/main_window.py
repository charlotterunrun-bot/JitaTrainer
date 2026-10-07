"""主窗口：首页与调音器页的容器，负责设置装载与首次运行向导。"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtWidgets import QMainWindow, QStackedWidget, QWidget

from .. import __version__
from ..core.audio import device as device_mod
from ..data.db import Database
from ..data.settings import Settings
from ..i18n import LANGUAGE_LABELS, Translator
from ..practice.session import (
    DEFAULT_COUNT,
    DEFAULT_DURATION_MINUTES,
    MODE_COUNT,
    SessionConfig,
)
from .pages.home import HomePage
from .pages.tuner import TunerPage
from .pages.wizard import FirstRunWizard
from .theme import dark_stylesheet


class MainWindow(QMainWindow):
    def __init__(self, tr: Translator | None = None, db: Database | None = None) -> None:
        super().__init__()
        self.db = db or Database()
        self.db.initialize()
        self.conn = self.db.connect()

        profile = self._current_profile()
        self.profile_id = int(profile["id"]) if profile else None
        self.profile_name = str(profile["name"]) if profile else "—"

        self.settings = Settings(self.db, self.conn, self.profile_id)
        self.tr = tr or Translator(self.settings.get("language", "zh_CN"))

        self.setWindowTitle(f"{self.tr('app.title')} · {__version__}")
        self.resize(1000, 680)
        self.setStyleSheet(dark_stylesheet())

        self.stack = QStackedWidget()
        self.home = HomePage(self.settings, self.tr, self.profile_name)
        self.home.tuner_requested.connect(self.show_tuner)
        self.home.wizard_requested.connect(self.open_wizard)
        self.home.practice_requested.connect(self.start_practice)
        self.tuner = TunerPage(self.settings, self.tr)
        self.practice = None

        self.stack.addWidget(self.home)
        self.stack.addWidget(self.tuner)
        self.setCentralWidget(self.stack)

        self._build_menu()
        self.home.refresh()
        self.statusBar().showMessage(self._device_summary())

    # ------------------------------------------------------------------ 设置
    def _current_profile(self):
        with self.db.connect() as conn:
            profiles = self.db.list_profiles(conn)
            if not profiles:
                return None
            last = self.settings_last_profile_id()
            for row in profiles:
                if row["id"] == last:
                    return row
            return profiles[0]

    def settings_last_profile_id(self) -> int:
        try:
            with self.db.connect() as conn:
                raw = self.db.get_app_setting(conn, "last_profile_id", "1")
            return int(raw or 1)
        except (TypeError, ValueError):
            return 1

    def _device_summary(self) -> str:
        devices = device_mod.list_input_devices()
        if not devices:
            return self.tr("error.no_input_device")
        index = self.settings.get_optional_int("input_device_index")
        for item in devices:
            if item.index == index:
                return f"{self.tr('settings.input_device')}: {item.name}"
        default = device_mod.default_input_device()
        return f"{self.tr('settings.input_device')}: {default.name if default else '—'}"

    def _build_menu(self) -> None:
        menu = self.menuBar().addMenu(self.tr("settings.title"))
        language_menu = menu.addMenu(self.tr("settings.language"))
        for code in self.tr.available_languages():
            action = language_menu.addAction(LANGUAGE_LABELS.get(code, code))
            action.setCheckable(True)
            action.setChecked(code == self.tr.language)
            action.triggered.connect(lambda _checked=False, c=code: self.switch_language(c))
        menu.addSeparator()
        wizard_action = menu.addAction(self.tr("wizard.title"))
        wizard_action.triggered.connect(self.open_wizard)
        menu.addSeparator()
        quit_action = menu.addAction(self.tr("common.close"))
        quit_action.triggered.connect(self.close)

    # ------------------------------------------------------------------ 页面
    def show_home(self) -> None:
        self.tuner.leave()
        self._teardown_practice()
        self.home.refresh()
        self.stack.setCurrentWidget(self.home)

    def show_tuner(self) -> None:
        self._teardown_practice()
        self.stack.setCurrentWidget(self.tuner)

    # ------------------------------------------------------------------ 练习
    def session_config(self) -> SessionConfig:
        """从设置还原上次的会话配置。"""
        return SessionConfig(
            module_id="pitch_find",
            mode=self.settings.get("session_mode", MODE_COUNT),
            target_minutes=self.settings.get_int("session_minutes", DEFAULT_DURATION_MINUTES),
            target_count=self.settings.get_int("session_count", DEFAULT_COUNT),
            level_id=self.settings.get("level_id", "L1"),
            include_accidentals=self.settings.get_bool("include_accidentals", False),
            scoring_enabled=self.settings.get_bool("scoring_enabled", True),
            show_note_name=self.settings.get_bool("show_note_name", False),
        )

    def save_session_config(self, config: SessionConfig) -> None:
        self.settings.update(
            {
                "session_mode": config.mode,
                "session_minutes": config.target_minutes,
                "session_count": config.target_count,
                "level_id": config.level_id,
                "include_accidentals": config.include_accidentals,
                "scoring_enabled": config.scoring_enabled,
                "show_note_name": config.show_note_name,
            }
        )

    def start_practice(self, module_id: str = "pitch_find") -> None:
        from ..practice import registry
        from .dialogs import SessionSetupDialog

        try:
            module = registry.get_module(module_id)
        except KeyError:
            self.statusBar().showMessage(f"未找到练习模块：{module_id}")
            return

        base = self.session_config()
        base = replace(base, module_id=module_id)
        dialog = SessionSetupDialog(base, self.tr, self)
        if not dialog.exec():
            return
        config = dialog.config()
        self.save_session_config(config)
        self._open_practice(config, module)

    def _open_practice(self, config: SessionConfig, module) -> None:  # noqa: ANN001
        from .pages.practice import PracticePage

        self._teardown_practice()
        self.tuner.leave()
        page = PracticePage(
            self.settings,
            self.tr,
            config,
            module,
            profile_id=self.profile_id,
        )
        page.finished.connect(self._on_practice_finished)
        page.exit_requested.connect(self.show_home)
        self.practice = page
        self.stack.addWidget(page)
        self.stack.setCurrentWidget(page)
        page.start()
        self.statusBar().showMessage(
            f"{module.label(self.tr.language)} · {config.describe(self.tr.language)}"
        )

    def _teardown_practice(self) -> None:
        if self.practice is None:
            return
        page, self.practice = self.practice, None
        page.leave()
        self.stack.removeWidget(page)
        page.deleteLater()

    def _on_practice_finished(self, summary) -> None:  # noqa: ANN001
        from .dialogs import SessionSummaryDialog

        dialog = SessionSummaryDialog(summary, self.tr, self)
        dialog.exec()
        if dialog.retry_requested:
            module = None
            from ..practice import registry

            try:
                module = registry.get_module(summary.module_id)
            except KeyError:
                pass
            if module is not None:
                config = replace(self.session_config(), level_id=summary.level_id)
                self._open_practice(config, module)
                return
        self.show_home()

    def switch_language(self, code: str) -> None:
        self.tr.set_language(code)
        self.settings.set("language", code)
        self._build_menu()
        self.home.tr = self.tr
        self.tuner.tr = self.tr
        self.statusBar().showMessage(
            f"语言已切换为 {LANGUAGE_LABELS.get(code, code)}（部分文案将在下次启动时完全生效）"
        )

    def open_wizard(self) -> None:
        wizard = FirstRunWizard(self.settings, self.tr, self)
        if wizard.exec():
            wizard.apply()
            self.tuner.leave()
            self.home.refresh()
            self.statusBar().showMessage("向导结果已保存")

    def ensure_first_run(self) -> None:
        """首次运行时自动弹出向导。"""
        if not self.settings.get_bool("wizard_completed", False):
            self.open_wizard()

    def closeEvent(self, event) -> None:  # noqa: ANN001, N802
        self.tuner.leave()
        try:
            self.settings.set("last_profile_id", self.profile_id or 1)
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(event)


__all__ = ["MainWindow", "QWidget"]
