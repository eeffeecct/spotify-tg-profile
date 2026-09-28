"""Ставит обложку текущего трека из Spotify на аватарку Telegram, название — в био,
🎧 в эмодзи-статус и красит профиль в цвет обложки. Управление — командами в «Избранном» (.help)."""
import asyncio
import colorsys
import io
import json
import logging
import os
import sys
import time
from collections import deque
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
import spotipy
from dotenv import load_dotenv
from PIL import Image
from pyrogram import Client, filters, raw, types
from pyrogram.errors import AuthKeyUnregistered, FloodWait, RPCError
from pyrogram.handlers import MessageHandler
from spotipy.oauth2 import SpotifyOAuth, SpotifyOauthError

BASE_DIR = Path(__file__).resolve().parent  # всё (сессия, токены, state) лежит рядом со скриптом
load_dotenv(BASE_DIR / ".env")

log = logging.getLogger("spotify_tg")


def env_flag(name: str, default: bool) -> bool:
    return os.getenv(name, str(int(default))).strip().lower() in ("1", "true", "yes", "on")


SPOTIFY_CACHE = BASE_DIR / ".cache"
STATE_FILE = BASE_DIR / "state.json"  # помним, что поменяли, и исходный профиль между перезапусками
BLACKLIST_FILE = BASE_DIR / "blacklist.txt"
LOG_FILE = BASE_DIR / "bot.log"
LOCK_FILE = BASE_DIR / "bot.lock"
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", 5))    # как часто спрашивать Spotify, сек
IDLE_TIMEOUT = float(os.getenv("IDLE_TIMEOUT", 60))     # через сколько после паузы вернуть профиль, сек
BIO_TEMPLATE = os.getenv("BIO_TEMPLATE", "🎧 {artist} — {title}")
BIO_MAX_LEN = int(os.getenv("BIO_MAX_LEN", 70))         # 70 обычный аккаунт, 140 с Premium
MIN_LISTEN = float(os.getenv("MIN_LISTEN", 5))          # трек должен проиграть столько секунд, чтобы попасть в профиль
# защита от флуда: не больше MAX_UPDATES смен профиля за UPDATE_WINDOW секунд.
# обычное прослушивание и редкие скипы — мгновенно, притормаживает только если листаешь без остановки
MAX_UPDATES = int(os.getenv("MAX_UPDATES", 5))
UPDATE_WINDOW = float(os.getenv("UPDATE_WINDOW", 300))
EMOJI_STATUS = env_flag("EMOJI_STATUS", True)           # 🎧 в статусе до конца трека (Premium)
EMOJI_STATUS_ID = os.getenv("EMOJI_STATUS_ID")          # id своего кастомного эмодзи, иначе ищем 🎧
PROFILE_COLOR = env_flag("PROFILE_COLOR", True)         # цвет профиля под обложку (Premium)


@dataclass
class Track:
    uri: str
    title: str
    artist: str
    cover_url: str | None
    progress: float  # сколько секунд трек уже играет
    duration: float
    artists: tuple[str, ...] = ()

    @property
    def bio(self) -> str:
        return fit_bio(BIO_TEMPLATE.format(title=self.title, artist=self.artist))

    @property
    def remaining(self) -> float:
        return max(self.duration - self.progress, 0)


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def fit_bio(text: str) -> str:
    # Telegram считает длину в UTF-16, эмодзи там занимает 2 символа
    if utf16_len(text) <= BIO_MAX_LEN:
        return text
    while utf16_len(text) > BIO_MAX_LEN - 1:
        text = text[:-1]
    return text.rstrip() + "…"


# ---------- цвет обложки ----------

