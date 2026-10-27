"""Резервные копии: база и обложки в одном zip-архиве.

  - make_backup — архив со всеми данными (кнопка «Скачать резервную копию» в настройках);
  - restore     — восстановление из такого архива; перед этим текущие данные сохраняются
                  в data/backups/, так что откатиться можно всегда;
  - auto_backup — раз в день при запуске сайта, последние AUTO_KEEP копий в data/backups/.

Копия базы снимается через SQLite backup API — она целостная, даже если сайт в этот момент
пишет в базу (и в режиме WAL, где часть данных лежит в файле games.db-wal).
"""
import io
import json
import re
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from db import connect, init_db

AUTO_KEEP = 7
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
COVER_NAME = re.compile(r"^covers/([A-Za-z0-9_\-]+\.(?:jpg|jpeg|png|webp|gif))$", re.I)
MAX_DB_SIZE = 500 * 1024 * 1024


class BackupError(Exception):
    pass


def _snapshot(db_path: Path, target: Path):
    """Целостная копия базы в target."""
    src = connect(db_path)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def make_backup(db_path: Path, covers_dir: Path) -> bytes:
    """zip-архив: games.db, covers/*, manifest.json."""
    with tempfile.TemporaryDirectory() as tmp:
        snap = Path(tmp) / "games.db"
        _snapshot(db_path, snap)
        # closing: в Windows незакрытая база не даст удалить временную папку
        with closing(sqlite3.connect(snap)) as conn:
            games = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snap, "games.db")
            covers = [f for f in covers_dir.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXTS]
            for f in covers:
                z.write(f, f"covers/{f.name}", compress_type=zipfile.ZIP_STORED)  # картинки уже сжаты
            z.writestr("manifest.json", json.dumps({
                "app": "Game Journal", "created": datetime.now().isoformat(timespec="seconds"),
                "games": games, "covers": len(covers),
            }, ensure_ascii=False, indent=2))
        return buf.getvalue()


def write_backup(db_path: Path, covers_dir: Path, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(make_backup(db_path, covers_dir))
    tmp.replace(path)


def auto_backup(db_path: Path, covers_dir: Path, backups_dir: Path) -> Path | None:
    """Ежедневная копия: если сегодня ещё не делали — делаем, старые сверх AUTO_KEEP удаляем."""
    if not db_path.exists():
        return None
    target = backups_dir / f"auto-{date.today().isoformat()}.zip"
    if target.exists():
        return None
    write_backup(db_path, covers_dir, target)
    for old in sorted(backups_dir.glob("auto-*.zip"))[:-AUTO_KEEP]:
        old.unlink(missing_ok=True)
    return target


def list_backups(backups_dir: Path) -> list[dict]:
    if not backups_dir.exists():
        return []
    return [{"name": f.name, "size": f.stat().st_size,
             "created": datetime.fromtimestamp(f.stat().st_mtime)}
            for f in sorted(backups_dir.glob("*.zip"), key=lambda f: f.stat().st_mtime, reverse=True)]


def restore(db_path: Path, covers_dir: Path, backups_dir: Path, archive) -> dict:
    """Восстанавливает данные из архива. archive — файл (путь или поток) с zip.

    Порядок: проверить архив → сохранить текущие данные в backups_dir → заменить базу
    (через backup API, сайт при этом может работать) → обновить структуру базы до текущей
    версии → разложить обложки и убрать те, на которые новая база не ссылается.
    """
    try:
        z = zipfile.ZipFile(archive)
    except zipfile.BadZipFile:
        raise BackupError("Это не zip-архив резервной копии.") from None
    with z, tempfile.TemporaryDirectory() as tmp:
        names = z.namelist()
        if "games.db" not in names:
            raise BackupError("В архиве нет базы games.db — это не резервная копия Game Journal.")
        if z.getinfo("games.db").file_size > MAX_DB_SIZE:
            raise BackupError("База в архиве слишком большая.")
        uploaded = Path(tmp) / "games.db"
        with z.open("games.db") as src, open(uploaded, "wb") as dst:
            shutil.copyfileobj(src, dst)
        try:
            with closing(sqlite3.connect(uploaded)) as conn:
                if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise BackupError("База в архиве повреждена.")
                games = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
        except sqlite3.DatabaseError:
            raise BackupError("Файл games.db в архиве — не база Game Journal.") from None

        # страховка: текущие данные — в отдельный архив, чтобы можно было откатиться
        safety = backups_dir / f"before-restore-{datetime.now():%Y-%m-%d_%H-%M-%S}.zip"
        n = 2
        while safety.exists():  # два восстановления в одну секунду не должны затирать друг друга
            safety = safety.with_name(f"{safety.stem.split('~')[0]}~{n}.zip")
            n += 1
        write_backup(db_path, covers_dir, safety)

        src = sqlite3.connect(uploaded)
        dst = connect(db_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        init_db(db_path)  # копия могла быть сделана старой версией сайта

        covers_dir.mkdir(parents=True, exist_ok=True)
        restored = 0
        for name in names:
            m = COVER_NAME.match(name)  # только картинки из covers/, без путей вида ../
            if not m:
                continue
            with z.open(name) as src_f, open(covers_dir / m.group(1), "wb") as dst_f:
                shutil.copyfileobj(src_f, dst_f)
            restored += 1

    with closing(connect(db_path)) as conn:
        used = {r[0] for r in conn.execute("SELECT cover FROM games WHERE cover IS NOT NULL")}
    removed = 0
    for f in covers_dir.iterdir():
        if f.is_file() and f.suffix.lower() in IMAGE_EXTS and f.name not in used:
            f.unlink()
            removed += 1
    return {"games": games, "covers": restored, "removed": removed, "safety": safety.name}
