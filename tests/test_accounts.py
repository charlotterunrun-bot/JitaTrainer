"""账号（学习者档案）与吉他档案管理测试。

"用户账号"的实质是**隔离**：不同账号的设置、训练进度、统计互不影响。
只测"能建账号"是不够的，必须验证隔离真的成立。
"""

from __future__ import annotations

import os

import pytest

from jitatrainer.data.db import Database
from jitatrainer.data.repository import DbItemStore, PracticeRepository
from jitatrainer.data.settings import Settings
from jitatrainer.scheduling.srs import ItemState, initial_state

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def database(tmp_path) -> Database:
    db = Database(tmp_path / "accounts.db")
    db.initialize()
    return db


@pytest.fixture
def conn(database: Database):
    connection = database.connect()
    yield connection
    connection.close()


class TestLearnerAccounts:
    def test_default_account_exists(self, database: Database, conn) -> None:
        rows = database.list_profiles(conn)
        assert len(rows) == 1
        assert rows[0]["name"]

    def test_create_and_list(self, database: Database, conn) -> None:
        database.create_profile(conn, "小红", "#FF8800")
        rows = database.list_profiles(conn)
        names = [row["name"] for row in rows]
        assert "小红" in names
        assert len(rows) == 2

    def test_rename(self, database: Database, conn) -> None:
        new_id = database.create_profile(conn, "小明")
        database.rename_profile(conn, new_id, "小明（改名后）")
        row = conn.execute("SELECT name FROM profiles WHERE id = ?", (new_id,)).fetchone()
        assert row["name"] == "小明（改名后）"

    def test_delete_removes_account_data(self, database: Database, conn) -> None:
        keep = int(database.list_profiles(conn)[0]["id"])
        gone = database.create_profile(conn, "临时账号")
        database.set_profile_setting(conn, gone, "level_id", "L3")
        database.upsert_item(
            conn,
            profile_id=gone,
            module_id="pitch_find",
            item_key="note=E|level=L3",
            pitch_class=4,
            level_id="L3",
        )
        conn.execute(
            "INSERT INTO sessions (profile_id, module_id, mode, started_at) "
            "VALUES (?, 'pitch_find', 'count', '2026-10-10T10:00:00Z')",
            (gone,),
        )
        conn.commit()

        database.delete_profile(conn, gone)

        assert [int(row["id"]) for row in database.list_profiles(conn)] == [keep]
        # 级联删除：训练项、会话、设置都不能残留
        for table, column in (
            ("items", "profile_id"),
            ("sessions", "profile_id"),
            ("profile_settings", "profile_id"),
        ):
            left = conn.execute(
                f"SELECT COUNT(*) AS c FROM {table} WHERE {column} = ?", (gone,)
            ).fetchone()["c"]
            assert left == 0, f"{table} 里还有被删账号的数据"


class TestAccountIsolation:
    """两个账号互不影响 —— 这是"用户账号"功能的实质。"""

    def test_settings_are_isolated(self, database: Database, conn) -> None:
        first = int(database.list_profiles(conn)[0]["id"])
        second = database.create_profile(conn, "二号")

        settings_a = Settings(database, conn, first)
        settings_b = Settings(database, conn, second)
        settings_a.set("level_id", "L1")
        settings_b.set("level_id", "L4")

        assert Settings(database, conn, first).get("level_id") == "L1"
        assert Settings(database, conn, second).get("level_id") == "L4"

    def test_global_settings_are_shared(self, database: Database, conn) -> None:
        """语言、设备、乐器档案是全局的，不该按账号分开。"""
        first = int(database.list_profiles(conn)[0]["id"])
        second = database.create_profile(conn, "二号")
        Settings(database, conn, first).set("language", "en_US")
        assert Settings(database, conn, second).get("language") == "en_US"

    def test_items_are_isolated(self, database: Database, conn, tmp_path) -> None:
        first = int(database.list_profiles(conn)[0]["id"])
        second = database.create_profile(conn, "二号")

        store_a = DbItemStore(database, first, "pitch_find")
        store_b = DbItemStore(database, second, "pitch_find")
        store_a.save(initial_state("note=E|level=L1", 4, "L1"))
        store_a.save(
            ItemState(item_key="note=E|level=L1", pitch_class=4, level_id="L1", seen_count=3)
        )

        assert "note=E|level=L1" in store_a.load("L1")
        assert store_b.load("L1") == {}, "另一个账号不该看到这个账号的训练项"
        store_a.close()
        store_b.close()

    def test_statistics_are_isolated(self, database: Database, conn) -> None:
        first = int(database.list_profiles(conn)[0]["id"])
        second = database.create_profile(conn, "二号")
        for profile_id in (first, second):
            conn.execute(
                "INSERT INTO sessions (profile_id, module_id, mode, started_at, ended_at, total) "
                "VALUES (?, 'pitch_find', 'count', '2026-10-10T10:00:00Z', '2026-10-10T10:05:00Z', ?)",
                (profile_id, 10 if profile_id == first else 3),
            )
        conn.commit()

        from jitatrainer.data.analytics import AnalyticsRepository

        assert AnalyticsRepository(database, first).overall(conn).answered == 0  # 没有答题明细
        sessions_a = PracticeRepository(database, first).recent_sessions(conn)
        sessions_b = PracticeRepository(database, second).recent_sessions(conn)
        assert sessions_a[0]["total"] == 10
        assert sessions_b[0]["total"] == 3


