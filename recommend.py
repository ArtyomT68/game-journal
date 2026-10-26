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
import re
import sqlite3
import threading
import time
from datetime import datetime

import requests

STEAMSPY = "https://steamspy.com/api.php"
REQUEST_PAUSE = 1.0          # SteamSpy: не чаще раза в секунду
TOP_TAGS_PER_GAME = 15
PROFILE_TAGS_FOR_SEARCH = 8  # по скольким любимым тегам искать кандидатов
CANDIDATES_TO_CHECK = 60     # сколько лучших кандидатов проверять по полным тегам
RESULTS = 30
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
    fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS recommendations (
    appid      INTEGER PRIMARY KEY,
    name       TEXT,
    score      REAL,
    rating     REAL,        -- доля положительных отзывов
    reviews    INTEGER,
    tags       TEXT,        -- JSON: общие с профилем теги
    similar    TEXT,        -- JSON: мои игры, на которые похожа
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS reco_hidden (
    appid INTEGER PRIMARY KEY
);
"""


def init(conn: sqlite3.Connection):
    conn.executescript(SCHEMA)


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

def _spy(params: dict) -> dict:
    time.sleep(REQUEST_PAUSE)
    resp = requests.get(STEAMSPY, params=params, timeout=60)
    resp.raise_for_status()
    return resp.json()


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
    conn = sqlite3.connect(db_path)
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
        search_tags = list(profile)[:PROFILE_TAGS_FOR_SEARCH]
        for i, tag in enumerate(search_tags, 1):
            progress(f"Ищу игры по тегу «{tag_ru(tag)}» ({i} из {len(search_tags)})")
            try:
                listing = _spy({"request": "tag", "tag": tag})
            except (requests.RequestException, ValueError):
                continue
            for item in listing.values():
                appid = int(item["appid"])
                pos, neg = item.get("positive") or 0, item.get("negative") or 0
                if (appid in have or appid in hidden or pos + neg < MIN_REVIEWS
                        or pos / (pos + neg) < MIN_POSITIVE or NOT_A_GAME.search(item.get("name") or "")):
                    continue
                c = candidates.setdefault(appid, {"name": item["name"], "pos": pos, "neg": neg, "coarse": 0.0})
                c["coarse"] += profile[tag]

        def rating(c):
            return c["pos"] / (c["pos"] + c["neg"])

        shortlist = sorted(candidates.items(), key=lambda kv: -kv[1]["coarse"] * rating(kv[1]))[:CANDIDATES_TO_CHECK]

        results = []
        for i, (appid, c) in enumerate(shortlist, 1):
            progress(f"Сравниваю кандидатов с твоим вкусом: {i} из {len(shortlist)}")
            info = game_tags(conn, appid)
            if not info:
                continue
            vec = tag_vector(info["tags"])
            sim = cosine(vec, profile)
            if sim <= 0:
                continue
            r = rating(c)
            score = sim * (0.7 + 0.3 * (r - MIN_POSITIVE) / (1 - MIN_POSITIVE))
            shared = sorted((t for t in vec if t in profile), key=lambda t: -vec[t] * profile[t])[:4]
            similar = pick_diverse(sorted(seeds, key=lambda s: -cosine(vec, s[1]) * s[2]),
                                   key=lambda s: franchise(s[0]), per_group=1)[:2]
            results.append((appid, c["name"], score, r, c["pos"] + c["neg"],
                            json.dumps([tag_ru(t) for t in shared], ensure_ascii=False),
                            json.dumps([s[0] for s in similar], ensure_ascii=False)))

        results.sort(key=lambda x: -x[2])
        results = pick_diverse(results, key=lambda r: franchise(r[1]))  # не больше двух игр одной серии
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute("DELETE FROM recommendations")
        conn.executemany(
            "INSERT INTO recommendations(appid, name, score, rating, reviews, tags, similar, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            [(*r, now) for r in results[:RESULTS]],
        )
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('reco_profile', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps([[tag_ru(t), round(w, 4)] for t, w in list(profile.items())[:12]], ensure_ascii=False),),
        )
        conn.commit()
        _set_status(conn, "done", f"Готово: {min(len(results), RESULTS)} рекомендаций.")
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

    with sqlite3.connect(db_path) as conn:
        init(conn)
        _set_status(conn, "running", "Запускаю расчёт…")
    threading.Thread(target=run, daemon=True).start()
    return True


def is_running() -> bool:
    return _lock.locked()
