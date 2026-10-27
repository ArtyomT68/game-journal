"""Распознавание прохождения сюжета по достижениям Steam.

Steam не сообщает «игра пройдена», но во многих играх есть достижение за финал.
Ищем его в списке достижений игры (на английском):

  - явные формулировки: «Finish the game», «Story Complete», «Complete the campaign»,
    «Komplete Story Mode», «Complete 100% of Story mode», «Defeat the final boss», «Epilogue»…
    Достижения за прохождение на разной сложности тоже считаются — подходит любое;
  - главы/части/акты: если есть «Complete Chapter 1…12» (минимум три номера), финал —
    достижение последней главы;
  - выбор вручную: в карточке игры можно указать финальное достижение самому —
    для игр, где финал назван по последней миссии («Complete the CASPIAN level»).

Если финальное достижение получено — сюжет пройден, дата = дата его получения.
Список достижений игры кэшируется в базе (таблица story_info): английский — для распознавания,
русский (из русской версии Steam) — для показа на сайте.
"""
import json
import re
from datetime import datetime

import requests

SCHEMA_URL = "https://api.steampowered.com/ISteamUserStats/GetSchemaForGame/v2/"
TIMEOUT = 20

FINISH = re.compile(
    # «Complete/Finish/Beat the game|the story|the main storyline|the campaign», «Complete 100% of Story mode»
    r"\b(?:finish(?:ed)?|complete[ds]?|beat(?:en)?|komplete[ds]?)\s+(?:100%\s+of\s+)?"
    r"(?:the\s+(?:main\s+)?(?:game|story|storyline|campaign|quest)|story\s*mode)\b"
    r"|\bfinish(?:ed)?\s+game\b|\bstory complete|\bgame complete|\bcampaign complete"
    r"|\broll(?:ed)? the credits|\bsee the (?:end )?credits|\breach(?:ed)? the end of the (?:game|story)"
    r"|\b(?:defeat|kill|beat)(?:ed)? the final boss",
    re.I,
)
# Частичное прохождение, вступление, кооп/сетевые режимы, DLC — это не финал основной игры
NOT_FINAL = re.compile(
    r"\b(?!100\b)\d{1,2}\s*%|\bhalf\b|all (?:base game )?achievements|\bevery\b|\beach of\b|collect|all side"
    r"|\bintro\b|prologue|tutorial|\bfirst\b|optional|assignments|\bchapters\b"
    r"|co-?op|multiplayer|online|ranked|win streak"
    r"|\bdlc\b|expansion|add-on|season pass|new game\s*(?:\+|plus)|\bng\+",
    re.I,
)
DONE_VERB = re.compile(r"\b(?:complete[ds]?|finish(?:ed)?|beat(?:en)?|clear(?:ed)?|survive[ds]?)\b", re.I)
CHAPTER = re.compile(r"\b(chapter|part|act|episode|mission)\s+(\d{1,2}|[ivx]{1,5})\b", re.I)
ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10,
         "xi": 11, "xii": 12, "xiii": 13, "xiv": 14, "xv": 15}

