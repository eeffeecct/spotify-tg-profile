# spotify_tg_bot

Пока играет музыка в Spotify — ставит обложку трека на аватарку Telegram, а «Исполнитель — Трек» в био.
С Telegram Premium ещё ставит 🎧 в эмодзи-статус (до конца трека) и красит профиль в цвет обложки.
Когда музыка на паузе дольше `IDLE_TIMEOUT` или скрипт остановлен — удаляет свою обложку и возвращает исходное био.
Твои настоящие аватарки не трогает, свой статус и цвет профиля вернутся как были.
Быстро листаешь треки — профиль не дёргается: трек должен играть `MIN_LISTEN` секунд, а менять профиль бот будет не больше `MAX_UPDATES` раз за `UPDATE_WINDOW` секунд.

## Запуск

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env   # и заполнить
.venv\Scripts\python main.py
```

- **Spotify**: в [Dashboard](https://developer.spotify.com/dashboard) → приложение → Settings → Redirect URIs добавить
  `http://127.0.0.1:8888/callback` (`localhost` Spotify больше не принимает). При первом запуске откроется браузер для входа.
- **Telegram**: при первом запуске попросит номер телефона и код, потом сессия хранится в `my_account.session`.
  Если долго не запускать, Telegram убивает сессию — тогда удалить `.session` и войти заново.

Остановка — `Ctrl+C`.