def cover_color(image: bytes) -> tuple[int, int, int]:
    """Главный цвет обложки: яркие пиксели раскладываем по оттенкам и берём самый весомый.
    Так красная надпись на чёрном фоне даёт красный, а не чёрный."""
    img = Image.open(io.BytesIO(image)).convert("RGB").resize((64, 64))
    data = img.tobytes()
    pixels = [tuple(data[i:i + 3]) for i in range(0, len(data), 3)]
    bins = [[0.0, 0.0, 0.0, 0.0] for _ in range(12)]  # по 30° оттенка: вес, сумма r, g, b
    for r, g, b in pixels:
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if s < 0.25 or v < 0.2:  # серое/тёмное не считаем
            continue
        w = s * v
        acc = bins[int(h * 12) % 12]
        acc[0] += w
        acc[1] += r * w
        acc[2] += g * w
        acc[3] += b * w
    best = max(bins, key=lambda acc: acc[0])
    if best[0] < len(pixels) * 0.02:  # почти ч/б обложка
        avg = [sum(p[i] for p in pixels) // len(pixels) for i in range(3)]
        return avg[0], avg[1], avg[2]
    return round(best[1] / best[0]), round(best[2] / best[0]), round(best[3] / best[0])


def color_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    # цвета профиля в Telegram все приглушённые, поэтому сравниваем в первую очередь оттенок
    ha, sa, va = colorsys.rgb_to_hsv(*(c / 255 for c in a))
    hb, sb, vb = colorsys.rgb_to_hsv(*(c / 255 for c in b))
    if sa < 0.2:  # серая обложка — ищем серый цвет
        return sb * 4 + (va - vb) ** 2
    dh = min(abs(ha - hb), 1 - abs(ha - hb))  # оттенок по кругу, 0..0.5
    return dh ** 2 * 20 + (min(sa, 0.6) - min(sb, 0.6)) ** 2 + (va - vb) ** 2 * 0.3


def int_to_rgb(value: int) -> tuple[int, int, int]:
    return (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF


def nearest_profile_color(rgb: tuple[int, int, int], palette: dict[int, list[int]]) -> int:
    return min(palette, key=lambda cid: min(color_distance(rgb, int_to_rgb(c)) for c in palette[cid]))


# ---------- Spotify ----------

def make_spotify() -> spotipy.Spotify:
    auth = SpotifyOAuth(
        client_id=os.environ["SPOTIFY_CLIENT_ID"],
        client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
        redirect_uri=os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"),
        scope="user-read-currently-playing",
        cache_path=str(SPOTIFY_CACHE),
    )
    try:
        auth.get_access_token(as_dict=False)  # при первом запуске откроет браузер для входа
    except SpotifyOauthError as e:
        log.warning("Токен Spotify протух (%s) — нужно войти заново", e)
        SPOTIFY_CACHE.unlink(missing_ok=True)
        auth.get_access_token(as_dict=False)
    return spotipy.Spotify(auth_manager=auth)


def fetch_current_track(sp: spotipy.Spotify) -> Track | None:
    """Текущий трек или None, если ничего не играет (пауза, реклама, подкаст)."""
    data = sp.current_user_playing_track()
    if not data or not data.get("is_playing") or not data.get("item"):
        return None
    item = data["item"]
    images = (item.get("album") or {}).get("images") or []  # у локальных файлов обложки нет
    return Track(
        uri=item["uri"],
        title=item["name"],
        artist=", ".join(a["name"] for a in item.get("artists", [])),
        cover_url=images[0]["url"] if images else None,
        progress=(data.get("progress_ms") or 0) / 1000,
        duration=(item.get("duration_ms") or 0) / 1000,
        artists=tuple(a["name"] for a in item.get("artists", [])),
    )


def download(url: str) -> bytes:
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    return response.content


# ---------- Telegram ----------

def dump_emoji_status(status) -> dict | None:
    if isinstance(status, raw.types.EmojiStatus):
        return {"document_id": status.document_id}
    if isinstance(status, raw.types.EmojiStatusCollectible):  # статус-подарок
        return {"collectible_id": status.collectible_id}
    return None


def load_emoji_status(data: dict | None):
    if not data:
        return raw.types.EmojiStatusEmpty()
    if "collectible_id" in data:
        return raw.types.InputEmojiStatusCollectible(collectible_id=data["collectible_id"])
    return raw.types.EmojiStatus(document_id=data["document_id"])


def dump_profile_color(color) -> dict | None:
    if isinstance(color, raw.types.PeerColor):
        return {"color": color.color, "background_emoji_id": color.background_emoji_id}
    if isinstance(color, raw.types.PeerColorCollectible):  # цвет от подарка
        return {"collectible_id": color.collectible_id, "background_emoji_id": color.background_emoji_id}
    return None


class Profile:
    """Меняет аватарку/био/статус/цвет и удаляет только те фото, которые загрузил сам."""

    def __init__(self, app: Client):
        self.app = app
        self.state = self._load()
        self.emoji_status_id: int | None = None
        self.palette: dict[int, list[int]] = {}  # color_id -> цвета фона профиля

    @staticmethod
    def _load() -> dict:
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self):
        STATE_FILE.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), "utf-8")

    @staticmethod
    async def _call(func, *args, **kwargs):
        while True:
            try:
                return await func(*args, **kwargs)
            except FloodWait as e:
                log.warning("Telegram просит подождать %s с", e.value)
                await asyncio.sleep(e.value)

    async def _invoke(self, query):
        return await self._call(self.app.invoke, query)

    @property
    def track_uri(self) -> str | None:
        return self.state.get("track_uri")

    @property
    def active(self) -> bool:
        return bool(self.state.get("track_uri") or self.state.get("photo_unique_id"))

    @property
    def status_expiring(self) -> bool:
        # статус живёт с запасом после конца трека; пока музыка играет — продлеваем заранее,
        # иначе в промежутке Telegram покажет пустой статус (звезду Premium)
        until = self.state.get("status_until")
        return self.emoji_status_id is not None and until is not None and until - time.time() < POLL_INTERVAL * 2 + 10

    async def init(self):
        # если прошлый запуск завершился нормально — берём актуальный профиль;
        # если упал и в профиле висит трек — настоящие значения берём из state
        me = (await self._invoke(raw.functions.users.GetUsers(id=[raw.types.InputUserSelf()])))[0]
        if not self.active or "original_bio" not in self.state:
            chat = await self._call(self.app.get_chat, "me")
            self.state["original_bio"] = chat.bio or ""
            self.state["original_emoji_status"] = dump_emoji_status(me.emoji_status)
            self.state["original_profile_color"] = dump_profile_color(me.profile_color)
            self._save()

        if not me.premium and (EMOJI_STATUS or PROFILE_COLOR):
            log.warning("Нет Telegram Premium — эмодзи-статус и цвет профиля выключены")
            return

        if EMOJI_STATUS:
            if EMOJI_STATUS_ID:
                self.emoji_status_id = int(EMOJI_STATUS_ID)
            else:
                found = await self._invoke(raw.functions.messages.SearchCustomEmoji(emoticon="🎧", hash=0))
                ids = getattr(found, "document_id", None)
                self.emoji_status_id = ids[0] if ids else None
                if not ids:
                    log.warning("Не нашёл эмодзи 🎧 для статуса — укажи EMOJI_STATUS_ID в .env")

        if PROFILE_COLOR:
            colors = await self._invoke(raw.functions.help.GetPeerProfileColors(hash=0))
            self.palette = {
                o.color_id: list(o.colors.bg_colors)
                for o in getattr(colors, "colors", [])
                if not o.hidden and isinstance(o.colors, raw.types.help.PeerColorProfileSet)
            }

    async def _delete_our_photo(self):
        # храним unique_id, а не file_id: у file_id протухает file_reference после перезапуска
        unique_id = self.state.pop("photo_unique_id", None)
        self.state.pop("cover_url", None)
        if not unique_id:
            return
        try:
            async for p in self.app.get_chat_photos("me", limit=10):
                if p.big_photo_unique_id == unique_id:
                    await self._call(self.app.delete_profile_photos, p.big_file_id)
                    return
        except RPCError as e:
            log.warning("Не удалось удалить старую обложку: %s", e)

    async def _set_profile_color(self, color_id: int | None):
        if color_id == self.state.get("profile_color_id"):
            return
        original = self.state.get("original_profile_color") or {}
        if color_id is None:  # вернуть как было
            if "collectible_id" in original:
                color = raw.types.InputPeerColorCollectible(collectible_id=original["collectible_id"])
            elif original:
                color = raw.types.PeerColor(color=original["color"], background_emoji_id=original["background_emoji_id"])
            else:
                color = None
        else:  # свой узор на фоне профиля оставляем, меняем только цвет
            color = raw.types.PeerColor(color=color_id, background_emoji_id=original.get("background_emoji_id"))
        await self._invoke(raw.functions.account.UpdateColor(for_profile=True, color=color))
        if color_id is None:
            self.state.pop("profile_color_id", None)
        else:
            self.state["profile_color_id"] = color_id
        self._save()

    async def refresh_status(self, track: Track):
        if self.emoji_status_id is None:
            return
        # запас после конца трека покрывает переход к следующему; если бот упадёт — статус всё равно сам исчезнет
        until = int(time.time() + track.remaining + IDLE_TIMEOUT + 30)
        await self._invoke(raw.functions.account.UpdateEmojiStatus(
            emoji_status=raw.types.EmojiStatus(document_id=self.emoji_status_id, until=until)))
        self.state["status_until"] = until
        self._save()

    async def show(self, track: Track):
        if not track.cover_url:
            await self._delete_our_photo()
        elif track.cover_url != self.state.get("cover_url"):  # тот же альбом — аву и цвет не трогаем
            cover = await asyncio.to_thread(download, track.cover_url)
            photo = io.BytesIO(cover)
            photo.name = "cover.jpg"
            await self._call(self.app.set_profile_photo, photo=types.InputChatPhotoStatic(photo))
            # сначала ставим новую, потом удаляем старую — чтобы не мелькала настоящая ава
            new_photo = None
            async for p in self.app.get_chat_photos("me", limit=1):
                new_photo = p
            await self._delete_our_photo()
            if new_photo:
                self.state["photo_unique_id"] = new_photo.big_photo_unique_id
                self.state["cover_url"] = track.cover_url
            self._save()  # сразу, чтобы после падения не потерять, какую аву мы поставили

            if self.palette:
                await self._set_profile_color(nearest_profile_color(cover_color(cover), self.palette))

        await self._call(self.app.update_profile, bio=track.bio)
        await self.refresh_status(track)
        self.state["track_uri"] = track.uri
        self.state["track_title"] = f"{track.artist} — {track.title}"
        self._save()

    async def restore(self):
        if not self.active:
            return
        await self._delete_our_photo()
        await self._call(self.app.update_profile, bio=self.state.get("original_bio", ""))
        if "status_until" in self.state:
            await self._invoke(raw.functions.account.UpdateEmojiStatus(
                emoji_status=load_emoji_status(self.state.get("original_emoji_status"))))
            self.state.pop("status_until")
        if "profile_color_id" in self.state:
            await self._set_profile_color(None)
        self.state.pop("track_uri", None)
        self.state.pop("track_title", None)
        self._save()


