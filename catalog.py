"""Поиск игр во внешних каталогах и загрузка обложек.

Steam Store работает без ключа. RAWG (игры не из Steam: консоли, GOG и т.д.)
подключается, если в настройках указан бесплатный ключ с https://rawg.io/apidocs.
"""
import re
import uuid
from pathlib import Path

import requests

TIMEOUT = 10
HEADERS = {"User-Agent": "GameJournal/1.0 (personal use)"}

STEAM_SEARCH = "https://store.steampowered.com/api/storesearch/"
STEAM_DETAILS = "https://store.steampowered.com/api/appdetails"
STEAM_COVER = "https://cdn.akamai.steamstatic.com/steam/apps/{appid}/library_600x900.jpg"
RAWG_API = "https://api.rawg.io/api"
STEAM_GLOBAL_ACH = "https://api.steampowered.com/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2/"
# Steam отдаёт в поиске саундтреки и артбуки как обычные приложения
NOT_A_GAME = re.compile(r"soundtrack|\bOST\b|artbook|art book|wallpaper", re.I)


def steam_cover_url(appid: int) -> str:
    return STEAM_COVER.format(appid=appid)


def steam_global_achievements(appid: int) -> dict | None:
    """Сколько в игре достижений и у какой доли игроков есть самое редкое.

    Самое редкое достижение — потолок для доли игроков со 100%:
    если его получили 3% игроков, то все достижения есть максимум у 3%.
    Ключ API не нужен. None — если достижений нет или Steam не ответил.
    """
    try:
        resp = requests.get(STEAM_GLOBAL_ACH, params={"gameid": appid}, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException:
        return None
    if not resp.ok:
        return None
    achs = resp.json().get("achievementpercentages", {}).get("achievements") or []
    if not achs:
        return None
    return {
        "total": len(achs),
        "rarest": round(min(float(a["percent"]) for a in achs), 1),
    }


def search_steam(query: str, limit: int = 10) -> list[dict]:
    resp = requests.get(
        STEAM_SEARCH,
        params={"term": query, "l": "russian", "cc": "US"},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    results = []
    for item in resp.json().get("items", []):
        if item.get("type") != "app" or NOT_A_GAME.search(item["name"]):
            continue
        results.append({
            "source": "steam",
            "id": item["id"],
            "title": item["name"],
            "cover": steam_cover_url(item["id"]),
            "thumb_fallback": item.get("tiny_image"),
            "subtitle": "Steam",
        })
    return results[:limit]


def search_rawg(query: str, api_key: str, limit: int = 10) -> list[dict]:
    resp = requests.get(
        f"{RAWG_API}/games",
        params={"key": api_key, "search": query, "page_size": limit},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    results = []
    for item in resp.json().get("results", []):
        year = (item.get("released") or "")[:4]
        platforms = ", ".join(p["platform"]["name"] for p in (item.get("parent_platforms") or [])[:3])
        results.append({
            "source": "rawg",
            "id": item["id"],
            "title": item["name"],
            "cover": item.get("background_image"),
            "thumb_fallback": None,
            "subtitle": " · ".join(x for x in (year, platforms) if x) or "RAWG",
        })
    return results


def steam_details(appid: int) -> dict:
    """Год, разработчик, жанры и запасная обложка из Steam Store."""
    resp = requests.get(
        STEAM_DETAILS,
        params={"appids": appid, "l": "russian"},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    entry = resp.json().get(str(appid)) or {}
    if not entry.get("success"):
        return {}
    data = entry["data"]
    year = re.search(r"\d{4}", (data.get("release_date") or {}).get("date", ""))
    return {
        "title": data.get("name"),
        "release_year": int(year.group()) if year else None,
        "developer": ", ".join(data.get("developers") or []) or None,
        "genres": ", ".join(g["description"] for g in (data.get("genres") or [])[:3]) or None,
        "header_image": data.get("header_image"),
        "description": data.get("short_description"),
        "metacritic": (data.get("metacritic") or {}).get("score"),
    }


def rawg_details(rawg_id: int, api_key: str) -> dict:
    resp = requests.get(
        f"{RAWG_API}/games/{rawg_id}",
        params={"key": api_key},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    released = data.get("released") or ""
    return {
        "title": data.get("name"),
        "release_year": int(released[:4]) if released[:4].isdigit() else None,
        "developer": ", ".join(d["name"] for d in (data.get("developers") or [])) or None,
        "genres": ", ".join(g["name"] for g in (data.get("genres") or [])[:3]) or None,
        "cover": data.get("background_image"),
    }


def download_cover(urls: list[str], covers_dir: Path) -> str | None:
    """Скачивает первую доступную картинку из списка, возвращает имя файла."""
    covers_dir.mkdir(parents=True, exist_ok=True)
    for url in urls:
        if not url:
            continue
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        except requests.RequestException:
            continue
        ctype = resp.headers.get("Content-Type", "")
        if resp.ok and ctype.startswith("image/"):
            ext = {"image/png": ".png", "image/webp": ".webp"}.get(ctype.split(";")[0], ".jpg")
            name = f"{uuid.uuid4().hex}{ext}"
            (covers_dir / name).write_bytes(resp.content)
            return name
    return None