METHOD_LABELS = {
    "explicit": "по достижению за прохождение",
    "chapter": "по достижению за последнюю главу",
    "manual": "по выбранному тобой достижению",
    "none": "в этой игре нет понятного достижения за финал",
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS story_info (
    appid      INTEGER PRIMARY KEY,
    method     TEXT,          -- explicit | chapter | manual | none
    finals     TEXT,          -- JSON: apiname финальных достижений
    names      TEXT,          -- JSON: их названия для показа
    all_achs   TEXT,          -- JSON: [[apiname, название, описание], …] — для выбора вручную
    checked_at TEXT
);
-- игры, которые синхронизация не должна добавлять сама (ты удалил их из библиотеки)
CREATE TABLE IF NOT EXISTS sync_ignore (
    appid INTEGER PRIMARY KEY
);
"""


def init(conn):
    conn.executescript(SCHEMA_SQL)
    if "all_achs_ru" not in {r[1] for r in conn.execute("PRAGMA table_info(story_info)")}:
        # названия и описания из русской версии Steam: {apiname: [название, описание]}
        conn.execute("ALTER TABLE story_info ADD COLUMN all_achs_ru TEXT")


def _schema(api_key: str, appid: int, lang: str = "english") -> list[dict] | None:
    try:
        resp = requests.get(SCHEMA_URL, params={"key": api_key, "appid": appid, "l": lang}, timeout=TIMEOUT)
        resp.raise_for_status()
        return (resp.json().get("game", {}).get("availableGameStats") or {}).get("achievements") or []
    except (requests.RequestException, ValueError):
        return None


def _chapter(text: str) -> tuple[str, int] | None:
    m = CHAPTER.search(text)
    if not m:
        return None
    raw = m.group(2).lower()
    num = int(raw) if raw.isdigit() else ROMAN.get(raw)
    return (m.group(1).lower(), num) if num else None


def detect(achs: list[dict]) -> dict:
    """Ищет финальные достижения в списке достижений игры."""
    texts = {a["name"]: f"{a.get('displayName', '')} {a.get('description') or ''}" for a in achs}
    explicit = [a for a in achs if FINISH.search(texts[a["name"]]) and not NOT_FINAL.search(texts[a["name"]])]
    if explicit:
        return {"method": "explicit", "finals": [a["name"] for a in explicit],
                "names": [a.get("displayName") or a["name"] for a in explicit]}

    groups: dict[str, list[tuple[int, dict]]] = {}
    for a in achs:
        ch = _chapter(texts[a["name"]])
        # «Complete Chapter 5» — да; «In the job, Part 4, use the blueprint…» — нет
        if ch and DONE_VERB.search(texts[a["name"]]) and not NOT_FINAL.search(texts[a["name"]]):
            groups.setdefault(ch[0], []).append((ch[1], a))
    for kind, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        numbers = {n for n, _ in items}
        if len(numbers) >= 3 and min(numbers) <= 2:  # похоже на последовательность глав
            last = max(n for n, _ in items)
            finals = [a for n, a in items if n == last]
            return {"method": "chapter", "finals": [a["name"] for a in finals],
                    "names": [a.get("displayName") or a["name"] for a in finals]}
    return {"method": "none", "finals": [], "names": []}


def _russian(api_key: str, appid: int) -> dict[str, list[str]] | None:
    """Названия и описания достижений из русской версии Steam: {apiname: [название, описание]}."""
    achs = _schema(api_key, appid, "russian")
    if achs is None:
        return None
    return {a["name"]: [a.get("displayName") or a["name"], a.get("description") or ""] for a in achs}


def _localize(info: dict):
    """Названия финальных достижений — по-русски, если перевод есть."""
    ru = info.get("ru") or {}
    info["names"] = [ru[a][0] if a in ru else n for a, n in zip(info["finals"], info["names"])]


def get_info(conn, api_key: str, appid: int, refresh: bool = False, russian: bool = False) -> dict | None:
    """Сведения о финале игры из кэша; при отсутствии — из Steam (и сохранить).
    russian=True — догрузить русские названия, если их ещё нет в кэше (для показа в карточке).
    None — Steam не ответил (попробуем при следующей синхронизации)."""
    row = conn.execute("SELECT method, finals, names, all_achs, all_achs_ru FROM story_info WHERE appid = ?",
                       (appid,)).fetchone()
    if row and not refresh:
        info = {"method": row[0], "finals": json.loads(row[1] or "[]"), "names": json.loads(row[2] or "[]"),
                "all": json.loads(row[3] or "[]"), "ru": json.loads(row[4]) if row[4] else None}
        if info["ru"] is None and russian and api_key and info["all"]:
            info["ru"] = _russian(api_key, appid)
            if info["ru"] is not None:
                conn.execute("UPDATE story_info SET all_achs_ru = ? WHERE appid = ?",
                             (json.dumps(info["ru"], ensure_ascii=False), appid))
                conn.commit()
        _localize(info)
        return info
    if not api_key:
        return None
    achs = _schema(api_key, appid)
    if achs is None:
        return None
    info = detect(achs)  # распознаём по английским описаниям — правила написаны под них
    info["all"] = [[a["name"], a.get("displayName") or a["name"], a.get("description") or ""] for a in achs]
    info["ru"] = _russian(api_key, appid) if achs else {}
    if row and row[0] == "manual":  # ручной выбор не перетираем
        info.update(method="manual", finals=json.loads(row[1] or "[]"), names=json.loads(row[2] or "[]"))
    conn.execute(
        "INSERT OR REPLACE INTO story_info(appid, method, finals, names, all_achs, all_achs_ru, checked_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
        (appid, info["method"], json.dumps(info["finals"]), json.dumps(info["names"], ensure_ascii=False),
         json.dumps(info["all"], ensure_ascii=False),
         json.dumps(info["ru"], ensure_ascii=False) if info["ru"] is not None else None,
         datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    _localize(info)
    return info


def label(info: dict | None, apiname: str, fallback: str = "") -> str:
    """Название достижения для показа: русское, если есть, иначе английское."""
    if not info:
        return fallback
    ru = (info.get("ru") or {}).get(apiname)
    if ru:
        return ru[0]
    return next((n for a, n, _ in info["all"] if a == apiname), fallback)


def set_manual(conn, appid: int, apiname: str | None):
    """Выбор финального достижения вручную; None — вернуть автоматическое определение."""
    row = conn.execute("SELECT all_achs FROM story_info WHERE appid = ?", (appid,)).fetchone()
    all_achs = json.loads(row[0] or "[]") if row else []
    if apiname:
        name = next((n for a, n, _ in all_achs if a == apiname), apiname)
        conn.execute("UPDATE story_info SET method = 'manual', finals = ?, names = ? WHERE appid = ?",
                     (json.dumps([apiname]), json.dumps([name], ensure_ascii=False), appid))
    else:
        conn.execute("DELETE FROM story_info WHERE appid = ?", (appid,))
    conn.commit()


def finished_on(info: dict | None, unlocked: dict[str, int]) -> tuple[str, str] | None:
    """(дата, название достижения), если финальное достижение получено. unlocked: {apiname: unixtime}."""
    if not info or not info["finals"]:
        return None
    got = [(unlocked[a], n) for a, n in zip(info["finals"], info["names"]) if a in unlocked]
    if not got:
        return None
    ts, name = min(got)  # прохождение на любой сложности — берём самое раннее
    return datetime.fromtimestamp(ts).date().isoformat(), name
