"""Рекомендации игр по библиотеке и хотелкам.

Как это работает:
  1. Для каждой моей игры из Steam берём пользовательские теги Steam (через SteamSpy):
     «Open World», «Story Rich», «Stealth» и т.д. с числом голосов.
  2. Складываем их в «профиль вкуса». Пройденные игры весят больше хотелок, а игры,
     которые я перепроходил, закрыл на 100% или оценил высоко, — ещё больше.
  3. По самым весомым тегам набираем кандидатов — популярные игры с хорошими отзывами,
     которых у меня ещё нет.
  4. Сравниваем теги каждого кандидата с профилем (косинусное сходство) и учитываем отзывы.
     Для каждой рекомендации показываем общие теги и мои игры, на которые она похожа.

SteamSpy разрешает примерно один запрос в секунду, поэтому теги игр кэшируются в базе:
первый расчёт занимает пару минут, следующие — секунды.
"""
import json
import math
import random
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

import catalog
from db import connect

STEAMSPY = "https://steamspy.com/api.php"
REQUEST_PAUSE = 1.0          # SteamSpy: не чаще раза в секунду
TOP_TAGS_PER_GAME = 15
PROFILE_TAGS_FIXED = 6       # самые любимые теги — по ним ищем всегда
PROFILE_TAGS_RANDOM = 4      # плюс несколько случайных из следующих — чтобы выдача менялась
CANDIDATES_TO_CHECK = 70     # сколько кандидатов проверять по полным тегам
NEW_PER_REFRESH = 25         # из них — не больше стольких новых (их теги грузятся из SteamSpy по секунде)
LISTING_TTL = 12 * 3600      # списки игр по тегам меняются редко — кэшируем на 12 часов
RESULTS = 30
SHOWN_PENALTY = 0.85         # показ в прошлой выдаче немного снижает место игры (разнообразие при обновлении)
JITTER = 0.08                # небольшая случайность в оценке: ±8%
MIN_MATCH = 0.40             # слабее совпадение с профилем — не рекомендуем, даже ради разнообразия

# Фильтр по годам выхода: (подпись, с какого года). None — без ограничения.
YEAR_FILTERS = {
    "all": ("любые годы", None),
    "2015": ("с 2015 года", 2015),
    "recent": ("последние 5 лет", datetime.now().year - 4),
}
# Грубая оценка года по номеру приложения в Steam (номера выдаются по порядку) —
# чтобы не проверять заведомо старые игры, когда включён фильтр по годам
APPID_YEARS = [(250_000, 2013), (400_000, 2015), (600_000, 2017), (900_000, 2018),
               (1_200_000, 2020), (1_600_000, 2021), (2_000_000, 2022), (2_400_000, 2023), (2_800_000, 2024)]
MIN_REVIEWS = 3000
MIN_POSITIVE = 0.75

# Теги, которые есть почти у всех игр и ничего не говорят о вкусе
GENERIC_TAGS = {
    "Singleplayer", "Multiplayer", "Great Soundtrack", "Classic", "Masterpiece", "Beautiful",
    "Free to Play", "Early Access", "Controller", "Moddable", "Co-op", "Online Co-Op",
    "PvP", "PvE", "Massively Multiplayer", "Remake", "Remaster", "Sequel", "Addictive",
    "Replay Value", "Family Friendly", "Cult Classic", "Funny", "Casual", "Short",
    "Steam Achievements", "Full controller support", "Steam Trading Cards", "Steam Cloud",
}
NOT_A_GAME = re.compile(r"soundtrack|\bOST\b|artbook|dedicated server|\bSDK\b|\bdemo\b|playtest|benchmark", re.I)

