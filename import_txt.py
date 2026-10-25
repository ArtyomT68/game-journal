"""Импорт списка пройденных игр из текстового файла.

Формат строк:  «12.Название игры (перепройден 09.09.2026) "100%"»
  - номер в начале необязателен;
  - дата ДД.ММ.ГГГГ → дата прохождения;
  - «перепройден» → 2 прохождения;
  - «100%» → процент прохождения;
  - остальное в скобках и «+ DLC …» попадает в заметку.
Повторяющиеся игры объединяются (количество прохождений растёт).

Запуск:
  .venv\\Scripts\\python import_txt.py "путь\\к\\файлу.txt" --dry-run   # только показать сопоставление
  .venv\\Scripts\\python import_txt.py "путь\\к\\файлу.txt"             # импортировать
"""
import argparse
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

import requests

import catalog
from db import init_db

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "games.db"
COVERS_DIR = BASE_DIR / "static" / "covers"

# Поисковые запросы для названий, которые Steam по записи из файла не находит
QUERY_FIXES = {
    "saint row": "saints row",
    "far cry new down": "far cry new dawn",
    "resident evil iv": "resident evil 4",
    "god of war рагнарёк": "god of war ragnarok",
}
# Точное указание игры в Steam: название → appid (0 — нет в Steam).
# Нужно там, где поиск Steam промахивается или находит DLC/саундтрек/демо.
_APPIDS = {
    "God of War Рагнарёк": 2322010,
    "Hogwarts Legacy": 990080,
    "Call of Duty: Modern Warfare 2019": 2000950,
    "Assassin's Creed Unity": 289650,
    "Assassin's Creed III": 911400,  # Remastered — эта версия в аккаунте Steam
    "Assassin's Creed Rogue": 311560,
    "Assassin's Creed Syndicate": 368500,
    "Mirror's Edge Catalyst": 1233570,
    "STAR WARS Jedi: Fallen Order": 1172380,
    "HITMAN Episode 1": 236870,
    "HITMAN Episode 2": 236870,
    "Spec Ops The Line": 50300,
    "Call of Duty 4: Modern Warfare": 7940,
    "Call of Duty Modern Warfare 2": 10180,
    "Call of Duty Modern Warfare 3": 115300,
    "Call Of Duty Modern Warfare II 2022": 3595230,
    "Call of Duty: Advanced Warfare": 209650,
    "Mafia III": 360430,
    "Grand Theft Auto V": 271590,
    "Grand Theft Auto: San Andreas": 12120,
    "Ghost of Tsushima": 2215430,
    "The Last of Us": 1888930,
    "The Last of Us Part II": 2531310,
}
APPID_OVERRIDES: dict[str, int] = {}  # заполняется ниже, после normalize()

DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


@dataclass
class Entry:
    title: str
    line_no: int
    completed_on: str | None = None
    percent: int | None = None
    times: int = 1
    notes: list[str] = field(default_factory=list)


def normalize(title: str) -> str:
    t = title.lower().replace("ё", "е")
    t = re.sub(r"[®™©]", "", t)
    t = re.sub(r"[^\w\s]", " ", t).replace("_", " ")
    return re.sub(r"\s+", " ", t).strip()


APPID_OVERRIDES.update({normalize(k): v for k, v in _APPIDS.items()})


def parse_line(line: str, line_no: int) -> Entry | None:
    line = line.strip()
    if not line:
        return None
    line = re.sub(r"^\d+\s*[.)]\s*", "", line)
    entry = Entry(title="", line_no=line_no)

    if '"100%"' in line or "100%" in line:
        entry.percent = 100
        line = line.replace('"100%"', "").replace("100%", "")

    for inner in re.findall(r"\(([^)]*)\)", line):
        dates = DATE_RE.findall(inner)
        if dates:
            d, m, y = dates[-1]
            entry.completed_on = f"{y}-{m}-{d}"
        if "перепрой" in inner.lower():
            entry.times = 2
        rest = DATE_RE.sub("", inner)
        rest = re.sub(r"(?i)перепройден[аоы]?|пройден[аоы]?", "", rest).strip(" ,.-")
        if rest:
            entry.notes.append(rest)
    line = re.sub(r"\([^)]*\)", "", line)

    if "+" in line:
        line, extra = line.split("+", 1)
        entry.notes.append("+ " + extra.strip())

    entry.title = re.sub(r"\s+", " ", line).strip(" .-")
    return entry if entry.title else None


