"""Работа с базой SQLite: подключение, схема, настройки."""
import sqlite3
from pathlib import Path

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    title                  TEXT    NOT NULL,
    steam_appid            INTEGER,
    rawg_id                INTEGER,
    cover                  TEXT,            -- имя файла в static/covers или внешний URL
    release_year           INTEGER,
    developer              TEXT,
    genres                 TEXT,
    -- статистика, которую заполняет пользователь
    playtime_minutes       INTEGER,
    completion_percent     INTEGER,
    completed_on           TEXT,            -- ISO-дата YYYY-MM-DD
    times_completed        INTEGER NOT NULL DEFAULT 1,
    note                   TEXT,
    status                 TEXT    NOT NULL DEFAULT 'completed',  -- completed | playing | wishlist
    started_on             TEXT,            -- когда начал проходить (для status = playing)
    story_done_on          TEXT,            -- дата финального достижения Steam (сюжет пройден)
    story_ach              TEXT,            -- название этого достижения
    auto_added             INTEGER,         -- 1 — добавлена синхронизацией Steam по достижениям
    wish_rank              INTEGER,         -- место в списке хотелок (1 — первая на очереди)
    -- оценка 100% достижений (заполняет пользователь)
    hundred_fun            INTEGER,         -- 1–5: насколько интересно закрывать
    hundred_grind          INTEGER,         -- 1–5: насколько это гринд и трата времени
    hundred_hours          INTEGER,         -- ожидаемое время на 100%, часы
    -- оценка 100% по данным из интернета (estimates.py), для хотелок
    hltb_id                INTEGER,
    hltb_main              REAL,            -- часы на сюжет (HowLongToBeat)
    hltb_100               REAL,            -- часы на 100%
    hltb_100_share         REAL,            -- доля прошедших, закрывших на 100%
    quality_score          INTEGER,         -- рейтинг игры 0–100
    quality_source         TEXT,
    ach_grind_share        REAL,            -- доля «гриндовых» достижений
    ach_online             INTEGER,         -- число онлайн-достижений
    auto_fun               INTEGER,         -- 1–5
    auto_grind             INTEGER,         -- 1–5
    auto_reasons           TEXT,            -- пояснения через перевод строки
    auto_checked_at        TEXT,
    -- глобальная статистика достижений Steam (по всем игрокам)
    steam_global_ach_total INTEGER,
    steam_global_rarest    REAL,            -- % игроков с самым редким достижением
    -- данные из Steam (только для справки, пользовательские поля не перезаписывают)
    steam_playtime_minutes INTEGER,
    steam_ach_unlocked     INTEGER,
    steam_ach_total        INTEGER,
    steam_last_unlock      TEXT,
    steam_synced_at        TEXT,
    created_at             TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_games_steam ON games(steam_appid) WHERE steam_appid IS NOT NULL;

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


# Колонки, добавленные после первой версии: базы, созданные раньше, получают их здесь
MIGRATIONS = {
    "status": "ALTER TABLE games ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'",
    "started_on": "ALTER TABLE games ADD COLUMN started_on TEXT",
    "story_done_on": "ALTER TABLE games ADD COLUMN story_done_on TEXT",
    "story_ach": "ALTER TABLE games ADD COLUMN story_ach TEXT",
    "auto_added": "ALTER TABLE games ADD COLUMN auto_added INTEGER",
    "wish_rank": "ALTER TABLE games ADD COLUMN wish_rank INTEGER",
    "hundred_fun": "ALTER TABLE games ADD COLUMN hundred_fun INTEGER",
    "hundred_grind": "ALTER TABLE games ADD COLUMN hundred_grind INTEGER",
    "hundred_hours": "ALTER TABLE games ADD COLUMN hundred_hours INTEGER",
    "steam_global_ach_total": "ALTER TABLE games ADD COLUMN steam_global_ach_total INTEGER",
    "steam_global_rarest": "ALTER TABLE games ADD COLUMN steam_global_rarest REAL",
    **{
        col: f"ALTER TABLE games ADD COLUMN {col} {kind}"
        for col, kind in [
            ("hltb_id", "INTEGER"), ("hltb_main", "REAL"), ("hltb_100", "REAL"), ("hltb_100_share", "REAL"),
            ("quality_score", "INTEGER"), ("quality_source", "TEXT"), ("ach_grind_share", "REAL"),
            ("ach_online", "INTEGER"), ("auto_fun", "INTEGER"), ("auto_grind", "INTEGER"),
            ("auto_reasons", "TEXT"), ("auto_checked_at", "TEXT"),
        ]
    },
}


def _migrate(conn: sqlite3.Connection):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(games)")}
    for column, sql in MIGRATIONS.items():
        if column not in columns:
            conn.execute(sql)


def get_setting(key: str, default: str = "") -> str:
    row = get_db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def set_setting(key: str, value: str):
    db = get_db()
    db.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    db.commit()