TAG_RU = {
    "Action": "Экшен", "Adventure": "Приключение", "Open World": "Открытый мир", "Story Rich": "Сильный сюжет",
    "Shooter": "Шутер", "FPS": "Шутер от 1-го лица", "Third-Person Shooter": "Шутер от 3-го лица",
    "Third Person": "От третьего лица", "First-Person": "От первого лица", "Stealth": "Стелс",
    "Horror": "Хоррор", "Survival Horror": "Survival horror", "Psychological Horror": "Психологический хоррор",
    "Atmospheric": "Атмосферная", "RPG": "RPG", "Action RPG": "Action RPG", "Action-Adventure": "Экшен-приключение",
    "Cinematic": "Кинематографичная", "Story": "Сюжет", "Choices Matter": "Важен выбор",
    "Multiple Endings": "Несколько концовок", "Narration": "Повествование", "Realistic": "Реалистичная",
    "Sci-fi": "Научная фантастика", "Fantasy": "Фэнтези", "Dark Fantasy": "Тёмное фэнтези",
    "Post-apocalyptic": "Постапокалипсис", "Survival": "Выживание", "Zombies": "Зомби", "War": "Война",
    "Military": "Военная", "World War II": "Вторая мировая", "Historical": "Историческая", "Crime": "Криминал",
    "Mystery": "Детектив", "Detective": "Детектив", "Puzzle": "Головоломка", "Platformer": "Платформер",
    "Parkour": "Паркур", "Hack and Slash": "Слэшер", "Souls-like": "Souls-like", "Difficult": "Сложная",
    "Violent": "Жестокая", "Gore": "Кровь", "Mature": "Для взрослых", "Female Protagonist": "Героиня",
    "Superhero": "Супергерои", "Comic Book": "Комиксы", "Racing": "Гонки", "Driving": "Вождение",
    "Fighting": "Файтинг", "Martial Arts": "Боевые искусства", "Western": "Вестерн", "Cyberpunk": "Киберпанк",
    "Dystopian": "Антиутопия", "Futuristic": "Футуристичная", "Space": "Космос", "Aliens": "Пришельцы",
    "Linear": "Линейная", "Exploration": "Исследование", "Sandbox": "Песочница", "Immersive Sim": "Immersive sim",
    "Tactical": "Тактика", "Strategy": "Стратегия", "Indie": "Инди", "Local Co-Op": "Локальный кооп",
    "Split Screen": "Разделённый экран", "Emotional": "Эмоциональная", "Drama": "Драма",
    "Dark": "Мрачная", "Thriller": "Триллер", "Spy": "Шпионы", "Heist": "Ограбления", "Ninja": "Ниндзя",
    "Samurai": "Самураи", "Medieval": "Средневековье", "Magic": "Магия", "Vampire": "Вампиры",
    "Mythology": "Мифология", "Character Customization": "Создание персонажа", "Loot": "Лут",
    "Crafting": "Крафт", "Base Building": "Строительство базы", "Hunting": "Охота", "Horses": "Лошади",
    "Nudity": "Обнажёнка", "Psychological": "Психологическая", "Interactive Fiction": "Интерактивное кино",
    "Bullet Time": "Замедление времени", "Destruction": "Разрушаемость", "Team-Based": "Командная",
    "Online Co-Op": "Онлайн-кооп", "Co-op Campaign": "Кооп-кампания", "Puzzle Platformer": "Головоломка-платформер",
    "Metroidvania": "Метроидвания", "Roguelike": "Рогалик", "Roguelite": "Рогалик", "Turn-Based": "Пошаговая",
    "Anime": "Аниме", "Cartoony": "Мультяшная", "Colorful": "Яркая", "Funny": "Смешная", "Comedy": "Комедия",
    "Political": "Политика", "Conspiracy": "Заговор", "Lore-Rich": "Богатый лор", "Great Soundtrack": "Саундтрек",
}

_lock = threading.Lock()


def tag_ru(tag: str) -> str:
    return TAG_RU.get(tag, tag)


# ---------------------------------------------------------------- база

SCHEMA = """
CREATE TABLE IF NOT EXISTS steam_tags (
    appid      INTEGER PRIMARY KEY,
    name       TEXT,
    tags       TEXT,        -- JSON {тег: голоса}
    positive   INTEGER,
    negative   INTEGER,
    fetched_at TEXT,
    release_year INTEGER    -- 0 — год неизвестен
);
CREATE TABLE IF NOT EXISTS recommendations (
    appid      INTEGER PRIMARY KEY,
    name       TEXT,
    score      REAL,
    rating     REAL,        -- доля положительных отзывов
    reviews    INTEGER,
    tags       TEXT,        -- JSON: общие с профилем теги
    similar    TEXT,        -- JSON: мои игры, на которые похожа
    release_year INTEGER,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS reco_hidden (
    appid INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS reco_shown (     -- сколько раз игра уже была в рекомендациях
    appid INTEGER PRIMARY KEY,
    times INTEGER NOT NULL DEFAULT 0
);
"""


