"""Game Journal — журнал пройденных игр.

Запуск:  .venv\\Scripts\\python app.py   → http://localhost:5000
"""
import json
import os
import re
import secrets
import sqlite3
import uuid
from datetime import date
from pathlib import Path

import requests
from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, url_for)

import catalog
import estimates
import recommend
import steam_sync
import story
from db import close_db, get_db, get_setting, init_db, set_setting

BASE_DIR = Path(__file__).resolve().parent
COVERS_DIR = BASE_DIR / "static" / "covers"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

SORTS = {
    "date": "completed_on IS NULL, completed_on DESC, created_at DESC",
    "added": "created_at DESC",
    "title": "title COLLATE NOCASE",
    "playtime": "playtime_minutes IS NULL, playtime_minutes DESC",
    "percent": "completion_percent IS NULL, completion_percent DESC",
}
SORT_LABELS = {
    "date": "дата прохождения", "added": "дата добавления", "title": "название",
    "playtime": "время", "percent": "процент",
}
STATUSES = ("completed", "playing", "wishlist")
STATUS_PAGES = {"completed": "index", "playing": "playing", "wishlist": "wishlist"}


def hundred_scores(game) -> tuple[int | None, int | None]:
    """Интерес и гринд: у пройденных — своё впечатление, у остальных — оценка по данным из интернета."""
    if game["status"] == "completed":
        return game["hundred_fun"], game["hundred_grind"]
    return game["auto_fun"], game["auto_grind"]


def hundred_verdict(game) -> dict | None:
    """Итог по 100% достижений из пары «интерес — гринд»."""
    fun, grind = hundred_scores(game)
    if not (fun and grind):
        return None
    score = fun - grind
    if score >= 2 or (score == 1 and fun >= 4):
        return {"cls": "good", "text": "Стоит закрыть на 100%"}
    if score >= 0 or (score == -1 and fun >= 4):
        return {"cls": "mid", "text": "На любителя"}
    return {"cls": "bad", "text": "Больше гринда, чем удовольствия"}


def refresh_estimate(db, game_id: int, fresh: bool = False):
    """Пересчитывает оценку 100% по данным из интернета и сохраняет её."""
    game = db.execute("SELECT title, release_year, steam_appid FROM games WHERE id = ?", (game_id,)).fetchone()
    values = estimates.estimate(game["title"], game["release_year"], game["steam_appid"],
                                get_setting("steam_api_key"), fresh=fresh)
    sets = ", ".join(f"{k} = ?" for k in values)
    db.execute(f"UPDATE games SET {sets} WHERE id = ?", (*values.values(), game_id))
    db.commit()


def rarity_label(rarest: float | None) -> str | None:
    """Насколько сложно получить 100% — по доле игроков с самым редким достижением."""
    if rarest is None:
        return None
    if rarest >= 20:
        return "легко"
    if rarest >= 5:
        return "средне"
    if rarest >= 1:
        return "сложно"
    return "очень сложно"


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["DATABASE"] = Path(os.environ.get("GAME_JOURNAL_DB") or BASE_DIR / "data" / "games.db")
    app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
    init_db(app.config["DATABASE"])
    with sqlite3.connect(app.config["DATABASE"]) as conn:
        recommend.init(conn)
        story.init(conn)
    app.secret_key = _load_secret_key(app.config["DATABASE"])
    app.teardown_appcontext(close_db)
    register_filters(app)
    register_routes(app)
    return app


def _load_secret_key(db_path: Path) -> str:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = 'secret_key'").fetchone()
        if row:
            return row[0]
        key = secrets.token_hex(32)
        conn.execute("INSERT INTO settings(key, value) VALUES('secret_key', ?)", (key,))
        return key


# ---------------------------------------------------------------- helpers

def parse_playtime(text: str) -> int | None:
    """'61 ч 24 мин', '61:24', '61.5', '61' → минуты."""
    text = (text or "").strip().lower().replace(",", ".")
    if not text:
        return None
    if m := re.fullmatch(r"(\d+):(\d{1,2})", text):
        return int(m[1]) * 60 + int(m[2])
    hours = re.search(r"(\d+(?:\.\d+)?)\s*(?:ч|h)", text)
    minutes = re.search(r"(\d+)\s*(?:м|m)", text)
    if hours or minutes:
        return round(float(hours[1]) * 60 if hours else 0) + (int(minutes[1]) if minutes else 0)
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return round(float(text) * 60)
    raise ValueError(f"Не понимаю время «{text}». Пример: 61 ч 24 мин, 61:24 или 61.5")