# ---------- чёрный список ----------

class Blacklist:
    """blacklist.txt: по строке на запись — `spotify:track:…` (конкретный трек) или имя артиста.
    Всё после ` #` — комментарий. Файл можно править руками, бот перечитывает его на лету."""

    def __init__(self, path: Path):
        self.path = path
        self.entries: dict[str, str] = {}  # запись -> комментарий
        self._mtime: float | None = None

    def _reload(self):
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self.entries, self._mtime = {}, None
            return
        if mtime == self._mtime:
            return
        self.entries = {}
        for line in self.path.read_text("utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, _, comment = line.partition(" #")
            self.entries[key.strip()] = comment.strip()
        self._mtime = mtime

    def _write(self):
        lines = [f"{k}  # {c}" if c else k for k, c in self.entries.items()]
        self.path.write_text("\n".join(lines) + "\n" if lines else "", "utf-8")
        self._mtime = self.path.stat().st_mtime

    def _find(self, key: str) -> str | None:
        return next((k for k in self.entries if k.lower() == key.lower()), None)

    def matches(self, track: Track) -> bool:
        self._reload()
        return track.uri in self.entries or any(self._find(a) for a in track.artists)

    def add(self, key: str, comment: str = "") -> bool:
        self._reload()
        if self._find(key):
            return False
        self.entries[key] = comment
        self._write()
        return True

    def remove(self, key: str) -> bool:
        self._reload()
        found = self._find(key)
        if not found:
            return False
        del self.entries[found]
        self._write()
        return True

    def items(self) -> list[tuple[str, str]]:
        self._reload()
        return list(self.entries.items())


# ---------- бот: цикл + команды в «Избранном» ----------

HELP = """🎧 Команды (пиши в «Избранное»):
.status — что сейчас в профиле
.off — пауза: вернуть профиль и не трогать, пока не .on
.on — снова показывать музыку
.ban — скрыть текущий трек
.ban Имя артиста — скрыть артиста
.unban [Имя артиста] — убрать из чёрного списка (без имени — текущий трек)
.bans — чёрный список
.stop — вернуть профиль и выключить бота"""


class Bot:
    def __init__(self, app: Client, sp: spotipy.Spotify):
        self.app = app
        self.sp = sp
        self.profile = Profile(app)
        self.blacklist = Blacklist(BLACKLIST_FILE)
        self.lock = asyncio.Lock()  # цикл и команды не должны менять профиль одновременно
        self.stopped = asyncio.Event()
        self.current: Track | None = None  # что сейчас играет в Spotify
        self.idle_since: float | None = None
        self.throttled_uri: str | None = None  # чтобы писать в лог про лимит один раз на трек
        self.updates: deque[float] = deque()  # когда меняли профиль (для лимита MAX_UPDATES / UPDATE_WINDOW)

    @property
    def paused(self) -> bool:
        return self.profile.state.get("paused", False)

    async def tick(self) -> float:
        """Один опрос Spotify. Возвращает, через сколько секунд опросить снова."""
        loop = asyncio.get_running_loop()
        track = await asyncio.to_thread(fetch_current_track, self.sp)
        self.current = track
        async with self.lock:
            if self.paused:
                return POLL_INTERVAL
            if track is None or self.blacklist.matches(track):  # трек из чёрного списка = ничего не играет
                self.idle_since = self.idle_since or loop.time()
                if self.profile.active and loop.time() - self.idle_since >= IDLE_TIMEOUT:
                    log.info("Музыка не играет — возвращаю профиль")
                    await self.profile.restore()
                return POLL_INTERVAL

            self.idle_since = None
            while self.updates and loop.time() - self.updates[0] >= UPDATE_WINDOW:
                self.updates.popleft()
            rate_wait = UPDATE_WINDOW - (loop.time() - self.updates[0]) if len(self.updates) >= MAX_UPDATES else 0
            # быстро листаешь треки — профиль не дёргается: трек должен играть MIN_LISTEN секунд
            if track.uri != self.profile.track_uri and track.progress >= MIN_LISTEN and rate_wait <= 0:
                await self.profile.show(track)
                self.updates.append(loop.time())
                log.info("Сейчас играет: %s — %s", track.artist, track.title)
                return POLL_INTERVAL

            if self.profile.active and self.profile.status_expiring:
                await self.profile.refresh_status(track)  # длинная пауза между сменами, повтор трека
            if track.uri == self.profile.track_uri:
                return POLL_INTERVAL
            if rate_wait > 0 and self.throttled_uri != track.uri:
                self.throttled_uri = track.uri
                log.info("Слишком часто листаешь — обновлю профиль через %.0f с", rate_wait)
            # проснёмся ровно когда новый трек можно ставить
            wait = max(MIN_LISTEN - track.progress, rate_wait)
            return min(POLL_INTERVAL, max(wait + 0.2, 0.5))

    async def run(self):
        await self.profile.init()
        self.app.add_handler(MessageHandler(
            self.on_command, filters.chat("me") & filters.text & filters.regex(r"(?i)^\.[a-z]+\b")))
        log.info("Запущен, слежу за Spotify каждые %s с%s", POLL_INTERVAL, " (на паузе, .on — включить)" if self.paused else "")
        try:
            while not self.stopped.is_set():
                delay = POLL_INTERVAL
                try:
                    delay = await self.tick()
                except (spotipy.SpotifyException, SpotifyOauthError, requests.RequestException, RPCError) as e:
                    log.warning("Ошибка, попробую ещё раз: %s", e)
                except Exception:
                    log.exception("Неожиданная ошибка, продолжаю работать")
                try:
                    await asyncio.wait_for(self.stopped.wait(), delay)
                except TimeoutError:
                    pass
        finally:
            log.info("Выключаюсь — возвращаю профиль")
            try:
                async with self.lock:
                    await self.profile.restore()
            except Exception as e:
                log.error("Не удалось вернуть профиль (%s) — вернётся при следующем запуске", e)

    # ----- команды -----

    async def on_command(self, _, message):
        cmd, _, arg = message.text.strip().partition(" ")
        handler = {
            ".help": self.cmd_help, ".status": self.cmd_status, ".on": self.cmd_on, ".off": self.cmd_off,
            ".ban": self.cmd_ban, ".unban": self.cmd_unban, ".bans": self.cmd_bans, ".stop": self.cmd_stop,
        }.get(cmd.lower())
        if handler is None:  # обычная заметка с точкой — не наше дело
            return
        log.info("Команда: %s", message.text.strip())
        try:
            reply = await handler(arg.strip())
        except Exception as e:
            log.exception("Команда %s упала", cmd)
            reply = f"⚠️ Ошибка: {e}"
        try:
            await message.edit_text(reply)
        except RPCError as e:
            log.warning("Не удалось ответить на команду: %s", e)
        if cmd.lower() == ".stop":
            self.stopped.set()

    async def cmd_help(self, _):
        return HELP

    async def cmd_status(self, _):
        now = f"{self.current.artist} — {self.current.title}" if self.current else "ничего"
        if self.current and self.blacklist.matches(self.current):
            now += " (в чёрном списке)"
        shown = self.profile.state.get("track_title") or "ничего (профиль как обычно)"
        return (f"{'⏸ На паузе' if self.paused else '▶️ Работает'}\n"
                f"В Spotify: {now}\n"
                f"В профиле: {shown}\n"
                f"В чёрном списке: {len(self.blacklist.items())}\n\n.help — команды")

    async def cmd_on(self, _):
        if not self.paused:
            return "▶️ Бот и так работает"
        self.profile.state.pop("paused", None)
        self.profile._save()
        self.idle_since = None
        return "▶️ Включил — музыка снова будет в профиле"

    async def cmd_off(self, _):
        async with self.lock:
            self.profile.state["paused"] = True
            await self.profile.restore()
            self.profile._save()
        return "⏸ На паузе, профиль вернул как было. .on — включить"

    async def cmd_ban(self, arg):
        if arg:
            added = self.blacklist.add(arg)
            text = f"🚫 Артист «{arg}» больше не попадёт в профиль" if added else f"«{arg}» уже в чёрном списке"
        elif self.current:
            added = self.blacklist.add(self.current.uri, f"{self.current.artist} — {self.current.title}")
            text = (f"🚫 Трек «{self.current.artist} — {self.current.title}» больше не попадёт в профиль"
                    if added else "Этот трек уже в чёрном списке")
        else:
            return "Сейчас ничего не играет. Чтобы скрыть артиста: .ban Имя артиста"
        await self._hide_if_banned()
        return text

    async def cmd_unban(self, arg):
        if arg:
            return f"✅ «{arg}» убран из чёрного списка" if self.blacklist.remove(arg) else f"«{arg}» нет в чёрном списке"
        if not self.current:
            return "Сейчас ничего не играет. Чтобы вернуть артиста: .unban Имя артиста"
        if self.blacklist.remove(self.current.uri):
            return f"✅ Трек «{self.current.artist} — {self.current.title}» убран из чёрного списка"
        return "Этого трека нет в чёрном списке (если скрыт артист — .unban Имя артиста)"

    async def cmd_bans(self, _):
        items = self.blacklist.items()
        if not items:
            return "Чёрный список пуст. .ban — скрыть текущий трек, .ban Имя — артиста"
        lines = [f"• {c or k}" + (" (трек)" if k.startswith("spotify:track:") else "") for k, c in items]
        return "🚫 Чёрный список:\n" + "\n".join(lines)

    async def cmd_stop(self, _):
        return "⏹ Выключаюсь, профиль верну как было. Запустить снова — перезайти в Windows или запустить main.py"

    async def _hide_if_banned(self):
        # если в профиле прямо сейчас то, что забанили, — убираем сразу
        async with self.lock:
            shown = self.profile.track_uri
            if shown and (shown in dict(self.blacklist.items())
                          or (self.current and self.current.uri == shown and self.blacklist.matches(self.current))):
                await self.profile.restore()


# ---------- запуск ----------

async def run():
    sp = make_spotify()
    app = Client(
        os.getenv("TG_SESSION", "my_account"),
        api_id=int(os.environ["TG_API_ID"]),
        api_hash=os.environ["TG_API_HASH"],
        workdir=str(BASE_DIR),
    )
    async with app:
        await Bot(app, sp).run()


def setup_logging():
    handlers: list[logging.Handler] = [
        RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")]
    if sys.stderr:  # под pythonw (фоновый запуск) консоли нет
        sys.stderr.reconfigure(errors="replace")  # иероглифы в названиях не должны ронять логи
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%d.%m %H:%M:%S", handlers=handlers)
    logging.getLogger("pyrogram").setLevel(logging.WARNING)  # без «Connecting…» на каждый чих


def single_instance():
    """Не даём запустить второго бота на ту же сессию. Возвращает открытый lock-файл (держим до выхода)."""
    lock = open(LOCK_FILE, "w")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return None
    return lock


def main():
    setup_logging()
    lock = single_instance()
    if lock is None:
        log.error("Бот уже запущен (возможно, в фоне). Второй экземпляр не нужен — управляй им через .status/.stop в «Избранном»")
        return
    session = BASE_DIR / f"{os.getenv('TG_SESSION', 'my_account')}.session"
    if not sys.stdin and not session.exists():
        log.error("Нет сессии Telegram, а в фоне войти нельзя. Запусти один раз в консоли: .venv\\Scripts\\python main.py")
        return

    backoff = 10
    while True:
        started = time.monotonic()
        try:
            asyncio.run(run())
            return  # .stop
        except KeyboardInterrupt:
            return
        except AuthKeyUnregistered:
            log.error("Сессия Telegram больше не действует. Удали %s и запусти в консоли — попросит номер и код.", session.name)
            return
        except Exception:
            log.exception("Бот упал, перезапущу через %s с", backoff)
        if time.monotonic() - started > 600:  # долго работал — значит, сбой разовый
            backoff = 10
        time.sleep(backoff)
        backoff = min(backoff * 2, 300)


if __name__ == "__main__":
    main()
