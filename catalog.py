"""Поиск игр во внешних каталогах и загрузка обложек.

Steam Store работает без ключа. RAWG (игры не из Steam: консоли, GOG и т.д.)
подключается, если в настройках указан бесплатный ключ с https://rawg.io/apidocs.
"""
import html
import re
import time
import uuid
from pathlib import Path

import requests

TIMEOUT = 10
HEADERS = {"User-Agent": "GameJournal/1.0 (personal use)"}

STEAM_SEARCH = "https://store.steampowered.com/api/storesearch/"
STORE_SEARCH = "https://store.steampowered.com/search/results/"
STEAM_TAGS = "https://store.steampowered.com/tagdata/populartags/{lang}"
COVERS_DIR = Path(__file__).resolve().parent / "static" / "covers"
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


def _row_value(pattern: str, row: str) -> str | None:
    m = re.search(pattern, row, re.S)
    return html.unescape(m.group(1)).strip() if m else None


def store_search(query: str = "", tags: list[int] | None = None, sort: str = "", count: int = 50,
                 start: int = 0) -> list[dict]:
    """Поиск как в магазине Steam: только игры (без DLC и саундтреков), до 100 результатов,
    с фильтром по тегам и сортировкой. Сразу отдаёт год выхода и долю положительных отзывов.

    sort: "" — по релевантности, "Released_DESC" — сначала новые, "Reviews_DESC" — по отзывам.
    """
    params = {"term": query, "infinite": 1, "category1": 998, "count": min(count, 100), "start": start,
              "cc": "US", "l": "russian"}
    if tags:
        params["tags"] = ",".join(map(str, tags))
    if sort:
        params["sort_by"] = sort
    # магазин Steam иногда не отвечает с первого раза — одна повторная попытка
    for attempt in range(2):
        try:
            resp = requests.get(STORE_SEARCH, params=params, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            break
        except requests.RequestException:
            if attempt:
                raise
            time.sleep(1)
    rows = re.split(r'(?=<a [^>]*class="search_result_row)', resp.json().get("results_html", ""))
    results = []
    for row in rows:
        appid = _row_value(r'data-ds-appid="(\d+)"', row)
        if not appid or "data-ds-bundleid" in row:
            continue
        title = _row_value(r'<span class="title">(.*?)</span>', row) or ""
        if NOT_A_GAME.search(title):
            continue
        released = _row_value(r'search_released[^>]*>(.*?)</div>', row) or ""
        year = re.search(r"\b(19|20)\d{2}\b", released)
        review = _row_value(r'search_review_summary[^"]*" data-tooltip-html="(.*?)"', row) or ""
        pct = re.search(r"(\d+)%", review)
        votes = re.search(r"([\d,\s]+)\s", review[review.find("%") + 1:]) if pct else None
        appid = int(appid)
        results.append({
            "source": "steam",
            "id": appid,
            "title": title,
            "cover": steam_cover_url(appid),
            "thumb_fallback": _row_value(r'<img src="([^"]+)"', row),
            "year": int(year.group()) if year else None,
            "rating": int(pct.group(1)) if pct else None,
            "reviews": int(re.sub(r"\D", "", votes.group(1)) or 0) if votes else None,
        })
    for r in results:
        r["subtitle"] = " · ".join(x for x in (
            str(r["year"]) if r["year"] else "",
            f"👍 {r['rating']}%" if r["rating"] is not None else "",
        ) if x) or "Steam"
    return results


def quick_search(query: str) -> list[dict]:
    """Быстрый поиск Steam (storesearch): понимает начало слова («cyberp», «hogw») и русские
    названия («ведьм» → «Ведьмак 3»), но отдаёт не больше 10 результатов."""
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
    return results


def search_steam(query: str, limit: int = 30) -> list[dict]:
    """Поиск по названию: объединяем оба поиска Steam.

    storesearch понимает недописанные слова, но отдаёт максимум 10 результатов (на «Assassin's
    Creed» Odyssey в них не попадает); поиск магазина отдаёт до 100, но ищет только целые слова.
    """
    results: list[dict] = []
    for search in (lambda: quick_search(query), lambda: store_search(query, count=limit)):
        try:
            found = search()
        except (requests.RequestException, ValueError):
            continue
        known = {r["id"]: r for r in results}
        for r in found:
            if r["id"] in known:
                # у поиска магазина есть год и рейтинг — дополняем ими запись быстрого поиска
                if r.get("year") or r.get("rating") is not None:
                    known[r["id"]].update({k: r[k] for k in ("year", "rating", "reviews", "subtitle") if k in r})
            else:
                results.append(r)
    if not results:
        raise requests.RequestException("Steam не ответил")
    return results[:limit]


_tags_cache: dict[str, tuple[float, list[dict]]] = {}


def steam_tags(lang: str = "russian") -> list[dict]:
    """Популярные теги Steam: [{tagid, name}], кэш на сутки.
    russian — для подписей; english — чтобы сопоставлять с тегами SteamSpy (они на английском)."""
    hit = _tags_cache.get(lang)
    if hit and time.time() - hit[0] < 86400:
        return hit[1]
    try:
        tags = requests.get(STEAM_TAGS.format(lang=lang), headers=HEADERS, timeout=TIMEOUT).json()
    except (requests.RequestException, ValueError):
        return hit[1] if hit else []
    _tags_cache[lang] = (time.time(), tags)
    return tags


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