def merge_duplicates(entries: list[Entry]) -> list[Entry]:
    merged: dict[str, Entry] = {}
    for e in entries:
        key = normalize(e.title)
        if key not in merged:
            merged[key] = e
            continue
        m = merged[key]
        m.times += e.times
        m.completed_on = max(filter(None, [m.completed_on, e.completed_on]), default=None)
        m.percent = max(filter(lambda x: x is not None, [m.percent, e.percent]), default=None)
        m.notes += e.notes
        m.notes.append(f"в списке также под №{e.line_no}")
    return list(merged.values())


def similarity(a: str, b: str) -> float:
    a, b = normalize(a), normalize(b)
    score = SequenceMatcher(None, a, b).ratio()
    if set(a.split()) <= set(b.split()):
        score = max(score, 0.85 - 0.02 * (len(b.split()) - len(a.split())))
    return score


def match_steam(title: str) -> tuple[dict | None, float]:
    key = normalize(title)
    if key in APPID_OVERRIDES:
        appid = APPID_OVERRIDES[key]
        return ({"id": appid, "title": title} if appid else None), 1.0
    query = key
    for wrong, right in QUERY_FIXES.items():
        query = query.replace(wrong, right)
    try:
        results = catalog.search_steam(query, limit=10)
    except requests.RequestException:
        return None, 0.0
    best, best_score = None, 0.0
    for r in results:
        s = similarity(query, r["title"])
        if s > best_score:
            best, best_score = r, s
    return (best, best_score) if best_score >= 0.6 else (None, best_score)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    text = Path(args.file).read_text(encoding="utf-8-sig")
    entries = [e for i, line in enumerate(text.splitlines(), 1) if (e := parse_line(line, i))]
    entries = merge_duplicates(entries)

    conn = None
    if not args.dry_run:
        init_db(DB_PATH)
        conn = sqlite3.connect(DB_PATH)

    # created_at растёт по порядку списка, чтобы сортировка «по добавлению» его повторяла
    base_time = datetime.now() - timedelta(minutes=len(entries))
    unmatched = []
    for n, e in enumerate(entries):
        hit, score = match_steam(e.title)
        mark = f"{hit['title']} [{hit['id']}] ({score:.2f})" if hit else f"— не найдено ({score:.2f})"
        extra = f" | {e.completed_on or ''} ×{e.times} {e.percent or ''}% {'; '.join(e.notes)}"
        print(f"{e.line_no:3}. {e.title:50} → {mark}{extra}")
        if not hit:
            unmatched.append(e.title)
        time.sleep(0.3)  # не дёргаем Steam слишком часто

        if conn is None:
            continue
        if conn.execute("SELECT 1 FROM games WHERE title = ?", (e.title,)).fetchone():
            print("     уже в библиотеке, пропускаю")
            continue
        appid = hit["id"] if hit else None
        info, cover = {}, None
        if appid:
            try:
                info = catalog.steam_details(appid)
            except requests.RequestException:
                pass
            cover = catalog.download_cover(
                [catalog.steam_cover_url(appid), info.get("header_image")], COVERS_DIR)
            # одна страница Steam на две записи (например, эпизоды HITMAN):
            # вторая получает обложку, но без привязки к Steam
            if conn.execute("SELECT 1 FROM games WHERE steam_appid = ?", (appid,)).fetchone():
                appid = None
        conn.execute(
            """INSERT INTO games(title, steam_appid, cover, release_year, developer, genres,
                                 completion_percent, completed_on, times_completed, note, created_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (e.title, appid, cover, info.get("release_year"),
             info.get("developer"), info.get("genres"), e.percent, e.completed_on, e.times,
             "; ".join(e.notes) or None,
             (base_time + timedelta(minutes=n)).isoformat(sep=" ", timespec="seconds")),
        )
        conn.commit()

    print(f"\nВсего игр: {len(entries)}, без совпадения в Steam: {len(unmatched)}")
    for t in unmatched:
        print("  -", t)
    if conn:
        conn.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