def parse_int(text: str, lo: int, hi: int, field: str) -> int | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        value = int(text)
    except ValueError:
        raise ValueError(f"Поле «{field}» должно быть числом.") from None
    if not lo <= value <= hi:
        raise ValueError(f"Поле «{field}» должно быть от {lo} до {hi}.")
    return value


def parse_date(text: str, what: str = "прохождения") -> str | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise ValueError(f"Неверная дата {what}.") from None


def save_uploaded_cover(file) -> str | None:
    if not file or not file.filename:
        return None
    ext = Path(file.filename).suffix.lower()
    if ext not in IMAGE_EXTS:
        raise ValueError("Обложка должна быть картинкой (jpg, png, webp, gif).")
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}{ext}"
    file.save(COVERS_DIR / name)
    return name


def remove_cover_file(cover: str | None):
    if cover and not cover.startswith("http"):
        (COVERS_DIR / cover).unlink(missing_ok=True)


def library_stats(db) -> dict:
    row = db.execute(
        """SELECT COUNT(*) AS total,
                  COALESCE(SUM(playtime_minutes), 0) AS minutes,
                  COUNT(playtime_minutes) AS timed,
                  SUM(completion_percent = 100) AS full,
                  SUM(strftime('%Y', completed_on) = strftime('%Y', 'now')) AS this_year
           FROM games WHERE status = 'completed'"""
    ).fetchone()
    return {
        "total": row["total"],
        "minutes": row["minutes"],
        "avg_minutes": row["minutes"] // row["timed"] if row["timed"] else 0,
        "full": row["full"] or 0,
        "this_year": row["this_year"] or 0,
        "year": date.today().year,
    }


# ---------------------------------------------------------------- filters

def register_filters(app: Flask):
    @app.template_filter("playtime")
    def fmt_playtime(minutes, short=False):
        if minutes is None:
            return "—"
        h, m = divmod(int(minutes), 60)
        if short or not m:
            return f"{h:,} ч".replace(",", " ")
        return f"{h} ч {m} мин" if h else f"{m} мин"

    @app.template_filter("ru_date")
    def fmt_date(value):
        if not value:
            return "—"
        return date.fromisoformat(value).strftime("%d.%m.%Y")

    app.add_template_global(hundred_verdict)
    app.add_template_global(hundred_scores)
    app.add_template_global(rarity_label)

    @app.template_filter("cover_url")
    def cover_url(cover):
        if not cover:
            return None
        return cover if cover.startswith("http") else url_for("static", filename=f"covers/{cover}")


# ---------------------------------------------------------------- routes

