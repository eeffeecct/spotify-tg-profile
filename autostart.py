"""Фоновый запуск бота без окна и автозапуск при входе в Windows.

    python autostart.py install   — добавить в автозагрузку и сразу запустить в фоне
    python autostart.py remove    — убрать из автозагрузки
    python autostart.py start     — запустить в фоне сейчас (без автозагрузки)
    python autostart.py status    — есть ли в автозагрузке

Остановить фонового бота — написать .stop в «Избранное» Telegram. Лог — bot.log.
"""
import os
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PYTHONW = BASE_DIR / ".venv" / "Scripts" / "pythonw.exe"
MAIN = BASE_DIR / "main.py"
SHORTCUT = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Spotify TG Bot.lnk"


def ps_quote(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def install():
    arguments = f'"{MAIN}"'
    script = (
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({ps_quote(SHORTCUT)}); "
        f"$s.TargetPath = {ps_quote(PYTHONW)}; "
        f"$s.Arguments = {ps_quote(arguments)}; "
        f"$s.WorkingDirectory = {ps_quote(BASE_DIR)}; "
        f"$s.Description = 'Spotify -> Telegram profile'; "
        f"$s.Save()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True)
    print(f"Добавлен в автозагрузку: {SHORTCUT}")
    start()


def remove():
    if SHORTCUT.exists():
        SHORTCUT.unlink()
        print("Убран из автозагрузки. Если бот сейчас работает — останови его командой .stop в «Избранном»")
    else:
        print("В автозагрузке его и не было")


def start():
    subprocess.Popen([str(PYTHONW), str(MAIN)], cwd=BASE_DIR,
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    print("Запущен в фоне. Проверить — .status в «Избранном», лог — bot.log")


def status():
    print("В автозагрузке" if SHORTCUT.exists() else "Не в автозагрузке")


if __name__ == "__main__":
    actions = {"install": install, "remove": remove, "start": start, "status": status}
    if os.name != "nt" or len(sys.argv) != 2 or sys.argv[1] not in actions:
        print(__doc__)
        sys.exit(1)
    if not PYTHONW.exists():
        sys.exit(f"Не нашёл {PYTHONW} — сначала создай .venv (см. README)")
    actions[sys.argv[1]]()
