"""文案完整性测试。

漏翻的键会**直接以键名显示在界面上**（例如标题变成 ``stats.period``），
用户看到的就是这种低级问题，所以必须在测试里拦住。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from jitatrainer.i18n import LANGUAGE_LABELS, SUPPORTED_LANGUAGES, Translator

SRC = Path(__file__).resolve().parents[1] / "src" / "jitatrainer"
I18N_DIR = SRC / "i18n"

#: 匹配 ``.tr("some.key")`` 与 ``tr('some.key')``
TR_CALL = re.compile(r"""\.tr\(\s*["']([A-Za-z0-9_.]+)["']""")
#: 匹配 ``{name}`` 形式的占位符
PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def load_pack(language: str) -> dict[str, str]:
    return json.loads((I18N_DIR / f"{language}.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def packs() -> dict[str, dict[str, str]]:
    return {language: load_pack(language) for language in SUPPORTED_LANGUAGES}


@pytest.fixture(scope="module")
def used_keys() -> dict[str, set[str]]:
    """源码里实际用到的键 → 使用它的文件名集合。"""
    found: dict[str, set[str]] = {}
    for path in SRC.rglob("*.py"):
        for match in TR_CALL.finditer(path.read_text(encoding="utf-8")):
            found.setdefault(match.group(1), set()).add(path.name)
    return found


class TestPackIntegrity:
    def test_languages_available(self, packs) -> None:  # noqa: ANN001
        assert set(packs) == {"zh_CN", "en_US"}
        assert set(LANGUAGE_LABELS) >= set(packs)

    def test_packs_have_same_keys(self, packs) -> None:  # noqa: ANN001
        zh, en = set(packs["zh_CN"]), set(packs["en_US"])
        assert zh - en == set(), f"只在中文包里：{sorted(zh - en)}"
        assert en - zh == set(), f"只在英文包里：{sorted(en - zh)}"

    def test_no_empty_values(self, packs) -> None:  # noqa: ANN001
        for language, pack in packs.items():
            empty = [key for key, value in pack.items() if not str(value).strip()]
            assert empty == [], f"{language} 里有空文案：{empty}"

    def test_placeholders_match_between_languages(self, packs) -> None:  # noqa: ANN001
        zh, en = packs["zh_CN"], packs["en_US"]
        mismatched = []
        for key in sorted(set(zh) & set(en)):
            zh_names = set(PLACEHOLDER.findall(str(zh[key])))
            en_names = set(PLACEHOLDER.findall(str(en[key])))
            if zh_names != en_names:
                mismatched.append((key, sorted(zh_names), sorted(en_names)))
        assert mismatched == [], f"占位符不一致（键, 中文, 英文）：{mismatched}"


class TestSourceCoverage:
    def test_every_used_key_exists(self, packs, used_keys) -> None:  # noqa: ANN001
        """源码里用的每个键都必须存在于两个语言包。"""
        for language, pack in packs.items():
            missing = sorted(key for key in used_keys if key not in pack)
            assert missing == [], (
                f"{language} 缺少这些键（界面会显示原始键名）：\n"
                + "\n".join(f"  {key}  ← {', '.join(sorted(used_keys[key]))}" for key in missing)
            )

    def test_translator_returns_text_not_key(self, packs) -> None:  # noqa: ANN001
        """抽查：常用键必须能翻译出真实文案，而不是回退成键名。"""
        for language in packs:
            translator = Translator(language)
            for key in ("app.title", "stats.title", "practice.correct", "home.due_today"):
                text = translator(key)
                assert text and text != key, f"{language} 的 {key} 翻译失败：{text!r}"

    def test_missing_key_falls_back_to_key_not_crash(self) -> None:
        translator = Translator("zh_CN")
        assert translator("definitely.not.a.key") == "definitely.not.a.key"

    def test_format_arguments_apply(self) -> None:
        translator = Translator("zh_CN")
        assert "3" in translator("home.due_today", count=3)
        assert "E" in translator("practice.wrong", played="F", target="E")

    def test_unused_keys_are_reported_but_allowed(self, packs, used_keys) -> None:  # noqa: ANN001
        """统计"定义了但没用到"的键（不失败，仅作为提示信息）。

        某些键是通过拼接或字典间接使用的，所以这里只做信息展示。
        """
        unused = sorted(set(packs["zh_CN"]) - set(used_keys))
        assert isinstance(unused, list)
        if unused:
            print(f"\n提示：{len(unused)} 个键未在源码中直接匹配到：{unused[:8]}…")