def register_routes(app: Flask):

    @app.context_processor
    def nav_counts():
        counts = dict(get_db().execute(
            "SELECT status, COUNT(*) FROM games WHERE status != 'completed' GROUP BY status").fetchall())
        return {"playing_count": counts.get("playing", 0), "wishlist_count": counts.get("wishlist", 0)}

    @app.get("/")
    def index():
        db = get_db()
        q = request.args.get("q", "").strip()
        flt = request.args.get("filter", "all")
        sort = request.args.get("sort", "date")
        where, params = ["status = 'completed'"], []
        if q:
            where.append("title LIKE ?")
            params.append(f"%{q}%")
        if flt == "100":
            where.append("completion_percent = 100")
        elif flt == "replayed":
            where.append("times_completed > 1")
        elif flt == "year":
            where.append("strftime('%Y', completed_on) = strftime('%Y', 'now')")
        sql = "SELECT * FROM games WHERE " + " AND ".join(where)
        sql += " ORDER BY " + SORTS.get(sort, SORTS["date"])
        games = db.execute(sql, params).fetchall()
        return render_template(
            "index.html", games=games, stats=library_stats(db),
            q=q, flt=flt, sort=sort, sort_labels=SORT_LABELS,
            steam_ready=bool(get_setting("steam_api_key") and get_setting("steam_id")),
        )

    @app.get("/timeline")
    def timeline():
        rows = get_db().execute(
            "SELECT * FROM games WHERE status = 'completed' "
            "ORDER BY completed_on IS NULL, completed_on DESC, title"
        ).fetchall()
        years: dict[str, list] = {}
        for g in rows:
            years.setdefault(g["completed_on"][:4] if g["completed_on"] else "Без даты", []).append(g)
        return render_template("timeline.html", years=years)

    @app.get("/playing")
    def playing():
        games = get_db().execute(
            "SELECT * FROM games WHERE status = 'playing' "
            "ORDER BY started_on IS NULL, started_on DESC, created_at DESC"
        ).fetchall()
        return render_template("playing.html", games=games)

    @app.get("/wishlist")
    def wishlist():
        games = get_db().execute(
            "SELECT * FROM games WHERE status = 'wishlist' ORDER BY wish_rank IS NULL, wish_rank, created_at"
        ).fetchall()
        return render_template("wishlist.html", games=games)

    @app.get("/recommendations")
    def recommendations():
        db = get_db()
        status = recommend.get_status(db)
        # сервер перезапускали во время расчёта — расчёт прервался
        if status["state"] == "running" and not recommend.is_running():
            status = {"state": "error", "text": "Расчёт прервался (сайт перезапускали). Нажми «Обновить рекомендации»."}
        recs = db.execute(
            """SELECT r.* FROM recommendations r
               WHERE r.appid NOT IN (SELECT steam_appid FROM games WHERE steam_appid IS NOT NULL)
                 AND r.appid NOT IN (SELECT appid FROM reco_hidden)
               ORDER BY r.score DESC"""
        ).fetchall()
        profile = json.loads(get_setting("reco_profile") or "[]")
        return render_template(
            "recommendations.html", recs=recs, status=status, profile=profile,
            cover_url=catalog.steam_cover_url, loads=json.loads,
            year_filters=recommend.YEAR_FILTERS, year_filter=recommend.year_filter(db),
        )

    @app.post("/recommendations/years")
    def recommendations_years():
        """Меняет фильтр по годам выхода и сразу пересчитывает рекомендации."""
        value = request.form.get("years", "all")
        if value in recommend.YEAR_FILTERS:
            set_setting("reco_years", value)
            if not recommend.start(app.config["DATABASE"]):
                flash("Фильтр сохранён — он применится после текущего расчёта.", "ok")
        return redirect(url_for("recommendations"))

    @app.post("/recommendations/refresh")
    def recommendations_refresh():
        if not recommend.start(app.config["DATABASE"]):
            flash("Рекомендации уже считаются — подожди немного.", "ok")
        return redirect(url_for("recommendations"))

    @app.post("/recommendations/<int:appid>/hide")
    def recommendations_hide(appid):
        db = get_db()
        db.execute("INSERT OR IGNORE INTO reco_hidden(appid) VALUES(?)", (appid,))
        db.commit()
        return redirect(url_for("recommendations"))

    @app.post("/games/<int:game_id>/move")
    def move_wish(game_id):
        """Сдвигает игру в списке хотелок на одну позицию вверх или вниз."""
        db = get_db()
        game = load_game(game_id)
        if game["status"] != "wishlist":
            abort(400)
        up = request.form.get("dir") == "up"
        neighbour = db.execute(
            f"""SELECT id, wish_rank FROM games
                WHERE status = 'wishlist' AND wish_rank {'<' if up else '>'} ?
                ORDER BY wish_rank {'DESC' if up else 'ASC'} LIMIT 1""",
            (game["wish_rank"],),
        ).fetchone()
        if neighbour:
            db.execute("UPDATE games SET wish_rank = ? WHERE id = ?", (neighbour["wish_rank"], game_id))
            db.execute("UPDATE games SET wish_rank = ? WHERE id = ?", (game["wish_rank"], neighbour["id"]))
            db.commit()
        return redirect(url_for("wishlist") + f"#g{game_id}")

    @app.post("/games/<int:game_id>/start")
    def start_game(game_id):
        game = load_game(game_id)
        db = get_db()
        db.execute(
            "UPDATE games SET status = 'playing', started_on = ?, wish_rank = NULL WHERE id = ?",
            (date.today().isoformat(), game_id),
        )
        db.commit()
        flash(f"«{game['title']}» — в «Сейчас прохожу». Удачной игры!", "ok")
        return redirect(url_for("playing"))

    # --- поиск и добавление

    @app.get("/api/search")
    def api_search():
        results, errors = search_catalog(request.args.get("q", "").strip())
        return jsonify(results=results, errors=errors)

    def search_catalog(q: str) -> tuple[list[dict], list[str]]:
        """Поиск в Steam (и RAWG, если есть ключ) с пометкой, есть ли игра уже у меня."""
        if len(q) < 2:
            return [], []
        results, errors = [], []
        try:
            results += catalog.search_steam(q)
        except requests.RequestException:
            errors.append("Steam Store недоступен")
        if rawg_key := get_setting("rawg_api_key"):
            try:
                steam_titles = {r["title"].lower() for r in results}
                results += [r for r in catalog.search_rawg(q, rawg_key)
                            if r["title"].lower() not in steam_titles]
            except requests.RequestException:
                errors.append("RAWG недоступен")

        mark_in_library(results)
        return results, errors

    def mark_in_library(results: list[dict]):
        """Помечает результаты поиска статусом игры, если она уже есть у меня."""
        db = get_db()
        have_steam = dict(db.execute("SELECT steam_appid, status FROM games WHERE steam_appid IS NOT NULL").fetchall())
        have_rawg = dict(db.execute("SELECT rawg_id, status FROM games WHERE rawg_id IS NOT NULL").fetchall())
        for r in results:
            have = have_steam if r["source"] == "steam" else have_rawg
            r["in_library"] = have.get(r["id"])

    # --- поиск по каталогу с карточкой-превью

    EXPLORE_SORTS = {"": "по совпадению", "Released_DESC": "сначала новые", "Reviews_DESC": "по отзывам"}

    @app.get("/explore")
    def explore():
        a = request.args
        f = {
            "q": a.get("q", "").strip(),
            "tag": a.get("tag", type=int),
            "tag2": a.get("tag2", type=int),
            "year_from": a.get("year_from", type=int),
            "year_to": a.get("year_to", type=int),
            "rating": a.get("rating", 0, type=int),
            # без названия ищем по жанру — без порога по отзывам в выдачу лезут тысячи безвестных игр
            "pop": a.get("pop", 0 if a.get("q") else 1000, type=int),
            "sort": a.get("sort", "") if a.get("sort", "") in EXPLORE_SORTS else "",
        }
        tags = [t for t in (f["tag"], f["tag2"]) if t]
        pop_default = 0 if f["q"] else 1000
        filtering = bool(tags or f["year_from"] or f["year_to"] or f["rating"] or f["sort"]
                         or f["pop"] != pop_default)
        results, errors = [], []

        if f["q"] and not filtering:
            results, errors = search_catalog(f["q"])
        elif f["q"] or tags or filtering:
            def keep(r):
                y = r.get("year")
                return ((not f["year_from"] or (y and y >= f["year_from"]))
                        and (not f["year_to"] or (y and y <= f["year_to"]))
                        and (not f["rating"] or (r.get("rating") or 0) >= f["rating"])
                        and (not f["pop"] or (r.get("reviews") or 0) >= f["pop"]))
            try:
                # фильтры по году и отзывам Steam не умеет — дочитываем страницы, пока не наберётся выдача
                for page in range(4):
                    batch = catalog.store_search(f["q"], tags=tags, sort=f["sort"], count=100, start=page * 100)
                    results += [r for r in batch if keep(r)]
                    if len(results) >= 40 or len(batch) < 100:
                        break
            except (requests.RequestException, ValueError):
                errors.append("Steam Store недоступен")
            results = results[:60]
            mark_in_library(results)

        all_tags = catalog.steam_tags()[:200]
        return render_template(
            "explore.html", q=f["q"], f=f, results=results, errors=errors, filtering=filtering,
            tag_options=sorted(all_tags, key=lambda t: t["name"].lower()), sorts=EXPLORE_SORTS,
            this_year=date.today().year,
        )

    @app.get("/api/suggest")
    def api_suggest():
        """Подсказки при вводе в поиске: первые совпадения из Steam."""
        q = request.args.get("q", "").strip()
        if len(q) < 2:
            return jsonify(results=[])
        try:
            results = catalog.search_steam(q, limit=8)
        except (requests.RequestException, ValueError):
            return jsonify(results=[])
        mark_in_library(results)
        return jsonify(results=[
            {"id": r["id"], "title": r["title"], "subtitle": r["subtitle"], "thumb": r["thumb_fallback"],
             "in_library": r["in_library"],
             "url": url_for("explore_game", source="steam", ext_id=r["id"], title=r["title"])}
            for r in results
        ])

    @app.get("/explore/<source>/<int:ext_id>")
    def explore_game(source, ext_id):
        """Карточка игры из каталога: время прохождения и оценка 100% — и для игр, которые уже есть у меня."""
        db = get_db()
        if source == "steam":
            mine = db.execute("SELECT * FROM games WHERE steam_appid = ?", (ext_id,)).fetchone()
        elif source == "rawg":
            mine = db.execute("SELECT * FROM games WHERE rawg_id = ?", (ext_id,)).fetchone()
        else:
            abort(404)

        info, rarity, cover = {}, None, None
        try:
            if source == "steam":
                info = catalog.steam_details(ext_id)
                cover = catalog.steam_cover_url(ext_id)
                rarity = catalog.steam_global_achievements(ext_id)
            elif key := get_setting("rawg_api_key"):
                info = catalog.rawg_details(ext_id, key)
                cover = info.get("cover")
        except requests.RequestException:
            flash("Каталог сейчас недоступен — попробуй позже.", "error")
        if not info.get("title"):
            info["title"] = request.args.get("title") or "Без названия"
        return render_template("explore_game.html", source=source, ext_id=ext_id, info=info,
                               cover=cover, rarity=rarity, mine=mine)

    @app.get("/api/estimate")
    def api_estimate():
        """Оценка 100% для карточки из поиска — грузится отдельно, HowLongToBeat отвечает несколько секунд."""
        appid = request.args.get("appid", type=int)
        values = estimates.estimate(request.args.get("title", ""), request.args.get("year", type=int),
                                    appid, get_setting("steam_api_key"))
        # игра уже есть у меня — сохраняем оценку, чтобы она была видна и в её карточке
        if appid:
            db = get_db()
            sets = ", ".join(f"{k} = ?" for k in values)
            db.execute(f"UPDATE games SET {sets} WHERE steam_appid = ?", (*values.values(), appid))
            db.commit()
        values["verdict"] = hundred_verdict({**values, "status": "wishlist"})
        return jsonify(values)

    def form_status() -> str:
        status = request.values.get("status", "completed")
        return status if status in STATUSES else "completed"

    def insert_game(db, status: str, **fields) -> int:
        """Добавляет игру; у начатых игр сразу проставляется дата начала."""
        fields["status"] = status
        if status == "playing":
            fields["started_on"] = date.today().isoformat()
        elif status == "wishlist":  # новая хотелка встаёт в конец очереди
            fields["wish_rank"] = db.execute(
                "SELECT COALESCE(MAX(wish_rank), 0) + 1 FROM games WHERE status = 'wishlist'").fetchone()[0]
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = db.execute(f"INSERT INTO games({cols}) VALUES({marks})", tuple(fields.values()))
        if fields.get("steam_appid"):
            db.execute("DELETE FROM sync_ignore WHERE appid = ?", (fields["steam_appid"],))
        db.commit()
        return cur.lastrowid

    @app.post("/games/add")
    def add_from_catalog():
        db = get_db()
        source = request.form.get("source")
        status = form_status()
        try:
            ext_id = int(request.form.get("id", ""))
        except ValueError:
            abort(400)

        if source == "steam":
            existing = db.execute("SELECT id FROM games WHERE steam_appid = ?", (ext_id,)).fetchone()
            if existing:
                return redirect(url_for("edit_game", game_id=existing["id"]))
            try:
                info = catalog.steam_details(ext_id)
            except requests.RequestException:
                info = {}
            title = info.get("title") or request.form.get("title") or f"Steam #{ext_id}"
            cover = catalog.download_cover(
                [catalog.steam_cover_url(ext_id), info.get("header_image")], COVERS_DIR)
            steam_appid, rawg_id = ext_id, None
        elif source == "rawg":
            key = get_setting("rawg_api_key")
            if not key:
                abort(400)
            try:
                info = catalog.rawg_details(ext_id, key)
            except requests.RequestException:
                info = {}
            title = info.get("title") or request.form.get("title") or f"RAWG #{ext_id}"
            cover = catalog.download_cover([info.get("cover")], COVERS_DIR)
            steam_appid, rawg_id = None, ext_id
        else:
            abort(400)

        game_id = insert_game(
            db, status, title=title, steam_appid=steam_appid, rawg_id=rawg_id, cover=cover,
            release_year=info.get("release_year"), developer=info.get("developer"),
            genres=info.get("genres"),
        )
        if steam_appid:
            steam_sync.store_global_achievements(db, game_id, steam_appid)
            db.commit()
        if status in ("wishlist", "playing"):  # у них показывается оценка 100% из интернета
            refresh_estimate(db, game_id)
        if status == "playing":
            flash(f"«{title}» добавлена в «Сейчас прохожу».", "ok")
        elif status == "wishlist":
            flash(f"«{title}» добавлена в хотелки.", "ok")
        else:
            flash(f"«{title}» добавлена. Заполни статистику прохождения.", "ok")
        return redirect(url_for("edit_game", game_id=game_id, new=1))

    @app.route("/games/new", methods=["GET", "POST"])
    def new_game():
        if request.method == "POST":
            title = request.form.get("title", "").strip()
            if not title:
                flash("Укажи название игры.", "error")
                return render_template("new_game.html", form=request.form, status=form_status())
            try:
                cover = save_uploaded_cover(request.files.get("cover_file"))
            except ValueError as exc:
                flash(str(exc), "error")
                return render_template("new_game.html", form=request.form, status=form_status())
            if not cover and (url := request.form.get("cover_url", "").strip()):
                cover = catalog.download_cover([url], COVERS_DIR) or url
            db = get_db()
            game_id = insert_game(
                db, form_status(), title=title, cover=cover,
                release_year=request.form.get("release_year") or None,
                developer=request.form.get("developer", "").strip() or None,
                genres=request.form.get("genres", "").strip() or None,
            )
            if form_status() in ("wishlist", "playing"):
                refresh_estimate(db, game_id)
            return redirect(url_for("edit_game", game_id=game_id, new=1))
        return render_template("new_game.html", form={}, status=form_status())

    # --- карточка игры

    def load_game(game_id: int):
        game = get_db().execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        if game is None:
            abort(404)
        return game

    @app.route("/games/<int:game_id>", methods=["GET", "POST"])
    def edit_game(game_id):
        game = load_game(game_id)
        is_playing = game["status"] == "playing"
        if request.method == "POST":
            f = request.form
            action = f.get("action")
            try:
                values = {
                    "title": f.get("title", "").strip() or game["title"],
                    "note": f.get("note", "").strip() or None,
                }
                # своё впечатление о 100% ставится только у пройденных и начатых игр
                if "hundred_fun" in f:
                    values["hundred_fun"] = parse_int(f.get("hundred_fun"), 1, 5, "Интерес к 100%")
                    values["hundred_grind"] = parse_int(f.get("hundred_grind"), 1, 5, "Гринд")
                    values["hundred_hours"] = parse_int(f.get("hundred_hours"), 0, 10000, "Время на 100%")
                # у хотелок нет полей статистики — не затираем то, что пришло из Steam
                if "playtime" in f:
                    values["playtime_minutes"] = parse_playtime(f.get("playtime"))
                if "completion_percent" in f:
                    values["completion_percent"] = parse_int(
                        f.get("completion_percent"), 0, 100, "Процент прохождения")
                if is_playing:
                    values["started_on"] = parse_date(f.get("started_on"), "начала")
                elif game["status"] == "completed":
                    times = parse_int(f.get("times_completed"), 0, 999, "Количество прохождений")
                    values["completed_on"] = parse_date(f.get("completed_on"))
                    values["times_completed"] = 1 if times is None else times
                new_cover = save_uploaded_cover(request.files.get("cover_file"))
            except ValueError as exc:
                flash(str(exc), "error")
                return render_template("game.html", game=game, form=f)

            today = date.today().isoformat()
            if action == "finish" and is_playing:
                values["status"] = "completed"
                values["completed_on"] = game["story_done_on"] or today
                # игру уже проходили раньше и начали заново — это ещё одно прохождение
                if game["completed_on"]:
                    values["times_completed"] = game["times_completed"] + 1
            elif action == "replay" and game["status"] == "completed":
                values["status"] = "playing"
                values["started_on"] = today
            elif action == "start" and game["status"] == "wishlist":
                values["status"] = "playing"
                values["started_on"] = today
                values["wish_rank"] = None

            db = get_db()
            if new_cover:
                remove_cover_file(game["cover"])
                values["cover"] = new_cover
            sets = ", ".join(f"{k} = ?" for k in values)
            db.execute(f"UPDATE games SET {sets} WHERE id = ?", (*values.values(), game_id))
            db.commit()

            if action == "estimate":
                refresh_estimate(db, game_id, fresh=True)
                flash("Оценка 100% обновлена по данным из интернета.", "ok")
                return redirect(url_for("edit_game", game_id=game_id))
            if action == "finish" and is_playing:
                flash(f"«{values['title']}» пройдена и перенесена в библиотеку. Проверь дату и статистику.", "ok")
                return redirect(url_for("edit_game", game_id=game_id))
            if values.get("status") == "playing":
                if not game["auto_checked_at"]:  # у начатых игр показывается оценка 100% из интернета
                    refresh_estimate(db, game_id)
                what = "перепрохождения" if action == "replay" else "игры"
                flash(f"«{values['title']}» — в «Сейчас прохожу». Удачного {what}!", "ok")
                return redirect(url_for("playing"))
            flash("Сохранено.", "ok")
            return redirect(url_for(STATUS_PAGES[game["status"]]))
        finale_info = None
        if game["steam_appid"]:
            finale_info = story.get_info(get_db(), get_setting("steam_api_key"), game["steam_appid"])
        return render_template("game.html", game=game, form=None, is_new=request.args.get("new"),
                               finale=finale_info, finale_labels=story.METHOD_LABELS)

    @app.post("/games/<int:game_id>/finale")
    def set_finale(game_id):
        """Финальное достижение выбрано вручную (или сброшено на автоопределение)."""
        game = load_game(game_id)
        if not game["steam_appid"]:
            abort(400)
        db = get_db()
        story.set_manual(db, game["steam_appid"], request.form.get("apiname") or None)
        key, steamid = get_setting("steam_api_key"), get_setting("steam_id")
        info = story.get_info(db, key, game["steam_appid"])
        finale = None
        if key and steamid:
            ach = steam_sync.achievements(key, steamid, game["steam_appid"])
            finale = story.finished_on(info, ach["times"]) if ach else None
        story_date, story_ach = finale or (None, None)
        db.execute("UPDATE games SET story_done_on = ?, story_ach = ? WHERE id = ?", (story_date, story_ach, game_id))
        if finale and game["status"] == "completed" and not game["completed_on"]:
            db.execute("UPDATE games SET completed_on = ? WHERE id = ?", (story_date, game_id))
        db.commit()
        if finale:
            flash(f"🏁 Достижение «{story_ach}» получено {date.fromisoformat(story_date).strftime('%d.%m.%Y')}.", "ok")
        else:
            flash("Финальное достижение сохранено. Пока оно не получено.", "ok")
        return redirect(url_for("edit_game", game_id=game_id) + "#finale")

    @app.post("/games/<int:game_id>/delete")
    def delete_game(game_id):
        game = load_game(game_id)
        db = get_db()
        db.execute("DELETE FROM games WHERE id = ?", (game_id,))
        if game["steam_appid"]:
            db.execute("INSERT OR IGNORE INTO sync_ignore(appid) VALUES(?)", (game["steam_appid"],))
        db.commit()
        remove_cover_file(game["cover"])
        flash(f"«{game['title']}» удалена.", "ok")
        return redirect(url_for(STATUS_PAGES[game["status"]]))

    # --- Steam

    @app.post("/steam/sync")
    def steam_sync_route():
        key, steamid = get_setting("steam_api_key"), get_setting("steam_id")
        if not (key and steamid):
            flash("Сначала укажи ключ Steam API и профиль в настройках.", "error")
            return redirect(url_for("settings"))
        try:
            stats = steam_sync.sync_library(get_db(), key, steamid)
        except steam_sync.SteamError as exc:
            flash(str(exc), "error")
        else:
            msg = f"Steam: обновлено игр — {stats['updated']}."
            if stats["not_owned"]:
                msg += f" Не найдено в аккаунте — {stats['not_owned']}."
            if stats["story_dated"]:
                msg += f" Точная дата прохождения по достижениям — у {stats['story_dated']}."
            flash(msg, "ok")
            if stats["added"]:
                flash(f"🏁 По достижениям найдены пройденные игры, их добавлено в библиотеку — "
                      f"{len(stats['added'])}: {', '.join(stats['added'])}.", "ok")
            if stats["story_flagged"]:
                flash(f"🏁 Похоже, сюжет уже пройден: {', '.join(stats['story_flagged'])} — "
                      f"открой карточку и нажми «Пройдено ✓».", "ok")
        # хотелки без оценки 100% (например, добавленные до появления оценок) — досчитываем
        db = get_db()
        for row in db.execute(
                "SELECT id FROM games WHERE status IN ('wishlist', 'playing') AND auto_checked_at IS NULL").fetchall():
            refresh_estimate(db, row["id"])
        return redirect(request.referrer or url_for("index"))

    @app.get("/import")
    def import_page():
        key, steamid = get_setting("steam_api_key"), get_setting("steam_id")
        owned, error = [], None
        if key and steamid:
            try:
                games = steam_sync.owned_games(key, steamid)
            except steam_sync.SteamError as exc:
                error = str(exc)
            else:
                have = {r[0] for r in get_db().execute(
                    "SELECT steam_appid FROM games WHERE steam_appid IS NOT NULL")}
                owned = sorted(
                    (g for g in games.values() if g["appid"] not in have and g.get("playtime_forever")),
                    key=lambda g: g["playtime_forever"], reverse=True,
                )
        return render_template("import.html", owned=owned, error=error,
                               configured=bool(key and steamid),
                               cover_url=catalog.steam_cover_url)

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        if request.method == "POST":
            key = request.form.get("steam_api_key", "").strip()
            profile = request.form.get("steam_id", "").strip()
            set_setting("steam_api_key", key)
            set_setting("rawg_api_key", request.form.get("rawg_api_key", "").strip())
            if key and profile:
                try:
                    profile = steam_sync.resolve_steamid(key, profile)
                except steam_sync.SteamError as exc:
                    flash(str(exc), "error")
                    return redirect(url_for("settings"))
            set_setting("steam_id", profile)
            flash("Настройки сохранены.", "ok")
            return redirect(url_for("settings"))
        return render_template(
            "settings.html",
            steam_api_key=get_setting("steam_api_key"),
            steam_id=get_setting("steam_id"),
            rawg_api_key=get_setting("rawg_api_key"),
        )


app = create_app()

def lan_address() -> str | None:
    """IP компьютера в домашней сети — чтобы открыть сайт с телефона."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # пакеты не отправляются, так узнаём адрес сетевой карты
            return s.getsockname()[0]
    except OSError:
        return None


def main():
    import threading
    import webbrowser

    from waitress import serve

    port = int(os.environ.get("GAME_JOURNAL_PORT", 5000))
    url = f"http://localhost:{port}"
    print(f"Game Journal запущен: {url}")
    if ip := lan_address():
        print(f"С телефона и других устройств в сети: http://{ip}:{port}")
    print("Не закрывай это окно, пока пользуешься сайтом. Остановить — Ctrl+C.\n")
    if not os.environ.get("GAME_JOURNAL_NO_BROWSER"):
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    # 0.0.0.0 — сайт доступен с других устройств в домашней сети
    serve(app, host="0.0.0.0", port=port, threads=8)


if __name__ == "__main__":
    main()
