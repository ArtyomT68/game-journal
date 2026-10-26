"""Сборка портативной версии для Windows: Python и все библиотеки уже внутри.

Пользователь скачивает архив из GitHub Releases, распаковывает и запускает start.bat —
устанавливать Python не нужно.

Запуск (нужен любой Python 3.11+ с pip, интернет):
    python build_portable.py              # dist/GameJournal-dev-windows.zip
    python build_portable.py --version v1.2

Что делает:
  1. скачивает официальный «Windows embeddable package» Python с python.org;
  2. настраивает его ._pth-файл, чтобы он видел библиотеки и папку проекта;
  3. ставит зависимости из requirements.txt готовыми сборками (wheel) под этот Python;
  4. копирует код проекта и упаковывает всё в zip.
"""
import argparse
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

PY_VERSION = "3.13.9"
PY_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip"
PY_TAG = "".join(PY_VERSION.split(".")[:2])  # 3.13.9 → 313

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"
DIST = ROOT / "dist"
APP_NAME = "GameJournal"

# Что попадает в сборку. Личные данные (data/, обложки, import/) — никогда.
PROJECT_FILES = [
    "app.py", "catalog.py", "db.py", "estimates.py", "library_meta.py", "recommend.py", "steam_sync.py", "story.py", "import_txt.py",
    "requirements.txt", "start.bat", "README.md", "LICENSE",
]
PROJECT_DIRS = ["templates", "static"]


def step(text: str):
    print(f"\n==> {text}", flush=True)


def download_python(cache: Path) -> Path:
    archive = cache / f"python-{PY_VERSION}-embed-amd64.zip"
    if not archive.exists():
        step(f"Скачиваю Python {PY_VERSION} (embeddable)")
        cache.mkdir(parents=True, exist_ok=True)
        tmp = archive.with_suffix(".part")
        urllib.request.urlretrieve(PY_URL, tmp)
        tmp.rename(archive)
    return archive


def setup_python(archive: Path, target: Path):
    step("Распаковываю Python")
    with zipfile.ZipFile(archive) as z:
        z.extractall(target)
    # В embeddable-версии sys.path задаётся только этим файлом: добавляем библиотеки и папку
    # проекта (на уровень выше python/), иначе import app / flask не найдутся.
    pth = target / f"python{PY_TAG}._pth"
    pth.write_text(
        f"python{PY_TAG}.zip\n.\nLib\\site-packages\n..\nimport site\n",
        encoding="utf-8",
    )


def install_packages(site_packages: Path):
    step("Ставлю библиотеки из requirements.txt")
    site_packages.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-compile",
            "--target", str(site_packages),
            # готовые сборки именно под встроенный Python, а не под тот, что запускает сборку
            "--platform", "win_amd64", "--python-version", PY_VERSION.rsplit(".", 1)[0],
            "--implementation", "cp", "--only-binary=:all:",
            "-r", str(ROOT / "requirements.txt"),
        ],
        check=True,
    )
    for junk in site_packages.rglob("__pycache__"):
        shutil.rmtree(junk, ignore_errors=True)


def copy_project(target: Path):
    step("Копирую проект")
    for name in PROJECT_FILES:
        src = ROOT / name
        if src.exists():
            shutil.copy2(src, target / name)
        elif name != "LICENSE":
            sys.exit(f"Не найден файл проекта: {name}")
    for name in PROJECT_DIRS:
        shutil.copytree(
            ROOT / name, target / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    # скачанные обложки — личные данные, в сборку идёт только пустая папка
    covers = target / "static" / "covers"
    for f in covers.iterdir():
        if f.name != ".gitkeep":
            f.unlink() if f.is_file() else shutil.rmtree(f)


def make_zip(src: Path, version: str) -> Path:
    step("Упаковываю архив")
    DIST.mkdir(exist_ok=True)
    out = DIST / f"{APP_NAME}-{version}-windows.zip"
    out.unlink(missing_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(src.rglob("*")):
            z.write(f, Path(APP_NAME) / f.relative_to(src))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", default="dev", help="версия в имени архива, например v1.2")
    args = ap.parse_args()

    target = BUILD / APP_NAME
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    setup_python(download_python(BUILD / "cache"), target / "python")
    install_packages(target / "python" / "Lib" / "site-packages")
    copy_project(target)
    out = make_zip(target, args.version)
    print(f"\nГотово: {out}  ({out.stat().st_size / 1024 / 1024:.1f} МБ)")


if __name__ == "__main__":
    main()
