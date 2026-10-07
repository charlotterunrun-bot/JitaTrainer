"""迁移 v1：初始数据库结构（对应技术方案 §10.1）。"""

from __future__ import annotations

VERSION = 1

SQL = """
CREATE TABLE profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  color TEXT NOT NULL DEFAULT '#4A90D9',
  created_at TEXT NOT NULL,
  last_used_at TEXT
);

CREATE TABLE app_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE profile_settings (
  profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  value TEXT NOT NULL,
  PRIMARY KEY (profile_id, key)
);

CREATE TABLE items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  module_id TEXT NOT NULL,
  item_key TEXT NOT NULL,
  pitch_class INTEGER NOT NULL,
  level_id TEXT NOT NULL,
  ease REAL NOT NULL DEFAULT 2.5,
  interval_index INTEGER NOT NULL DEFAULT 0,
  due_at TEXT,
  reps INTEGER NOT NULL DEFAULT 0,
  lapses REAL NOT NULL DEFAULT 0,
  seen_count INTEGER NOT NULL DEFAULT 0,
  correct_count INTEGER NOT NULL DEFAULT 0,
  error_rate REAL NOT NULL DEFAULT 0,
  avg_rt_ms INTEGER,
  last_seen_at TEXT,
  UNIQUE (profile_id, module_id, item_key)
);

CREATE INDEX idx_items_due ON items(profile_id, module_id, due_at);
CREATE INDEX idx_items_level ON items(profile_id, module_id, level_id);

CREATE TABLE sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  module_id TEXT NOT NULL,
  mode TEXT NOT NULL,
  target_value INTEGER,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  total INTEGER NOT NULL DEFAULT 0,
  correct_first INTEGER NOT NULL DEFAULT 0,
  correct_final INTEGER NOT NULL DEFAULT 0,
  wrong INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0,
  timeout INTEGER NOT NULL DEFAULT 0,
  avg_rt_ms INTEGER,
  best_combo INTEGER NOT NULL DEFAULT 0,
  score INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_sessions_profile ON sessions(profile_id, started_at);

CREATE TABLE attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
  question_json TEXT NOT NULL,
  source_tag TEXT NOT NULL,
  answered_at TEXT NOT NULL,
  result TEXT NOT NULL,
  attempt_index INTEGER NOT NULL DEFAULT 1,
  rt_ms INTEGER,
  detected_pitch_class INTEGER,
  detected_hz REAL,
  cents_offset REAL,
  confidence REAL
);

CREATE INDEX idx_attempts_session ON attempts(session_id);
CREATE INDEX idx_attempts_time ON attempts(answered_at);

CREATE TABLE app_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  level TEXT NOT NULL,
  message TEXT NOT NULL,
  context_json TEXT
);

CREATE INDEX idx_events_time ON app_events(ts);
"""
