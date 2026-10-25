"""Синхронизация со Steam Web API.

Нужны ключ (https://steamcommunity.com/dev/apikey) и открытые
«Сведения об играх» в настройках приватности профиля.
"""
import re
from datetime import datetime

import requests

import catalog

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
    """Возвращает {'unlocked', 'total', 'last_unlock'} или None, если достижений нет."""
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
    }


def store_global_achievements(db, game_id: int, appid: int):
    """Сохраняет число достижений и редкость самого редкого (0 — достижений нет)."""
    info = catalog.steam_global_achievements(appid)
    db.execute(
        "UPDATE games SET steam_global_ach_total = ?, steam_global_rarest = ? WHERE id = ?",
        (info["total"] if info else 0, info["rarest"] if info else None, game_id),
    )


def sync_library(db, api_key: str, steamid: str) -> dict:
    """Обновляет Steam-данные у игр библиотеки и заполняет пустые поля.

    Поля, которые пользователь уже заполнил сам, не трогаются.
    """
    owned = owned_games(api_key, steamid)
    rows = db.execute("SELECT * FROM games WHERE steam_appid IS NOT NULL").fetchall()
    stats = {"updated": 0, "not_owned": 0}
    now = datetime.now().isoformat(timespec="seconds")

    for row in rows:
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

        db.execute(
            """UPDATE games SET
                   steam_playtime_minutes = ?,
                   steam_ach_unlocked     = ?,
                   steam_ach_total        = ?,
                   steam_last_unlock      = ?,
                   steam_synced_at        = ?,
                   playtime_minutes   = COALESCE(playtime_minutes, ?),
                   completion_percent = COALESCE(completion_percent, ?),
                   -- у игр «в процессе» последнее достижение не означает прохождение
                   completed_on       = CASE WHEN status = 'completed'
                                             THEN COALESCE(completed_on, ?) ELSE completed_on END
               WHERE id = ?""",
            (
                playtime,
                ach["unlocked"] if ach else None,
                ach["total"] if ach else None,
                ach["last_unlock"] if ach else None,
                now,
                playtime or None,
                percent,
                ach["last_unlock"] if ach else None,
                row["id"],
            ),
        )
        stats["updated"] += 1

    db.commit()
    return stats
