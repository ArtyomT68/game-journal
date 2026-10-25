"""Оценка «стоит ли закрывать на 100%» по данным из интернета.

Источники:
  - HowLongToBeat: время на сюжет и на 100%, рейтинг игроков,
    сколько из прошедших добили игру до 100%;
  - Steam: описания достижений (ищем гринд и онлайн) и оценка Metacritic как запасной рейтинг.

Итог — две оценки 1–5 (интерес и гринд) и пояснения, из чего они сложились.
"""
import re
import time
from datetime import datetime

import requests
from howlongtobeatpy import HowLongToBeat

import catalog

SCHEMA_URL = "https://api.steampowered.com/ISteamUserStats/GetSchemaForGame/v2/"

# Достижения «сделай 500 убийств», «собери все предметы», «прокачай до максимума»
GRIND_RE = re.compile(
    r"\b\d{3,}\b|\b\d{1,3}(?:,\d{3})+\b|collect (?:all|every)|all (?:the )?collectibles|find (?:all|every)"
    r"|every (?:collectible|item|weapon|upgrade)|max(?:imum|ed)? (?:level|out)|fully upgrade|all upgrades",
    re.I,
)
# Онлайн-достижения: зависят от других игроков и могут стать недоступны
ONLINE_RE = re.compile(r"\bonline\b|multiplayer|\branked\b|\bpvp\b|competitive|versus mode", re.I)


def clamp(x: float) -> int:
    return max(1, min(5, round(x)))


