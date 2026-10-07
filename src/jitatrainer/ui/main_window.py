"""主窗口：首页与调音器页的容器，负责设置装载与首次运行向导。"""

from __future__ import annotations

import logging
from dataclasses import replace

from PySide6.QtWidgets import QFileDialog, QMainWindow, QMessageBox, QStackedWidget, QWidget

from .. import __version__
from .. import paths
from ..core.audio import device as device_mod
from ..data.db import Database, utc_now_iso
from ..data.settings import Settings
from ..i18n import LANGUAGE_LABELS, Translator
from ..practice.session import (
    DEFAULT_COUNT,
    DEFAULT_DURATION_MINUTES,
    MODE_COUNT,
    SessionConfig,
)
from .pages.home import HomePage
from .pages.stats import StatsPage
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
        self.home.stats_requested.connect(self.show_stats)
        self.tuner = TunerPage(self.settings, self.tr)
        self.stats_page = StatsPage(self.settings, self.tr, profile_id=self.profile_id)
        self.stats_page.back_requested.connect(self.show_home)
        self.practice = None

        self.stack.addWidget(self.home)
        self.stack.addWidget(self.tuner)
        self.stack.addWidget(self.stats_page)
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

        # 数据菜单（需求 FR-1000：打开数据目录 / 立即备份 / 导入导出）
        data_menu = self.menuBar().addMenu(self.tr("settings.data_menu"))
        open_dir = data_menu.addAction(self.tr("settings.open_data_dir"))
        open_dir.triggered.connect(self.open_data_directory)
        backup_action = data_menu.addAction(self.tr("settings.backup_now"))
        backup_action.triggered.connect(self.backup_now)
        data_menu.addSeparator()
        export_action = data_menu.addAction(self.tr("settings.export"))
        export_action.triggered.connect(self.export_profile)
        import_action = data_menu.addAction(self.tr("settings.import"))
        import_action.triggered.connect(self.import_profile)
        data_menu.addSeparator()
        clear_action = data_menu.addAction(self.tr("settings.clear_stats"))
        clear_action.triggered.connect(self.clear_stats)

        menu.addSeparator()
        quit_action = menu.addAction(self.tr("common.close"))
        quit_action.triggered.connect(self.close)

    # ------------------------------------------------------------------ 数据
    def open_data_directory(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        target = paths.data_dir()
        target.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        self.statusBar().showMessage(str(target))

    def backup_now(self) -> None:
        from ..data.db import backup_database

        try:
            target = backup_database(reason="manual")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, self.tr("settings.data_menu"), f"{type(exc).__name__}: {exc}")
            return
        if target is None:
            QMessageBox.information(self, self.tr("settings.data_menu"), self.tr("settings.backup_none"))
            return
        self.statusBar().showMessage(self.tr("settings.backup_done", name=target.name))

    def export_profile(self) -> None:
        from ..data.profile_io import export_profile, suggest_export_name

        target, _selected = QFileDialog.getSaveFileName(
            self,
            self.tr("settings.export"),
            str(paths.exports_dir() / suggest_export_name(self.profile_name)),
            "JSON (*.json)",
        )
        if not target:
            return
        try:
            summary = export_profile(self.settings.db, self.profile_id or 1, target)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, self.tr("settings.export"), f"{type(exc).__name__}: {exc}")
            return
        QMessageBox.information(
            self,
            self.tr("settings.export"),
            self.tr("settings.export_done", detail=summary.describe(), path=summary.path.name),
        )

    def import_profile(self) -> None:
        from ..data.db import backup_database
        from ..data.profile_io import ProfileFormatError, import_profile, read_profile_file

        source, _selected = QFileDialog.getOpenFileName(
            self, self.tr("settings.import"), str(paths.exports_dir()), "JSON (*.json)"
        )
        if not source:
            return
        try:
            payload = read_profile_file(source)
        except ProfileFormatError as exc:
            QMessageBox.warning(self, self.tr("settings.import"), str(exc))
            return

        name = str((payload.get("profile") or {}).get("name") or "?")
        box = QMessageBox(self)
        box.setWindowTitle(self.tr("settings.import"))
        box.setText(self.tr("settings.import_confirm", name=name))
        new_button = box.addButton(self.tr("settings.import_mode_new"), QMessageBox.ButtonRole.AcceptRole)
        overwrite_button = box.addButton(
            self.tr("settings.import_mode_overwrite"), QMessageBox.ButtonRole.DestructiveRole
        )
        box.addButton(self.tr("common.cancel"), QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked not in (new_button, overwrite_button):
            return

        mode = "new" if clicked is new_button else "overwrite"
        # 导入前先备份，覆盖导入尤其需要（需求 FR-1000）
        try:
            backup_database(reason="import")
        except Exception:  # noqa: BLE001 - 备份失败不阻塞导入，但会记日志
            logging.getLogger(__name__).exception("导入前备份失败")
        try:
            summary = import_profile(
                self.settings.db, source, mode=mode, target_profile_id=self.profile_id
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, self.tr("settings.import"), f"{type(exc).__name__}: {exc}")
            return

        QMessageBox.information(
            self,
            self.tr("settings.import"),
            self.tr(
                "settings.import_done",
                name=summary.profile_name,
                items=summary.items,
                sessions=summary.sessions,
                attempts=summary.attempts,
            ),
        )
        self.switch_profile(summary.profile_id)

    def clear_stats(self) -> None:
        answer = QMessageBox.question(
            self,
            self.tr("settings.clear_stats"),
            self.tr("settings.clear_confirm", name=self.profile_name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        from ..data.db import backup_database

        try:
            backup_database(reason="clear")
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("清空前备份失败")

        profile_id = self.profile_id or 1
        with self.settings.db.connect() as conn:
            conn.execute(
                "DELETE FROM attempts WHERE session_id IN "
                "(SELECT id FROM sessions WHERE profile_id = ?)",
                (profile_id,),
            )
            conn.execute("DELETE FROM sessions WHERE profile_id = ?", (profile_id,))
            conn.execute("DELETE FROM items WHERE profile_id = ?", (profile_id,))
            conn.commit()
        self.show_home()
        self.statusBar().showMessage(self.tr("settings.clear_done"))

    def switch_profile(self, profile_id: int) -> None:
        """切换当前档案（导入后使用）。"""
        self.profile_id = profile_id
        with self.settings.db.connect() as conn:
            conn.execute(
                "UPDATE profiles SET last_used_at = ? WHERE id = ?",
                (utc_now_iso(), profile_id),
            )
            conn.commit()
            row = conn.execute("SELECT name FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        self.profile_name = str(row["name"]) if row else self.profile_name
        self.settings.profile_id = profile_id
        self.home.profile_name = self.profile_name
        self.stats_page.profile_id = profile_id
        self.stats_page.repository.profile_id = profile_id
        self.show_home()

    # ------------------------------------------------------------------ 页面
    def show_home(self) -> None:
        self.tuner.leave()
        self._teardown_practice()
        self.home.refresh()
        self.stack.setCurrentWidget(self.home)

    def show_tuner(self) -> None:
        self._teardown_practice()
        self.stack.setCurrentWidget(self.tuner)

    def show_stats(self) -> None:
        self._teardown_practice()
        self.stats_page.refresh()
        self.stack.setCurrentWidget(self.stats_page)

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
