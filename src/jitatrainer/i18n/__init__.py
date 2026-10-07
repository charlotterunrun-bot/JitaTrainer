"""国际化：JSON 语言包 + 运行时切换。"""

from __future__ import annotations

import json
from pathlib import Path

from ..paths import i18n_dir

DEFAULT_LANGUAGE = "zh_CN"
SUPPORTED_LANGUAGES = ("zh_CN", "en_US")
LANGUAGE_LABELS = {"zh_CN": "中文", "en_US": "English"}

#: 若语言包文件缺失时使用的最小内置词典（保证界面不会出现 key）
_FALLBACK = {
    "app.title": "JitaTrainer 吉他训练器",
    "app.version": "版本",
    "common.ok": "确定",
    "common.cancel": "取消",
}


class Translator:
    """语言包管理器。

    用法::

        tr = Translator("zh_CN")
        tr("app.title")
        tr.set_language("en_US")
    """

    def __init__(self, language: str = DEFAULT_LANGUAGE) -> None:
        self._catalogues: dict[str, dict[str, str]] = {}
        self._language = DEFAULT_LANGUAGE
        self.set_language(language)

    @property
    def language(self) -> str:
        return self._language

    def available_languages(self) -> list[str]:
        return list(SUPPORTED_LANGUAGES)

    def _load(self, language: str) -> dict[str, str]:
        if language in self._catalogues:
            return self._catalogues[language]
        path = Path(i18n_dir()) / f"{language}.json"
        data: dict[str, str] = {}
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
        self._catalogues[language] = data
        return data

    def set_language(self, language: str) -> None:
        self._language = language if language in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE

    def __call__(self, key: str, **kwargs: object) -> str:
        text = self._load(self._language).get(key)
        if text is None:
            text = self._load(DEFAULT_LANGUAGE).get(key)
        if text is None:
            text = _FALLBACK.get(key, key)
        if kwargs:
            try:
                return text.format(**kwargs)
            except (KeyError, IndexError):
                return text
        return text