def clean_title(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[™®©]", "", title)).strip()


def steam_english_name(appid: int) -> str | None:
    try:
        data = requests.get(catalog.STEAM_DETAILS, params={"appids": appid, "l": "english", "filters": "basic"},
                            timeout=15).json().get(str(appid), {})
    except (requests.RequestException, ValueError):
        return None
    return (data.get("data") or {}).get("name")


def find_hltb(title: str, year: int | None):
    """Лучшее совпадение на HowLongToBeat: по названию, при равенстве — по году выхода."""
    try:
        results = HowLongToBeat().search(title) or []
    except Exception:  # библиотека бросает разные ошибки при сбоях сайта
        return None
    # «compil» — сборники вроде Uncharted: Legacy of Thieves Collection
    results = [r for r in results if r.similarity >= 0.5 and r.game_type in ("game", "compil")]
    if not results:
        return None
    return max(results, key=lambda r: (r.similarity, year is not None and r.release_world == year))


def steam_achievement_flags(appid: int, api_key: str) -> dict | None:
    """Доля «гриндовых» достижений и число онлайн-достижений по их описаниям."""
    try:
        resp = requests.get(SCHEMA_URL, params={"key": api_key, "appid": appid, "l": "english"}, timeout=15)
    except requests.RequestException:
        return None
    if not resp.ok:
        return None
    achs = (resp.json().get("game", {}).get("availableGameStats", {}) or {}).get("achievements") or []
    if not achs:
        return None
    texts = [f"{a.get('displayName', '')} {a.get('description', '')}" for a in achs]
    return {
        "grind_share": round(sum(bool(GRIND_RE.search(t)) for t in texts) / len(texts), 2),
        "online": sum(bool(ONLINE_RE.search(t)) for t in texts),
    }


# Кэш оценок: карточка из поиска и последующее добавление игры не ждут HowLongToBeat дважды
CACHE_TTL = 6 * 3600
_cache: dict[tuple, tuple[float, dict]] = {}


def estimate(title: str, year: int | None, steam_appid: int | None, api_key: str = "",
             fresh: bool = False) -> dict:
    """Оценка с кэшем на несколько часов; fresh=True — пересчитать заново."""
    key = (title, year, steam_appid, bool(api_key))
    hit = _cache.get(key)
    if hit and not fresh and time.time() - hit[0] < CACHE_TTL:
        return dict(hit[1])
    result = _estimate(title, year, steam_appid, api_key)
    _cache[key] = (time.time(), result)
    return dict(result)


def _estimate(title: str, year: int | None, steam_appid: int | None, api_key: str = "") -> dict:
    """Собирает данные и считает оценки. Возвращает поля для таблицы games."""
    reasons: list[str] = []
    out: dict = {"auto_checked_at": datetime.now().isoformat(timespec="seconds")}

    hltb = find_hltb(clean_title(title), year)
    if hltb is None and steam_appid:  # русское название из Steam HowLongToBeat не находит
        english = steam_english_name(steam_appid)
        if english and english != title:
            hltb = find_hltb(clean_title(english), year)
    main =hltb.main_story if hltb and hltb.main_story else None
    full = hltb.completionist if hltb and hltb.completionist else None
    share = None
    if hltb:
        js = hltb.json_content
        finished = (js.get("comp_main_count") or 0) + (js.get("comp_plus_count") or 0) + (js.get("comp_100_count") or 0)
        if finished >= 20:  # на малой выборке доля ничего не значит
            share = round((js.get("comp_100_count") or 0) / finished, 2)
    out.update(hltb_id=hltb.game_id if hltb else None, hltb_main=main, hltb_100=full, hltb_100_share=share)

    quality, source = (hltb.review_score, "HowLongToBeat") if hltb and hltb.review_score else (None, None)
    if quality is None and steam_appid:
        try:
            meta = requests.get(catalog.STEAM_DETAILS, params={"appids": steam_appid, "filters": "metacritic"},
                                timeout=15).json().get(str(steam_appid), {})
            quality = ((meta.get("data") or {}).get("metacritic") or {}).get("score")
            source = "Metacritic" if quality else None
        except (requests.RequestException, ValueError):
            pass
    out.update(quality_score=quality, quality_source=source)

    flags = steam_achievement_flags(steam_appid, api_key) if steam_appid and api_key else None
    out.update(ach_grind_share=flags["grind_share"] if flags else None, ach_online=flags["online"] if flags else None)

    # --- гринд: во сколько раз 100% дольше сюжета + гриндовые и онлайн-достижения
    grind = None
    if main and full:
        ratio = full / main
        grind = 1 if ratio <= 1.6 else 2 if ratio <= 2.2 else 3 if ratio <= 3 else 4 if ratio <= 4.5 else 5
        reasons.append(f"100% дольше сюжета в {ratio:.1f} раза ({main:.0f} ч → {full:.0f} ч)")
    if flags:
        if flags["grind_share"] >= 0.25:
            grind = (grind or 3) + 1
            reasons.append(f"{flags['grind_share']:.0%} достижений — на сбор и счётчики")
        elif grind is None:
            grind = 2
    if flags and flags["online"] >= 3:
        grind = (grind or 3) + 1
        reasons.append(f"{flags['online']} онлайн-достижений — зависят от других игроков")
    out["auto_grind"] = clamp(grind) if grind is not None else None

    # --- интерес: насколько игра нравится людям и как часто её добивают до 100%
    fun = None
    if quality:
        fun = 4.5 if quality >= 88 else 4 if quality >= 80 else 3 if quality >= 72 else 2 if quality >= 64 else 1.5
        reasons.append(f"рейтинг {source}: {quality}/100")
    if share is not None:
        base = fun if fun is not None else 3
        if share >= 0.3:
            fun, note = base + 1, "добивают очень часто"
        elif share >= 0.18:
            fun, note = base + 0.5, "добивают часто"
        elif share < 0.08:
            fun, note = base - 0.5, "добивают редко"
        else:
            fun, note = base, "добивают средне"
        reasons.append(f"{share:.0%} прошедших закрыли на 100% — {note}")
    if fun is not None and flags and flags["online"] >= 3:
        fun -= 1
    out["auto_fun"] = clamp(fun) if fun is not None else None

    out["auto_reasons"] = "\n".join(reasons) or None
    return out