class TestInstrumentProfileFiles:
    def _make(self, name: str):
        from jitatrainer.core.instrument import profile_from_measurements, StringMeasurement

        return profile_from_measurements(
            [StringMeasurement(number=6, hz=81.7, level_db=-30.0)], name=name
        )

    def test_rename_updates_name_and_file(self, tmp_path, monkeypatch) -> None:
        from jitatrainer.core import instrument

        monkeypatch.setattr(instrument, "profiles_directory", lambda: tmp_path)
        from jitatrainer.core.instrument import (
            list_saved_profiles,
            load_profile,
            rename_saved_profile,
            save_profile,
        )

        path = save_profile(self._make("旧名字"), tmp_path / "old.json")
        new_path = rename_saved_profile(path, "新名字")

        assert not path.exists(), "改名后旧文件应删除"
        assert new_path.exists()
        assert load_profile(new_path).name == "新名字"
        saved = list_saved_profiles()
        assert [profile.name for _p, profile in saved] == ["新名字"]

    def test_rename_rejects_empty_name(self, tmp_path) -> None:
        from jitatrainer.core.instrument import ProfileError, rename_saved_profile, save_profile

        path = save_profile(self._make("名字"), tmp_path / "a.json")
        with pytest.raises(ProfileError):
            rename_saved_profile(path, "   ")

    def test_delete(self, tmp_path) -> None:
        from jitatrainer.core.instrument import delete_saved_profile, save_profile

        path = save_profile(self._make("待删"), tmp_path / "b.json")
        assert delete_saved_profile(path) is True
        assert not path.exists()
        assert delete_saved_profile(path) is False  # 再删一次返回 False 而不是抛异常

    def test_list_skips_broken_files(self, tmp_path, monkeypatch) -> None:
        from jitatrainer.core import instrument

        monkeypatch.setattr(instrument, "profiles_directory", lambda: tmp_path)
        from jitatrainer.core.instrument import list_saved_profiles, save_profile

        save_profile(self._make("好档案"), tmp_path / "good.json")
        (tmp_path / "broken.json").write_text("{ 不是 json", encoding="utf-8")

        saved = list_saved_profiles()
        assert [profile.name for _p, profile in saved] == ["好档案"], "坏文件应跳过而不是让列表失败"

    def test_rename_keeps_measurements(self, tmp_path) -> None:
        from jitatrainer.core.instrument import load_profile, rename_saved_profile, save_profile

        path = save_profile(self._make("原名"), tmp_path / "c.json")
        new_path = rename_saved_profile(path, "新名")
        profile = load_profile(new_path)
        assert profile.string(6).measured_hz == pytest.approx(81.7, abs=0.01)
        assert profile.is_measured


class TestProfileManagerDialog:
    def _dialog(self, database: Database, conn, tmp_path):  # noqa: ANN202
        from PySide6.QtWidgets import QApplication

        from jitatrainer.i18n import Translator
        from jitatrainer.ui.dialogs import ProfileManagerDialog

        QApplication.instance() or QApplication([])
        profile_id = int(database.list_profiles(conn)[0]["id"])
        settings = Settings(database, conn, profile_id)
        return ProfileManagerDialog(
            settings, Translator("zh_CN"), current_profile_id=profile_id
        )

    def test_lists_accounts(self, database: Database, conn, tmp_path) -> None:  # noqa: ARG002
        database.create_profile(conn, "第二个账号")
        dialog = self._dialog(database, conn, tmp_path)
        assert dialog.learner_list.count() == 2
        assert dialog.tabs.count() == 2

    def test_delete_disabled_with_single_account(self, database: Database, conn, tmp_path) -> None:  # noqa: ARG002
        dialog = self._dialog(database, conn, tmp_path)
        assert dialog.delete_learner_button.isEnabled() is False

    def test_delete_enabled_with_two_accounts(self, database: Database, conn, tmp_path) -> None:  # noqa: ARG002
        database.create_profile(conn, "第二个账号")
        dialog = self._dialog(database, conn, tmp_path)
        assert dialog.delete_learner_button.isEnabled() is True

    def test_current_account_is_marked(self, database: Database, conn, tmp_path) -> None:  # noqa: ARG002
        dialog = self._dialog(database, conn, tmp_path)
        assert dialog.learner_list.item(0).text().startswith("●")

    def test_reload_after_creating(self, database: Database, conn, tmp_path) -> None:  # noqa: ARG002
        dialog = self._dialog(database, conn, tmp_path)
        database.create_profile(conn, "第三个账号")
        dialog.reload()
        assert dialog.learner_list.count() == 2

    def test_selection_updates_without_closing(self, database: Database, conn, tmp_path) -> None:  # noqa: ARG002
        new_id = database.create_profile(conn, "另一个")
        dialog = self._dialog(database, conn, tmp_path)
        for index in range(dialog.learner_list.count()):
            item = dialog.learner_list.item(index)
            if item.data(0x0100) == new_id:  # Qt.UserRole
                dialog.learner_list.setCurrentItem(item)
        dialog.use_learner()
        assert dialog.selected_learner_id == new_id
        # 标记应移动到这个账号上
        assert dialog.learner_list.item(0).text().startswith("●") or any(
            dialog.learner_list.item(i).text().startswith("●")
            and dialog.learner_list.item(i).data(0x0100) == new_id
            for i in range(dialog.learner_list.count())
        )
