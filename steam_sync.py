"""Синхронизация со Steam Web API.

Нужны ключ (https://steamcommunity.com/dev/apikey) и открытые
«Сведения об играх» в настройках приватности профиля.
"""
import re
from datetime import datetime

import requests

import catalog
import story

API ="https://api.steampowered.com"
TIMEOUT = 15


class SteamError(Exception):
    pass


def _get(path: str, **params) -> dict:
    try:
        resp = requests.get(f"{API}/{path}", params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise SteamError(f"Steam недоступен: {exc}") from exc
    if resp.status_code in (401, 403):
        raise SteamError("Steam отклонил ключ API — проверь его в настройках.")
    if not resp.ok:
        raise SteamError(f"Steam вернул ошибку {resp.status_code}.")
    return resp.json()


def resolve_steamid(api_key: str, value: str) -> str:
    """Принимает SteamID64, ссылку на профиль или короткое имя профиля."""
    value = value.strip().rstrip("/")
    m = re.search(r"(\d{17})$", value)
    if m:
        return m.group(1)
    vanity = value.split("/id/")[-1] if "/id/" in value else value
    data = _get("ISteamUser/ResolveVanityURL/v1/", key=api_key, vanityurl=vanity)
    resp = data.get("response", {})
    if resp.get("success") != 1:
        raise SteamError(f"Не удалось найти профиль Steam «{vanity}».")
    return resp["steamid"]


def owned_games(api_key: str, steamid: str) -> dict[int, dict]:
    data = _get(
        "IPlayerService/GetOwnedGames/v1/",
        key=api_key, steamid=steamid, include_appinfo=1, include_played_free_games=1,
    )
    games = data.get("response", {}).get("games")
    if games is None:
        raise SteamError("Steam не отдал список игр. Открой «Сведения об играх» "
                         "в настройках приватности профиля.")
    return {g["appid"]: g for g in games}


def achievements(api_key: str, steamid: str, appid: int) -> dict | None:
    """Возвращает {'unlocked', 'total', 'last_unlock', 'times'} или None, если достижений нет.
    times — {apiname: unixtime} полученных достижений (для распознавания прохождения)."""
    try:
        resp = requests.get(
            f"{API}/ISteamUserStats/GetPlayerAchievements/v1/",
            params={"key": api_key, "steamid": steamid, "appid": appid},
            timeout=TIMEOUT,
        )
    except requests.RequestException:
        return None
    if not resp.ok:  # у игры нет достижений или статистика скрыта
        return None
    achs = resp.json().get("playerstats", {}).get("achievements") or []
    if not achs:
        return None
    unlocked = [a for a in achs if a.get("achieved")]
    last = max((a.get("unlocktime", 0) for a in unlocked), default=0)
    return {
        "unlocked": len(unlocked),
        "total": len(achs),
        "last_unlock": datetime.fromtimestamp(last).date().isoformat() if last else None,
        "times": {a["apiname"]: a.get("unlocktime", 0) for a in unlocked if a.get("unlocktime")},
    }


def store_global_achievements(db, game_id: int, appid: int):
    """Сохраняет число достижений и редкость самого редкого (0 — достижений нет)."""
    info = catalog.steam_global_achievements(appid)
    db.execute(
        "UPDATE games SET steam_global_ach_total = ?, steam_global_rarest = ? WHERE id = ?",
        (info["total"] if info else 0, info["rarest"] if info else None, game_id),
    )


AUTO_ADD_MIN_PLAYTIME = 60  # минут: не трогаем игры, запущенные «на минутку»

EDITION = re.compile(
    r"enhanced|definitive|complete|ultimate|deluxe|gold|remaster(?:ed)?|redux|director'?s cut"
    r"|game of the year|goty|edition|\(\d{4}\)|\(classic\)",
    re.I,
)


def same_game(title: str) -> str:
    """Ключ «та же игра»: Metro Exodus и Metro Exodus Enhanced Edition — одно и то же."""
    title = EDITION.sub(" ", re.sub(r"[™®©]", "", title.lower()))
    return re.sub(r"[^a-zа-яё0-9]+", " ", title).strip()


def sync_library(db, api_key: str, steamid: str, progress=lambda text: None) -> dict:
    """Обновляет Steam-данные у игр библиотеки, распознаёт прохождение по достижениям
    и добавляет в библиотеку пройденные игры из аккаунта, которых в ней ещё нет.

    Поля, которые пользователь уже заполнил сам, не трогаются.
    """
    story.init(db)
    owned = owned_games(api_key, steamid)
    rows = db.execute("SELECT * FROM games WHERE steam_appid IS NOT NULL").fetchall()
    stats = {"updated": 0, "not_owned": 0, "story_dated": 0, "story_flagged": [], "added": []}
    now = datetime.now().isoformat(timespec="seconds")

    for i, row in enumerate(rows, 1):
        progress(f"Игры библиотеки: {i} из {len(rows)}")
        # глобальная редкость достижений не зависит от аккаунта — загружаем один раз
        if row["steam_global_ach_total"] is None:
            store_global_achievements(db, row["id"], row["steam_appid"])

        game = owned.get(row["steam_appid"])
        if not game:
            stats["not_owned"] += 1
            continue
        playtime = game.get("playtime_forever") or 0
        ach = achievements(api_key, steamid, row["steam_appid"])
        percent = round(ach["unlocked"] * 100 / ach["total"]) if ach else None
        finale = None
        if ach:
            info = story.get_info(db, api_key, row["steam_appid"])
            finale = story.finished_on(info, ach["times"])
            if finale and info.get("ru") is None:  # плашка «сюжет пройден» — с русским названием
                finale = story.finished_on(story.get_info(db, api_key, row["steam_appid"], russian=True),
                                           ach["times"])
        story_date, story_ach = finale or (None, None)
        if finale and row["status"] == "completed" and not row["completed_on"]:
            stats["story_dated"] += 1
        if finale and row["status"] != "completed" and not row["story_done_on"]:
            stats["story_flagged"].append(row["title"])

        db.execute(
            """UPDATE games SET
                   steam_playtime_minutes = ?,
                   steam_ach_unlocked     = ?,
                   steam_ach_total        = ?,
                   steam_last_unlock      = ?,
                   steam_synced_at        = ?,
                   story_done_on          = ?,
                   story_ach              = ?,
                   playtime_minutes   = COALESCE(playtime_minutes, ?),
                   completion_percent = COALESCE(completion_percent, ?),
                   -- дата прохождения: точная — по финальному достижению, иначе примерная —
                   -- по последнему полученному; у игр «в процессе» и хотелок не ставится
                   completed_on       = CASE WHEN status = 'completed'
                                             THEN COALESCE(completed_on, ?, ?) ELSE completed_on END
               WHERE id = ?""",
            (
                playtime,
                ach["unlocked"] if ach else None,
                ach["total"] if ach else None,
                ach["last_unlock"] if ach else None,
                now,
                story_date,
                story_ach,
                playtime or None,
                percent,
                story_date,
                ach["last_unlock"] if ach else None,
                row["id"],
            ),
        )
        db.commit()
        stats["updated"] += 1

    # игры из аккаунта, которых нет в библиотеке: если финальное достижение получено — добавляем
    have = {r[0] for r in db.execute("SELECT steam_appid FROM games WHERE steam_appid IS NOT NULL")}
    have_titles = {same_game(r[0]) for r in db.execute("SELECT title FROM games")}
    ignored = {r[0] for r in db.execute("SELECT appid FROM sync_ignore")}
    others = [g for appid, g in owned.items()
              if appid not in have and appid not in ignored
              and same_game(g.get("name") or "") not in have_titles  # другое издание уже есть
              and (g.get("playtime_forever") or 0) >= AUTO_ADD_MIN_PLAYTIME]
    for i, game in enumerate(others, 1):
        progress(f"Ищу пройденные игры в аккаунте: {i} из {len(others)}")
        appid = game["appid"]
        info = story.get_info(db, api_key, appid)
        if not info or not info["finals"]:
            continue
        ach = achievements(api_key, steamid, appid)
        finale = story.finished_on(info, ach["times"]) if ach else None
        if not finale:
            continue
        if info.get("ru") is None:
            finale = story.finished_on(story.get_info(db, api_key, appid, russian=True), ach["times"])
        add_finished_game(db, game, ach, finale, now)
        stats["added"].append(game.get("name") or str(appid))

    db.commit()
    return stats


def add_finished_game(db, game: dict, ach: dict, finale: tuple[str, str], now: str):
    """Добавляет в библиотеку игру, пройденную по данным достижений Steam."""
    appid = game["appid"]
    try:
        info = catalog.steam_details(appid)
    except requests.RequestException:
        info = {}
    cover = catalog.download_cover([catalog.steam_cover_url(appid), info.get("header_image")], catalog.COVERS_DIR)
    playtime = game.get("playtime_forever") or None
    date_, ach_name = finale
    db.execute(
        """INSERT INTO games(title, steam_appid, cover, release_year, developer, genres, status,
                             playtime_minutes, completion_percent, completed_on, times_completed, note,
                             steam_playtime_minutes, steam_ach_unlocked, steam_ach_total, steam_last_unlock,
                             steam_synced_at, story_done_on, story_ach, auto_added)
           VALUES(?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
        (info.get("title") or game.get("name") or f"Steam #{appid}", appid, cover, info.get("release_year"),
         info.get("developer"), info.get("genres"), playtime, round(ach["unlocked"] * 100 / ach["total"]),
         date_, f"Добавлена синхронизацией Steam: получено достижение «{ach_name}».",
         playtime, ach["unlocked"], ach["total"], ach["last_unlock"], now, date_, ach_name),
    )
    db.commit()