def init(conn: sqlite3.Connection):
    conn.executescript(SCHEMA)
    # базы из прошлой версии: кэш тегов дополняем годом, рекомендации — просто пересоздаём
    if "release_year" not in {r[1] for r in conn.execute("PRAGMA table_info(steam_tags)")}:
        conn.execute("ALTER TABLE steam_tags ADD COLUMN release_year INTEGER")
    if "release_year" not in {r[1] for r in conn.execute("PRAGMA table_info(recommendations)")}:
        conn.execute("DROP TABLE recommendations")
        conn.executescript(SCHEMA)


def year_filter(conn) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = 'reco_years'").fetchone()
    return row[0] if row and row[0] in YEAR_FILTERS else "all"


def guess_year(appid: int) -> int:
    year = 2010
    for edge, y in APPID_YEARS:
        if appid >= edge:
            year = y
    return year


def release_year(conn, appid: int) -> int | None:
    """Год выхода из Steam Store (кэшируется рядом с тегами)."""
    row = conn.execute("SELECT release_year FROM steam_tags WHERE appid = ?", (appid,)).fetchone()
    if row and row[0] is not None:
        return row[0] or None
    time.sleep(0.3)  # Store API не любит частые запросы
    try:
        year = catalog.steam_details(appid).get("release_year") or 0
    except (requests.RequestException, ValueError):
        return None  # не сохраняем — попробуем в следующий раз
    conn.execute("UPDATE steam_tags SET release_year = ? WHERE appid = ?", (year, appid))
    conn.commit()
    return year or None


def production_factor(price_cents: int | None, mostly_big_games: bool) -> float:
    """Кто проходит в основном крупные игры, тому дешёвые поделки с тегом «Шутер» подходят хуже.
    Цена в Steam — грубый признак масштаба игры; для любителей инди не применяется."""
    if not mostly_big_games or price_cents is None:
        return 1.0
    if price_cents == 0:
        return 0.8
    return 0.82 if price_cents < 1500 else 0.92 if price_cents < 2500 else 1.0


def freshness(year: int | None) -> float:
    """Небольшой бонус новым играм — иначе классика с сотнями тысяч отзывов вытесняет всё."""
    if not year:
        return 1.0
    age = datetime.now().year - year
    return 1.15 if age <= 3 else 1.07 if age <= 6 else 1.0 if age <= 12 else 0.88


def _set_status(conn, state: str, text: str):
    conn.execute(
        "INSERT INTO settings(key, value) VALUES('reco_status', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (json.dumps({"state": state, "text": text, "at": datetime.now().isoformat(timespec="seconds")},
                    ensure_ascii=False),),
    )
    conn.commit()


def get_status(conn) -> dict:
    row = conn.execute("SELECT value FROM settings WHERE key = 'reco_status'").fetchone()
    return json.loads(row[0]) if row and row[0] else {"state": "never", "text": ""}


# ---------------------------------------------------------------- SteamSpy

_last_spy = 0.0
_listings: dict[str, tuple[float, dict]] = {}


