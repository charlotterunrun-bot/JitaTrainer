"""主题与配色。"""

from __future__ import annotations

COLORS = {
    "bg": "#14181d",
    "panel": "#1c2229",
    "panel_alt": "#232b34",
    "text": "#e8edf3",
    "text_dim": "#9aa4b2",
    "text_faint": "#6f7b8a",
    "accent": "#4a90d9",
    "good": "#3ecf8e",
    "warn": "#e8b339",
    "bad": "#e05c5c",
    "border": "#2c343d",
}

DARK_QSS = f"""
QWidget {{
    background-color: {COLORS["bg"]};
    color: {COLORS["text"]};
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 14px;
}}
QLabel#title {{ font-size: 28px; font-weight: 600; }}
QLabel#subtitle {{ font-size: 15px; color: {COLORS["text_dim"]}; }}
QLabel#sectionTitle {{ font-size: 18px; font-weight: 600; }}
QLabel#dim {{ color: {COLORS["text_dim"]}; }}
QLabel#faint {{ color: {COLORS["text_faint"]}; font-size: 13px; }}
QLabel#bigNote {{ font-size: 46px; font-weight: 700; }}

QPushButton {{
    background-color: {COLORS["panel_alt"]};
    color: {COLORS["text"]};
    border: 1px solid {COLORS["border"]};
    border-radius: 6px;
    padding: 9px 18px;
}}
QPushButton:hover {{ background-color: #2b343e; }}
QPushButton:disabled {{ color: {COLORS["text_faint"]}; background-color: {COLORS["panel"]}; }}
QPushButton#primary {{
    background-color: {COLORS["accent"]};
    border-color: {COLORS["accent"]};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton#primary:hover {{ background-color: #5a9ee6; }}

QComboBox, QSpinBox, QLineEdit {{
    background-color: {COLORS["panel"]};
    border: 1px solid {COLORS["border"]};
    border-radius: 6px;
    padding: 6px 10px;
    min-height: 22px;
}}
QComboBox QAbstractItemView {{
    background-color: {COLORS["panel"]};
    selection-background-color: {COLORS["accent"]};
}}

QProgressBar {{
    background-color: {COLORS["panel"]};
    border: 1px solid {COLORS["border"]};
    border-radius: 6px;
    text-align: center;
    height: 20px;
}}
QProgressBar::chunk {{ background-color: {COLORS["accent"]}; border-radius: 5px; }}

QListWidget {{
    background-color: {COLORS["panel"]};
    border: 1px solid {COLORS["border"]};
    border-radius: 6px;
}}
QListWidget::item:selected {{ background-color: {COLORS["accent"]}; }}

QGroupBox {{
    border: 1px solid {COLORS["border"]};
    border-radius: 8px;
    margin-top: 14px;
    padding-top: 10px;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; color: {COLORS["text_dim"]}; }}

QMenuBar, QMenu {{ background-color: {COLORS["panel"]}; }}
QMenu::item:selected {{ background-color: {COLORS["accent"]}; }}

QWizard, QDialog {{ background-color: {COLORS["bg"]}; }}
"""


def dark_stylesheet() -> str:
    return DARK_QSS
