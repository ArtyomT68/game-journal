"""Жанры, годы и отзывы для игр аккаунта Steam — чтобы фильтровать список на вкладке «Импорт».

Данные берутся из того же кэша, что и для рекомендаций (таблица steam_tags):
теги и отзывы — SteamSpy, год выхода — Steam Store. Чего нет в кэше, догружается в фоне:
SteamSpy отвечает примерно раз в секунду, поэтому первая загрузка для большой библиотеки
занимает минуту-другую, дальше всё мгновенно.
"""
import json
import sqlite3
import threading
import time
from datetime import datetime

import recommend
from db import connect

_lock = threading.Lock()
RETRY_AFTER = 24 * 3600
_tried: dict[int, float] = {}  # игры, для которых данных не нашлось: не перезапрашиваем сутки


def missing(conn, appids: list[int]) -> list[int]:
    """Игры, для которых ещё нет тегов или года выхода."""
    known = {r[0] for r in conn.execute(
        "SELECT appid FROM steam_tags WHERE tags IS NOT NULL AND release_year IS NOT NULL")}
    now = time.time()
    return [a for a in appids if a not in known and now - _tried.get(a, 0) > RETRY_AFTER]


def get_status(conn) -> dict:
    row = conn.execute("SELECT value FROM settings WHERE key = 'library_meta_status'").fetchone()
    status = json.loads(row[0]) if row and row[0] else {"state": "idle"}
    if status.get("state") == "running" and not _lock.locked():  # сайт перезапускали посреди загрузки
        status["state"] = "idle"
    return status


def _set_status(conn, **status):
    status["at"] = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO settings(key, value) VALUES('library_meta_status', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (json.dumps(status),),
    )
    conn.commit()


def start(db_path, appids: list[int]) -> bool:
    """Догружает данные в фоне. False — если загрузка уже идёт."""
    if not appids or not _lock.acquire(blocking=False):
        return False

    def run():
        conn = connect(db_path)
        try:
            for i, appid in enumerate(appids, 1):
                _set_status(conn, state="running", done=i - 1, total=len(appids))
                _tried[appid] = time.time()
                recommend.game_tags(conn, appid)      # теги и отзывы (SteamSpy)
                recommend.release_year(conn, appid)   # год выхода (Steam Store)
            _set_status(conn, state="done", done=len(appids), total=len(appids))
        except Exception as exc:  # фоновая задача: ошибку показываем на странице
            _set_status(conn, state="error", text=str(exc))
        finally:
            conn.close()
            _lock.release()

    with connect(db_path) as conn:
        recommend.init(conn)
        _set_status(conn, state="running", done=0, total=len(appids))
    threading.Thread(target=run, daemon=True).start()
    return True


def rows_meta(conn, appids: list[int], tag_ids: dict[str, int]) -> dict[int, dict]:
    """{appid: {tags: [tagid…], year, rating, reviews}} из кэша. tag_ids: английское имя тега → id."""
    out = {}
    marks = ",".join("?" * len(appids)) or "NULL"
    for appid, tags, pos, neg, year in conn.execute(
            f"SELECT appid, tags, positive, negative, release_year FROM steam_tags WHERE appid IN ({marks})",
            appids):
        names = json.loads(tags or "{}")
        total = (pos or 0) + (neg or 0)
        out[appid] = {
            "tags": [tag_ids[n] for n in names if n in tag_ids],
            "year": year or None,
            "rating": round(pos * 100 / total) if total else None,
            "reviews": total,
        }
    return out