def _spy(params: dict) -> dict:
    """Запрос к SteamSpy не чаще раза в секунду: ждём только остаток секунды с прошлого запроса
    (сам запрос тоже длится около секунды, так что лишних пауз почти нет)."""
    global _last_spy
    wait = REQUEST_PAUSE - (time.monotonic() - _last_spy)
    if wait > 0:
        time.sleep(wait)
    _last_spy = time.monotonic()
    resp = requests.get(STEAMSPY, params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()


def tag_listing(tag: str) -> dict:
    """Все игры с тегом (SteamSpy), с кэшем на 12 часов."""
    hit = _listings.get(tag)
    if hit and time.time() - hit[0] < LISTING_TTL:
        return hit[1]
    listing = _spy({"request": "tag", "tag": tag})
    _listings[tag] = (time.time(), listing)
    return listing


def _fetch_year(appid: int) -> int | None:
    """Год выхода из Steam Store без записи в базу — для параллельной загрузки."""
    try:
        return catalog.steam_details(appid).get("release_year") or 0
    except (requests.RequestException, ValueError):
        return None


def game_tags(conn, appid: int) -> dict | None:
    """Теги игры из кэша, при отсутствии — из SteamSpy (и сохранить)."""
    row = conn.execute("SELECT tags, name, positive, negative FROM steam_tags WHERE appid = ?", (appid,)).fetchone()
    if row:
        return {"tags": json.loads(row[0] or "{}"), "name": row[1], "positive": row[2], "negative": row[3]}
    try:
        data = _spy({"request": "appdetails", "appid": appid})
    except (requests.RequestException, ValueError):
        return None
    tags = data.get("tags") or {}
    if isinstance(tags, list):  # у игр без тегов SteamSpy отдаёт пустой список
        tags = {}
    conn.execute(
        "INSERT OR REPLACE INTO steam_tags(appid, name, tags, positive, negative, fetched_at) VALUES(?, ?, ?, ?, ?, ?)",
        (appid, data.get("name"), json.dumps(tags, ensure_ascii=False), data.get("positive"),
         data.get("negative"), datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    return {"tags": tags, "name": data.get("name"), "positive": data.get("positive"), "negative": data.get("negative")}


def tag_vector(tags: dict) -> dict[str, float]:
    """Топ тегов игры, нормированных к самому популярному (0–1), без общих тегов."""
    top = sorted(((t, v) for t, v in tags.items() if t not in GENERIC_TAGS), key=lambda x: -x[1])[:TOP_TAGS_PER_GAME]
    if not top:
        return {}
    peak = top[0][1] or 1
    return {t: v / peak for t, v in top}


def franchise(name: str) -> str:
    """Грубый ключ серии: «Half-Life 2: Episode One» и «Half-Life» → «half life»."""
    name = re.sub(r"[™®©]", "", name).lower()
    name = re.split(r"[:\-–—(]|\s\d|\s(?:i{1,3}|iv|v|vi{0,3})\b", name.replace("-", " "), maxsplit=1)[0]
    return " ".join(name.split()[:2])


def pick_diverse(items: list, key, per_group: int = 2) -> list:
    """Оставляет не больше per_group элементов с одинаковым ключом, сохраняя порядок."""
    seen: dict[str, int] = {}
    out = []
    for it in items:
        k = key(it)
        if seen.get(k, 0) < per_group:
            seen[k] = seen.get(k, 0) + 1
            out.append(it)
    return out


def shown_penalty(times: int) -> float:
    """Игра была в прошлой выдаче — опускаем её ниже, но не бесконечно (не больше двух ступеней)."""
    return SHOWN_PENALTY ** min(times, 2)


def tag_idf(conn) -> dict[str, float]:
    """Насколько тег редкий среди всех известных игр: «Экшен» есть почти у всех и мало что
    говорит, «Immersive sim» или «Стелс» — характерные. Считается по кэшу тегов."""
    df: dict[str, int] = {}
    rows = conn.execute("SELECT tags FROM steam_tags").fetchall()
    for (tags,) in rows:
        for t in tag_vector(json.loads(tags or "{}")):
            df[t] = df.get(t, 0) + 1
    n = len(rows)
    return {t: math.log((n + 1) / (d + 1)) + 0.5 for t, d in df.items()}


def similarity(vec: dict, profile: dict, idf: dict) -> float:
    """Пополам: обычное сходство по тегам и сходство, где редкие теги весят больше."""
    weigh = lambda v: {t: x * idf.get(t, 1.0) for t, x in v.items()}
    return 0.5 * cosine(vec, profile) + 0.5 * cosine(weigh(vec), weigh(profile))


def same_game_key(name: str) -> str:
    """Одна игра под разными номерами в Steam: «Fallout: New Vegas» и «… PCR», переиздания."""
    name = re.sub(r"[™®©]", "", name).lower()
    name = re.sub(r"\((?:\d{4}|classic)\)|game of the year edition|goty|definitive edition|remastered"
                  r"|maximum edition|complete edition|enhanced edition|\bpcr\b", "", name)
    return re.sub(r"[^a-zа-я0-9]+", " ", name).strip()


def cosine(a: dict, b: dict) -> float:
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def seed_weight(g: sqlite3.Row) -> float:
    """Насколько игра говорит о моём вкусе."""
    if g["status"] == "wishlist":
        return 0.7
    if g["status"] == "playing":
        return 0.8
    w = 1.0
    if (g["times_completed"] or 1) > 1:
        w += 0.5
    if g["completion_percent"] == 100:
        w += 0.3
    if g["hundred_fun"]:
        w += (g["hundred_fun"] - 3) * 0.25  # 5 → +0.5, 1 → −0.5
    return max(w, 0.2)


# ---------------------------------------------------------------- расчёт

def build_profile(conn, progress) -> tuple[dict[str, float], list[tuple[str, dict, float]]]:
    conn.row_factory = sqlite3.Row
    games = conn.execute("SELECT * FROM games WHERE steam_appid IS NOT NULL").fetchall()
    profile: dict[str, float] = {}
    seeds = []
    for i, g in enumerate(games, 1):
        progress(f"Собираю теги твоих игр: {i} из {len(games)}")
        info = game_tags(conn, g["steam_appid"])
        if not info:
            continue
        vec = tag_vector(info["tags"])
        w = seed_weight(g)
        for t, v in vec.items():
            profile[t] = profile.get(t, 0) + v * w
        seeds.append((g["title"], vec, w))
    total = sum(profile.values()) or 1
    return {t: v / total for t, v in sorted(profile.items(), key=lambda x: -x[1])}, seeds


def compute(db_path) -> None:
    """Полный расчёт рекомендаций; результат — в таблице recommendations."""
    conn = connect(db_path)
    init(conn)
    progress = lambda text: _set_status(conn, "running", text)
    try:
        profile, seeds = build_profile(conn, progress)
        if not profile:
            _set_status(conn, "error", "Нужны игры из Steam в библиотеке или хотелках — по ним строится профиль вкуса.")
            return

        have = {r[0] for r in conn.execute("SELECT steam_appid FROM games WHERE steam_appid IS NOT NULL")}
        hidden = {r[0] for r in conn.execute("SELECT appid FROM reco_hidden")}

        # кандидаты: популярные игры с хорошими отзывами по самым весомым тегам
        candidates: dict[int, dict] = {}
        # самые весомые теги + несколько случайных из следующих: так при каждом обновлении
        # в выборку попадают и другие игры
        tags = list(profile)
        extra = tags[PROFILE_TAGS_FIXED:PROFILE_TAGS_FIXED + 14]
        search_tags = tags[:PROFILE_TAGS_FIXED] + random.sample(extra, min(PROFILE_TAGS_RANDOM, len(extra)))
        min_year = YEAR_FILTERS[year_filter(conn)][1]
        shown = dict(conn.execute("SELECT appid, times FROM reco_shown").fetchall())
        # в библиотеке мало инди — значит, человек в основном играет в крупные игры
        mostly_big_games = sum(1 for _, vec, _ in seeds if vec.get("Indie", 0) >= 0.3) < 0.25 * len(seeds)
        for i, tag in enumerate(search_tags, 1):
            progress(f"Ищу игры по тегу «{tag_ru(tag)}» ({i} из {len(search_tags)})")
            try:
                listing = tag_listing(tag)
            except (requests.RequestException, ValueError):
                continue
            for item in listing.values():
                appid = int(item["appid"])
                pos, neg = item.get("positive") or 0, item.get("negative") or 0
                if (appid in have or appid in hidden or pos + neg < MIN_REVIEWS
                        or pos / (pos + neg) < MIN_POSITIVE or NOT_A_GAME.search(item.get("name") or "")
                        or (min_year and guess_year(appid) < min_year - 1)):
                    continue
                price = item.get("initialprice")
                c = candidates.setdefault(appid, {"name": item["name"], "pos": pos, "neg": neg, "coarse": 0.0,
                                                  "price": int(price) if str(price).isdigit() else None})
                c["coarse"] += profile[tag]

        def rating(c):
            return c["pos"] / (c["pos"] + c["neg"])

        def coarse(kv):
            appid, c = kv
            return (c["coarse"] * rating(c) * freshness(guess_year(appid))
                    * production_factor(c.get("price"), mostly_big_games)
                    * shown_penalty(shown.get(appid, 0)) * random.uniform(0.85, 1.15))

        # новые кандидаты дороги (теги из SteamSpy — по секунде на игру), поэтому за одно обновление
        # проверяем не больше NEW_PER_REFRESH новых, остальные — из кэша; кэш растёт с каждым разом
        cached = {r[0] for r in conn.execute("SELECT appid FROM steam_tags")}
        ranked = sorted(candidates.items(), key=lambda kv: -coarse(kv))
        fresh_ones = [kv for kv in ranked if kv[0] not in cached][:NEW_PER_REFRESH]
        known_ones = [kv for kv in ranked if kv[0] in cached][:CANDIDATES_TO_CHECK - len(fresh_ones)]
        shortlist = sorted(fresh_ones + known_ones, key=lambda kv: -coarse(kv))
        in_shortlist = {appid for appid, _ in shortlist}
        for appid, name, rating_, reviews in conn.execute(
                "SELECT appid, name, rating, reviews FROM recommendations").fetchall():
            if appid not in in_shortlist and appid not in have and appid not in hidden:
                pos = round(rating_ * reviews)
                shortlist.append((appid, {"name": name, "pos": pos, "neg": reviews - pos, "coarse": 0.0}))

        idf = tag_idf(conn)
        # годы выхода — из Steam Store, это другой сервис: грузим параллельно, пока идут запросы к SteamSpy
        no_year = [appid for appid, _ in shortlist if conn.execute(
            "SELECT 1 FROM steam_tags WHERE appid = ? AND release_year IS NOT NULL", (appid,)).fetchone() is None]
        pool = ThreadPoolExecutor(max_workers=4)
        years_ahead = {appid: pool.submit(_fetch_year, appid) for appid in no_year}
        results = []
        for i, (appid, c) in enumerate(shortlist, 1):
            progress(f"Сравниваю кандидатов с твоим вкусом: {i} из {len(shortlist)}")
            info = game_tags(conn, appid)
            if not info:
                continue
            vec = tag_vector(info["tags"])
            sim = similarity(vec, profile, idf)
            if sim < MIN_MATCH:
                continue
            if appid in years_ahead:
                year = years_ahead.pop(appid).result()
                if year is not None:
                    conn.execute("UPDATE steam_tags SET release_year = ? WHERE appid = ?", (year, appid))
                    conn.commit()
                year = year or None
            else:
                year = release_year(conn, appid)
            if min_year and year and year < min_year:
                continue
            r = rating(c)
            quality = sim * (0.7 + 0.3 * (r - MIN_POSITIVE) / (1 - MIN_POSITIVE))
            # «совпадение» на карточке — честное; порядок — с учётом новизны, прошлых показов и случайности
            order = (quality * freshness(year) * production_factor(c.get("price"), mostly_big_games)
                     * shown_penalty(shown.get(appid, 0)) * random.uniform(1 - JITTER, 1 + JITTER))
            shared = sorted((t for t in vec if t in profile), key=lambda t: -vec[t] * profile[t])[:4]
            similar = pick_diverse(sorted(seeds, key=lambda s: -cosine(vec, s[1]) * s[2]),
                                   key=lambda s: franchise(s[0]), per_group=1)[:2]
            results.append((order, (appid, c["name"], quality, r, c["pos"] + c["neg"],
                                    json.dumps([tag_ru(t) for t in shared], ensure_ascii=False),
                                    json.dumps([s[0] for s in similar], ensure_ascii=False), year)))

        pool.shutdown(wait=False, cancel_futures=True)
        results.sort(key=lambda x: -x[0])
        # не больше двух игр одной серии
        rows = pick_diverse([row for _, row in results], key=lambda r: same_game_key(r[1]), per_group=1)
        results = pick_diverse(rows, key=lambda r: franchise(r[1]))[:RESULTS]
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute("DELETE FROM recommendations")
        conn.executemany(
            "INSERT INTO recommendations(appid, name, score, rating, reviews, tags, similar, release_year, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(*r, now) for r in results],
        )
        # считаем показы подряд: игра, пропустившая обновление, снова «свежая»
        conn.execute(f"DELETE FROM reco_shown WHERE appid NOT IN ({','.join('?' * len(results)) or 'NULL'})",
                     [r[0] for r in results])
        conn.executemany(
            "INSERT INTO reco_shown(appid, times) VALUES(?, 1) ON CONFLICT(appid) DO UPDATE SET times = times + 1",
            [(r[0],) for r in results],
        )
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('reco_profile', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps([[tag_ru(t), round(w, 4)] for t, w in list(profile.items())[:12]], ensure_ascii=False),),
        )
        conn.commit()
        _set_status(conn, "done", f"Готово: {len(results)} рекомендаций.")
    except Exception as exc:  # фоновая задача: любую ошибку показываем на странице
        _set_status(conn, "error", f"Не удалось построить рекомендации: {exc}")
    finally:
        conn.close()


def start(db_path) -> bool:
    """Запускает расчёт в фоне. False — если расчёт уже идёт."""
    if not _lock.acquire(blocking=False):
        return False

    def run():
        try:
            compute(db_path)
        finally:
            _lock.release()

    with connect(db_path) as conn:
        init(conn)
        _set_status(conn, "running", "Запускаю расчёт…")
    threading.Thread(target=run, daemon=True).start()
    return True


def is_running() -> bool:
    return _lock.locked()
