"""Ставит обложку текущего трека из Spotify на аватарку Telegram, название — в био,
🎧 в эмодзи-статус и красит профиль в цвет обложки. Управление — командами в «Избранном» (.help)."""
import asyncio
import colorsys
import html
import io
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
import spotipy
from dotenv import load_dotenv
from PIL import Image
from pyrogram import Client, enums, filters, raw, types
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
EMOJI_RULES_FILE = BASE_DIR / "emoji_rules.txt"
LOG_FILE = BASE_DIR / "bot.log"
LOCK_FILE = BASE_DIR / "bot.lock"
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", 5))    # как часто спрашивать Spotify, сек
IDLE_TIMEOUT = float(os.getenv("IDLE_TIMEOUT", 60))     # через сколько после паузы вернуть профиль, сек
BIO_TEMPLATE = os.getenv("BIO_TEMPLATE", "🎧 {artist} — {title}")
BIO_MAX_LEN = int(os.getenv("BIO_MAX_LEN", 70))         # 70 обычный аккаунт, 140 с Premium
MIN_LISTEN = float(os.getenv("MIN_LISTEN", 5))          # трек должен проиграть столько секунд, чтобы попасть в профиль
# защита от флуда для текста/эмодзи/цвета: MAX_UPDATES смен подряд, дальше — по одной раз в UPDATE_REFILL секунд.
# обычное прослушивание и редкие скипы — мгновенно, притормаживает только если листаешь без остановки
MAX_UPDATES = int(os.getenv("MAX_UPDATES", 5))
UPDATE_REFILL = float(os.getenv("UPDATE_REFILL", 20))
# аватарок Telegram даёт мало (по опыту — блокировка на часы после ~85 за сутки), поэтому бережём:
# если треки листают, обложку ставим только тому, что играет PHOTO_MIN_LISTEN секунд, и не больше PHOTO_DAILY_LIMIT в сутки
PHOTO_MIN_LISTEN = float(os.getenv("PHOTO_MIN_LISTEN", 30))
PHOTO_DAILY_LIMIT = int(os.getenv("PHOTO_DAILY_LIMIT", 60))
MAX_FLOOD_SLEEP = 30  # дольше этого FloodWait не пережидаем, а отключаем ограниченную часть профиля
EMOJI_STATUS = env_flag("EMOJI_STATUS", True)           # 🎧 в статусе до конца трека (Premium)
EMOJI_STATUS_ID = os.getenv("EMOJI_STATUS_ID")          # id своего кастомного эмодзи, иначе ищем 🎧
AUTO_EMOJI = env_flag("AUTO_EMOJI", True)               # сам подбирать эмодзи: слова в названии → жанр → цвет обложки
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
    artist_ids: tuple[str, ...] = ()

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
        artist_ids=tuple(a["id"] for a in item.get("artists", []) if a.get("id")),  # у локальных файлов id нет
    )


@lru_cache(maxsize=8)  # обложку качаем один раз: и для цвета/эмодзи, и для аватарки
def download(url: str) -> bytes:
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    return response.content


# ---------- Telegram ----------

FEATURES = {"photo": "аватарка", "photo_delete": "удаление аватарки", "bio": "био",
            "status": "эмодзи-статус", "color": "цвет профиля"}


def human_time(seconds: float) -> str:
    hours, minutes = divmod(round(seconds / 60), 60)
    return f"{hours} ч {minutes} мин" if hours else f"{minutes} мин" if minutes else f"{round(seconds)} с"


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
        if "photo_unique_id" in self.state:  # формат до списка photo_ids
            self.state["photo_ids"] = [self.state.pop("photo_unique_id")]
        self._limit_logged = False  # про исчерпанный лимит аватарок пишем в лог один раз
        self.status_enabled = False
        self.emoji_status_id: int | None = None  # 🎧 по умолчанию
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
        """Короткий FloodWait пережидаем; длинный отдаём наверх — спать часами, заморозив весь бот, нельзя."""
        while True:
            try:
                return await func(*args, **kwargs)
            except FloodWait as e:
                if e.value > MAX_FLOOD_SLEEP:
                    raise
                log.warning("Telegram просит подождать %s с", e.value)
                await asyncio.sleep(e.value + 1)

    async def _invoke(self, query):
        return await self._call(self.app.invoke, query)

    # ----- лимиты Telegram: если ограничили одну часть профиля, остальные продолжаем обновлять -----

    def blocked_until(self, feature: str) -> float:
        until = self.state.get("blocked", {}).get(feature, 0)
        return until if until > time.time() else 0

    def _block(self, feature: str, e: FloodWait):
        until = time.time() + e.value + 10
        self.state.setdefault("blocked", {})[feature] = until  # в state — чтобы после перезапуска не долбить заново
        self._save()
        log.warning("Telegram ограничил «%s» на %s, до %s. Остальное продолжаю обновлять. Ответ: %s",
                    FEATURES[feature], human_time(e.value), time.strftime("%d.%m %H:%M", time.localtime(until)), e)

    async def _try(self, feature: str, func, *args, **kwargs) -> bool:
        if self.blocked_until(feature):
            return False
        try:
            await self._call(func, *args, **kwargs)
            return True
        except FloodWait as e:
            self._block(feature, e)
            return False

    def photos_used(self) -> int:
        """Сколько аватарок загрузили за последние сутки."""
        day_ago = time.time() - 86400
        self.state["photo_times"] = [t for t in self.state.get("photo_times", []) if t > day_ago]
        return len(self.state["photo_times"])

    @property
    def photo_allowed(self) -> bool:
        return not self.blocked_until("photo") and self.photos_used() < PHOTO_DAILY_LIMIT

    def photo_pending(self, track: Track) -> bool:
        """Обложки этого трека ещё нет на аватарке, но поставить её можно."""
        return bool(track.cover_url) and track.cover_url != self.state.get("cover_url") and self.photo_allowed

    @property
    def track_uri(self) -> str | None:
        return self.state.get("track_uri")

    @property
    def active(self) -> bool:
        """В профиле сейчас есть что-то наше."""
        return any(self.state.get(k) for k in ("track_uri", "photo_ids", "status_until", "profile_color_id", "bio_dirty"))

    @property
    def status_expiring(self) -> bool:
        # статус живёт с запасом после конца трека; пока музыка играет — продлеваем заранее,
        # иначе в промежутке Telegram покажет пустой статус (звезду Premium)
        until = self.state.get("status_until")
        return self.status_enabled and until is not None and until - time.time() < POLL_INTERVAL * 2 + 10

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
        for feature in FEATURES:
            if self.blocked_until(feature):
                log.warning("«%s» всё ещё ограничено Telegram до %s", FEATURES[feature],
                            time.strftime("%d.%m %H:%M", time.localtime(self.blocked_until(feature))))

        if not me.premium and (EMOJI_STATUS or PROFILE_COLOR):
            log.warning("Нет Telegram Premium — эмодзи-статус и цвет профиля выключены")
            return

        if EMOJI_STATUS:
            self.status_enabled = True
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

    # ----- аватарка -----

    async def _delete_our_photos(self, keep: str | None = None) -> bool:
        """Удаляет загруженные нами обложки (кроме keep). False — Telegram пока не даёт, повторим позже."""
        # храним unique_id, а не file_id: у file_id протухает file_reference после перезапуска
        ours = [u for u in self.state.get("photo_ids", []) if u != keep]
        if ours:
            if self.blocked_until("photo_delete"):
                return False
            try:
                async for p in self.app.get_chat_photos("me", limit=20):
                    if p.big_photo_unique_id in ours:
                        await self._call(self.app.delete_profile_photos, p.big_file_id)
            except FloodWait as e:
                self._block("photo_delete", e)
                return False
            except RPCError as e:
                log.warning("Не удалось удалить старую обложку: %s", e)
        self.state["photo_ids"] = [keep] if keep else []
        if not keep:
            self.state.pop("cover_url", None)
        self._save()
        return True

    async def _set_photo(self, cover_url: str) -> bool:
        cover = await asyncio.to_thread(download, cover_url)

        async def upload():
            photo = io.BytesIO(cover)  # свежий на каждую попытку
            photo.name = "cover.jpg"
            await self.app.set_profile_photo(photo=types.InputChatPhotoStatic(photo))

        if not await self._try("photo", upload):
            return False
        self.state.setdefault("photo_times", []).append(time.time())
        new_id = None
        try:
            async for p in self.app.get_chat_photos("me", limit=1):
                new_id = p.big_photo_unique_id
        except RPCError as e:
            log.warning("Не смог запомнить новую аватарку: %s", e)
        if new_id:
            self.state.setdefault("photo_ids", []).append(new_id)
            self.state["cover_url"] = cover_url
        log.info("Обложка на аватарке (за сутки %s из %s)", self.photos_used(), PHOTO_DAILY_LIMIT)
        self._save()  # сразу, чтобы после падения не потерять, какую аву мы поставили
        # сначала поставили новую, теперь удаляем старую — чтобы не мелькала настоящая ава
        await self._delete_our_photos(keep=new_id)
        return True

    async def sync_photo(self, track: Track, upload: bool):
        """Приводит аватарку к треку. Если обложку трека поставить нельзя (лимит) или рано (upload=False,
        треки листают) — убираем нашу прошлую: лучше настоящая ава, чем обложка от другого трека."""
        if track.cover_url and track.cover_url == self.state.get("cover_url"):
            return
        if track.cover_url and upload and self.photo_allowed and await self._set_photo(track.cover_url):
            self._limit_logged = False
            return
        if track.cover_url and not self.photo_allowed and not self._limit_logged:
            self._limit_logged = True
            until = self.blocked_until("photo")
            log.warning("Аватарку пока не меняю: %s. Текст, эмодзи и цвет обновляются как обычно",
                        f"Telegram ограничил до {time.strftime('%d.%m %H:%M', time.localtime(until))}" if until
                        else f"за сутки уже {self.photos_used()} из {PHOTO_DAILY_LIMIT} (PHOTO_DAILY_LIMIT)")
        await self._delete_our_photos()

    # ----- цвет, статус, био -----

    async def _set_profile_color(self, color_id: int | None) -> bool:
        if color_id == self.state.get("profile_color_id"):
            return True
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
        if not await self._try("color", self.app.invoke, raw.functions.account.UpdateColor(for_profile=True, color=color)):
            return False
        if color_id is None:
            self.state.pop("profile_color_id", None)
        else:
            self.state["profile_color_id"] = color_id
        self._save()
        return True

    async def refresh_status(self, track: Track, emoji_id: int | None = None):
        emoji_id = emoji_id or self.emoji_status_id
        if not self.status_enabled or emoji_id is None:
            return
        # запас после конца трека покрывает переход к следующему; если бот упадёт — статус всё равно сам исчезнет
        until = int(time.time() + track.remaining + IDLE_TIMEOUT + 30)
        if await self._try("status", self.app.invoke, raw.functions.account.UpdateEmojiStatus(
                emoji_status=raw.types.EmojiStatus(document_id=emoji_id, until=until))):
            self.state["status_until"] = until
            self._save()

    async def show(self, track: Track, emoji_id: int | None = None, with_photo: bool = True):
        # сначала быстрое и дешёвое (текст, эмодзи, цвет), аватарка — в конце
        await self._try("bio", self.app.update_profile, bio=track.bio)
        self.state["track_uri"] = track.uri
        self.state["track_title"] = f"{track.artist} — {track.title}"
        self._save()
        await self.refresh_status(track, emoji_id)
        if self.palette and track.cover_url:
            cover = await asyncio.to_thread(download, track.cover_url)
            await self._set_profile_color(nearest_profile_color(cover_color(cover), self.palette))
        await self.sync_photo(track, upload=with_photo)

    async def restore(self) -> bool:
        """Возвращает профиль как был. False — что-то Telegram пока не дал вернуть, повторим позже."""
        if not self.active:
            return True
        done = await self._delete_our_photos()
        if self.state.get("track_uri") or self.state.get("bio_dirty"):
            if await self._try("bio", self.app.update_profile, bio=self.state.get("original_bio", "")):
                self.state.pop("bio_dirty", None)
            else:
                self.state["bio_dirty"] = True
                done = False
        self.state.pop("track_uri", None)
        self.state.pop("track_title", None)
        if "status_until" in self.state:
            if await self._try("status", self.app.invoke, raw.functions.account.UpdateEmojiStatus(
                    emoji_status=load_emoji_status(self.state.get("original_emoji_status")))):
                self.state.pop("status_until")
            else:
                done = False
        if "profile_color_id" in self.state:
            done = await self._set_profile_color(None) and done
        self._save()
        return done


# ---------- файлы правил: чёрный список и эмодзи ----------

class RulesFile:
    """По строке `ключ` или `ключ = значение`, всё после ` #` — комментарий.
    Файл можно править руками — бот перечитывает его на лету. Ключи сравниваются без учёта регистра."""

    def __init__(self, path: Path):
        self.path = path
        self.entries: dict[str, tuple[str, str]] = {}  # ключ -> (значение, комментарий)
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
            body, _, comment = line.partition(" #")
            key, _, value = body.partition(" = ")
            self.entries[key.strip()] = (value.strip(), comment.strip())
        self._mtime = mtime

    def _write(self):
        lines = []
        for key, (value, comment) in self.entries.items():
            line = f"{key} = {value}" if value else key
            lines.append(f"{line}  # {comment}" if comment else line)
        self.path.write_text("\n".join(lines) + "\n" if lines else "", "utf-8")
        self._mtime = self.path.stat().st_mtime

    def _find(self, key: str) -> str | None:
        return next((k for k in self.entries if k.lower() == key.lower()), None)

    def has(self, key: str) -> bool:
        self._reload()
        return self._find(key) is not None

    def set(self, key: str, value: str = "", comment: str = ""):
        self._reload()
        found = self._find(key)
        if found:
            del self.entries[found]
        self.entries[key] = (value, comment)
        self._write()

    def remove(self, key: str) -> bool:
        self._reload()
        found = self._find(key)
        if not found:
            return False
        del self.entries[found]
        self._write()
        return True

    def items(self) -> list[tuple[str, str, str]]:
        self._reload()
        return [(k, v, c) for k, (v, c) in self.entries.items()]


class Blacklist(RulesFile):
    """blacklist.txt: `spotify:track:…` (конкретный трек) или имя артиста."""

    def matches(self, track: Track) -> bool:
        self._reload()
        return self._find(track.uri) is not None or any(self._find(a) for a in track.artists)

    def add(self, key: str, comment: str = "") -> bool:
        if self.has(key):
            return False
        self.set(key, "", comment)
        return True


class EmojiRules(RulesFile):
    """emoji_rules.txt: `ключ = эмодзи`.
    Ключ: `spotify:track:…` | имя артиста | `~слово` (есть в названии трека) | `*` (вместо 🎧 по умолчанию).
    Эмодзи: обычный символ (🔥 — бот сам найдёт премиум-версию) или `id:символ` кастомного эмодзи."""

    def pick(self, track: Track) -> tuple[str, str] | None:
        """Своё правило для трека: сам трек → артист → `~слово`. Возвращает (эмодзи, почему)."""
        self._reload()
        found = self._find(track.uri)
        if found:
            return self.entries[found][0], "твоё правило для трека"
        for artist in track.artists:
            found = self._find(artist)
            if found:
                return self.entries[found][0], f"твоё правило для {artist}"
        title = track.title.lower()
        for key, (value, _) in self.entries.items():
            word = key[1:].strip().lower() if key.startswith("~") else ""
            if word and word in title:
                return value, f"твоё правило ~{word}"
        return None

    def default(self) -> str | None:
        self._reload()
        found = self._find("*")
        return self.entries[found][0] if found else None


# ---------- автоподбор эмодзи ----------

# слова в названии трека → эмодзи; порядок важен — сначала более точные (heartbreak раньше love, rockstar раньше star)
_TITLE_WORDS = [
    (r"heartbreak\w*|broken heart|разбит\w* сердц\w*", "💔"),
    (r"rock ?star\w*|рок ?стар\w*", "🎸"),
    (r"love\w*|lover\w*|любов\w*|любви|любл\w*|любим\w*", "❤️"),
    (r"kiss\w*|lips|поцелу\w*|губ[ыа]?", "💋"),
    (r"rain\w*|storm\w*|дожд\w*|ливен\w*|ливн\w*|гроз\w*", "🌧"),
    (r"night\w*|midnight|moon\w*|ноч\w*|полноч\w*|лун[аыеу]\w*", "🌙"),
    (r"money|cash|dollar\w*|rich|деньг\w*|бабк\w*|кэш\w*|бабл\w*", "💸"),
    (r"fire|flame\w*|burn\w*|огон\w*|огн\w*|пожар\w*|горит|горю|гори|сгора\w*", "🔥"),
    (r"dead|death|die|dying|kill\w*|смерт\w*|мертв\w*|мёртв\w*|умер\w*|умир\w*", "💀"),
    (r"ghost\w*|призрак\w*|привидени\w*", "👻"),
    (r"angel\w*|heaven\w*|ангел\w*|рай|небес\w*", "😇"),
    (r"devil\w*|demon\w*|hell|дьявол\w*|демон\w*|ад", "😈"),
    (r"king\w*|queen\w*|crown|корол\w*|корон\w*|царь|цар[ияю]\w*", "👑"),
    (r"snow\w*|winter|ice|icy|frozen|cold|снег\w*|снеж\w*|зим\w*|лёд|лед|холод\w*", "❄️"),
    (r"sun\w*|summer|солнц\w*|солнеч\w*|лет[оа]", "☀️"),
    (r"stars?|starlight|starboy|звезд\w*|звёзд\w*", "⭐"),
    (r"rocket\w*|space|galaxy|ракет\w*|космос\w*|космич\w*", "🚀"),
    (r"cry\w*|tears?|sad\w*|lonely|alone|слез\w*|слёз\w*|груст\w*|плач\w*|плак\w*|одинок\w*", "😢"),
    (r"party|club|dance\w*|туса\w*|тус[ао]вк\w*|клуб\w*|танц\w*", "🪩"),
    (r"dream\w*|sleep\w*|сон|сны|снов\w*|мечт\w*", "💭"),
    (r"sea|ocean\w*|wave\w*|beach|мор[еяю]|океан\w*|волн\w*|пляж\w*", "🌊"),
    (r"flower\w*|rose\w*|цветы|цвет[оа]к\w*|роз[аыу]", "🌹"),
    (r"car|cars|drive|driving|speed|тачк\w*|машин\w*|скорост\w*", "🏎"),
    (r"smoke\w*|high|дым\w*|кур[юи]\w*", "💨"),
    (r"wine|drunk|вино|пьян\w*|бокал\w*", "🍷"),
    (r"god|pray\w*|бог\w*|молит\w*|молю", "🙏"),
    (r"crazy|psycho|insane|безум\w*|псих\w*|бешен\w*", "🤪"),
    (r"gold\w*|diamond\w*|золот\w*|бриллиант\w*|алмаз\w*", "💎"),
    (r"phone|call\w*|телефон\w*|звон\w*", "📱"),
]
TITLE_EMOJI = [(re.compile(rf"(?<!\w)(?:{pattern})(?!\w)", re.IGNORECASE), emoji) for pattern, emoji in _TITLE_WORDS]

# жанр артиста из Spotify → эмодзи; тоже по порядку (trap раньше rap, k-pop раньше pop)
GENRE_EMOJI = [
    ("phonk", "🏎"), ("drill", "🥶"), ("emo", "🖤"), ("trap", "🔥"), ("rap", "🎤"), ("hip hop", "🎤"),
    ("metal", "🤘"), ("punk", "⚡"), ("rock", "🎸"), ("grunge", "🎸"),
    ("r&b", "💜"), ("soul", "💜"), ("funk", "🕺"), ("disco", "🪩"), ("jazz", "🎷"), ("blues", "🎷"),
    ("classical", "🎻"), ("orchestra", "🎻"), ("soundtrack", "🎬"), ("anime", "🌸"),
    ("house", "🪩"), ("techno", "🪩"), ("edm", "⚡"), ("dubstep", "⚡"), ("electro", "⚡"), ("dance", "🪩"),
    ("lo-fi", "☕"), ("lofi", "☕"), ("chill", "☕"), ("ambient", "🌌"),
    ("country", "🤠"), ("reggae", "🌴"), ("reggaeton", "💃"), ("latin", "💃"), ("k-pop", "💜"),
    ("indie", "🌿"), ("folk", "🌿"), ("pop", "💖"),
]


def title_emoji(title: str) -> tuple[str, str] | None:
    # «(feat. …)», «[prod. …]» — не часть названия
    clean = re.sub(r"[(\[][^)\]]*\b(?:feat|ft|prod|with|remix)\b[^)\]]*[)\]]", "", title, flags=re.IGNORECASE)
    for regex, emoji in TITLE_EMOJI:
        match = regex.search(clean)
        if match:
            return emoji, f"слово «{match.group(0)}» в названии"
    return None


def genre_emoji(genres: list[str]) -> tuple[str, str] | None:
    for genre in genres:
        for key, emoji in GENRE_EMOJI:
            if key in genre.lower():
                return emoji, f"жанр {genre}"
    return None


def color_heart(rgb: tuple[int, int, int]) -> str:
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
    if s < 0.2:
        return "🤍" if v > 0.75 else "🖤" if v < 0.3 else "🩶"
    deg = h * 360
    if 15 <= deg < 45 and v < 0.55:
        return "🤎"
    for limit, heart in ((15, "❤️"), (45, "🧡"), (70, "💛"), (160, "💚"), (200, "🩵"), (250, "💙"), (290, "💜"), (345, "🩷")):
        if deg < limit:
            return heart
    return "❤️"


def parse_emoji(value: str) -> tuple[int | None, str]:
    """`5206…:🔥` / `5206…` / `🔥` → (id кастомного эмодзи или None, символ для показа)."""
    head, _, tail = value.partition(":")
    if head.isdigit():
        return int(head), tail or "⭐"
    return None, value


def emoji_html(value: str) -> str:
    doc_id, char = parse_emoji(value)
    return f'<emoji id="{doc_id}">{html.escape(char)}</emoji>' if doc_id else html.escape(char)


def describe_rule(key: str, comment: str) -> str:
    if key.startswith("spotify:track:"):
        return f"трек «{comment or key}»"
    if key == "*":
        return "по умолчанию (вместо 🎧)"
    if key.startswith("~"):
        return f"треки со словом «{key[1:].strip()}» в названии"
    return f"артист {key}"


# ---------- бот: цикл + команды в «Избранном» ----------

HELP = """🎧 Команды (пиши в «Избранное»):
.status — что сейчас в профиле
.off — пауза: вернуть профиль и не трогать, пока не .on
.on — снова показывать музыку
.ban — скрыть текущий трек
.ban Имя артиста — скрыть артиста
.unban [Имя артиста] — убрать из чёрного списка (без имени — текущий трек)
.bans — чёрный список

Эмодзи-статус подбирается сам: слова в названии → жанр → цвет обложки.
Свои правила важнее (можно любые премиум-эмодзи из наборов):
.emoji 🔥 — для текущего трека
.emoji Имя артиста 🔥 — для артиста
.emoji ~слово 🌧 — если слово есть в названии трека
.emoji * 🎶 — когда слов и жанра нет (вместо сердечка по цвету обложки)
.unemoji [ключ] — убрать правило (без ключа — текущий трек)
.emojis — все правила

.stop — вернуть профиль и выключить бота"""


class Bot:
    def __init__(self, app: Client, sp: spotipy.Spotify):
        self.app = app
        self.sp = sp
        self.profile = Profile(app)
        self.blacklist = Blacklist(BLACKLIST_FILE)
        self.emoji_rules = EmojiRules(EMOJI_RULES_FILE)
        self._emoji_cache: dict[str, int | None] = {}  # символ -> id премиум-версии
        self._genres: dict[str, list[str]] = {}  # id артиста -> жанры из Spotify
        self.emoji_reason = ""  # почему в статусе такой эмодзи (для лога и .status)
        self.lock = asyncio.Lock()  # цикл и команды не должны менять профиль одновременно
        self.stopped = asyncio.Event()
        self.current: Track | None = None  # что сейчас играет в Spotify
        self.idle_since: float | None = None
        self.throttled_uri: str | None = None  # чтобы писать в лог про лимит один раз на трек
        self.restore_logged = False
        self.tokens = float(MAX_UPDATES)  # сколько смен профиля можно сделать прямо сейчас
        self.tokens_at: float | None = None
        self.last_show = float("-inf")
        self.photo_tried_uri: str | None = None  # отложенную аватарку пробуем один раз на трек

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
                    if not self.restore_logged:
                        self.restore_logged = True
                        log.info("Музыка не играет — возвращаю профиль")
                    await self.profile.restore()
                return POLL_INTERVAL

            self.idle_since = None
            self.restore_logged = False
            now = loop.time()
            if self.tokens_at is not None:
                self.tokens = min(MAX_UPDATES, self.tokens + (now - self.tokens_at) / UPDATE_REFILL)
            self.tokens_at = now
            rate_wait = 0 if self.tokens >= 1 else (1 - self.tokens) * UPDATE_REFILL
            # быстро листаешь треки — профиль не дёргается: трек должен играть MIN_LISTEN секунд
            if track.uri != self.profile.track_uri and track.progress >= MIN_LISTEN and rate_wait <= 0:
                # прошлый трек слушали, а не пролистали — обложку ставим сразу; иначе ждём PHOTO_MIN_LISTEN
                calm = now - self.last_show >= PHOTO_MIN_LISTEN + 15
                emoji_id, self.emoji_reason = await self.status_emoji(track)
                self.photo_tried_uri = None
                await self.profile.show(track, emoji_id, with_photo=calm or track.progress >= PHOTO_MIN_LISTEN)
                self.tokens -= 1
                self.last_show = now
                log.info("Сейчас играет: %s — %s (статус: %s)", track.artist, track.title, self.emoji_reason)
                return self._photo_delay(track)

            if self.profile.active and self.profile.status_expiring:
                emoji_id, self.emoji_reason = await self.status_emoji(track)
                await self.profile.refresh_status(track, emoji_id)  # долгая пауза, повтор трека
            if track.uri == self.profile.track_uri:
                if (self.profile.photo_pending(track) and track.progress >= PHOTO_MIN_LISTEN
                        and self.photo_tried_uri != track.uri):
                    self.photo_tried_uri = track.uri
                    await self.profile.sync_photo(track, upload=True)  # трек прижился — теперь и обложку
                return self._photo_delay(track)
            if rate_wait > 1 and self.throttled_uri != track.uri:
                self.throttled_uri = track.uri
                log.info("Слишком часто листаешь — обновлю профиль через %.0f с", rate_wait)
            # проснёмся ровно когда новый трек можно ставить
            wait = max(MIN_LISTEN - track.progress, rate_wait)
            return min(POLL_INTERVAL, max(wait + 0.2, 0.5))

    def _photo_delay(self, track: Track) -> float:
        """Через сколько проснуться, чтобы вовремя поставить отложенную обложку."""
        if self.profile.photo_pending(track) and track.progress < PHOTO_MIN_LISTEN and self.photo_tried_uri != track.uri:
            return min(POLL_INTERVAL, max(PHOTO_MIN_LISTEN - track.progress + 0.2, 0.5))
        return POLL_INTERVAL

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
                except FloodWait as e:  # лимит на чём-то служебном — просто ждём, не долбим
                    delay = min(e.value, 600)
                    log.warning("Telegram просит подождать %s — следующая попытка через %s. Ответ: %s",
                                human_time(e.value), human_time(delay), e)
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
            ".emoji": self.cmd_emoji, ".unemoji": self.cmd_unemoji, ".emojis": self.cmd_emojis,
        }.get(cmd.lower())
        if handler is None:  # обычная заметка с точкой — не наше дело
            return
        log.info("Команда: %s", message.text.strip())
        try:
            reply = await handler(arg.strip(), message)
        except Exception as e:
            log.exception("Команда %s упала", cmd)
            reply = f"⚠️ Ошибка: {e}"
        text, mode = reply if isinstance(reply, tuple) else (reply, enums.ParseMode.DISABLED)
        try:
            await message.edit_text(text, parse_mode=mode)
        except RPCError as e:
            log.warning("Не удалось ответить на команду: %s", e)
        if cmd.lower() == ".stop":
            self.stopped.set()

    async def cmd_help(self, arg, message):
        return HELP

    async def cmd_status(self, arg, message):
        now = f"{self.current.artist} — {self.current.title}" if self.current else "ничего"
        if self.current and self.blacklist.matches(self.current):
            now += " (в чёрном списке)"
        shown = self.profile.state.get("track_title")
        status = f"\nЭмодзи-статус: {self.emoji_reason}" if shown and self.profile.status_enabled else ""
        limits = "".join(
            f"\n⛔ {FEATURES[f]}: Telegram ограничил до {time.strftime('%d.%m %H:%M', time.localtime(self.profile.blocked_until(f)))}"
            for f in FEATURES if self.profile.blocked_until(f))
        return (f"{'⏸ На паузе' if self.paused else '▶️ Работает'}\n"
                f"В Spotify: {now}\n"
                f"В профиле: {shown or 'ничего (профиль как обычно)'}{status}\n"
                f"Аватарок за сутки: {self.profile.photos_used()} из {PHOTO_DAILY_LIMIT}{limits}\n"
                f"В чёрном списке: {len(self.blacklist.items())}\n\n.help — команды")

    async def cmd_on(self, arg, message):
        if not self.paused:
            return "▶️ Бот и так работает"
        self.profile.state.pop("paused", None)
        self.profile._save()
        self.idle_since = None
        return "▶️ Включил — музыка снова будет в профиле"

    async def cmd_off(self, arg, message):
        async with self.lock:
            self.profile.state["paused"] = True
            await self.profile.restore()
            self.profile._save()
        return "⏸ На паузе, профиль вернул как было. .on — включить"

    async def cmd_ban(self, arg, message):
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

    async def cmd_unban(self, arg, message):
        if arg:
            return f"✅ «{arg}» убран из чёрного списка" if self.blacklist.remove(arg) else f"«{arg}» нет в чёрном списке"
        if not self.current:
            return "Сейчас ничего не играет. Чтобы вернуть артиста: .unban Имя артиста"
        if self.blacklist.remove(self.current.uri):
            return f"✅ Трек «{self.current.artist} — {self.current.title}» убран из чёрного списка"
        return "Этого трека нет в чёрном списке (если скрыт артист — .unban Имя артиста)"

    async def cmd_bans(self, arg, message):
        items = self.blacklist.items()
        if not items:
            return "Чёрный список пуст. .ban — скрыть текущий трек, .ban Имя — артиста"
        lines = [f"• {c or k}" + (" (трек)" if k.startswith("spotify:track:") else "") for k, _, c in items]
        return "🚫 Чёрный список:\n" + "\n".join(lines)

    async def cmd_stop(self, arg, message):
        return "⏹ Выключаюсь, профиль верну как было. Запустить снова — перезайти в Windows или запустить main.py"

    # ----- эмодзи-статус по правилам -----

    async def resolve_emoji(self, value: str) -> int | None:
        doc_id, char = parse_emoji(value)
        if doc_id:
            return doc_id
        if char not in self._emoji_cache:
            found = await self.profile._invoke(raw.functions.messages.SearchCustomEmoji(emoticon=char, hash=0))
            ids = getattr(found, "document_id", None)
            self._emoji_cache[char] = ids[0] if ids else None
        return self._emoji_cache[char]

    async def artist_genres(self, track: Track) -> list[str]:
        genres = []
        for artist_id in track.artist_ids:
            if artist_id not in self._genres:
                try:
                    artist = await asyncio.to_thread(self.sp.artist, artist_id)
                except (spotipy.SpotifyException, requests.RequestException) as e:
                    log.warning("Не получил жанры артиста: %s", e)
                    continue  # не кэшируем — попробуем в следующий раз
                self._genres[artist_id] = artist.get("genres") or []
            genres += self._genres[artist_id]
        return genres

    async def _emoji_candidates(self, track: Track):
        """Варианты эмодзи по порядку: свои правила → слова в названии → жанр → `*` → цвет обложки."""
        rule = self.emoji_rules.pick(track)
        if rule:
            yield rule
        if AUTO_EMOJI:
            hit = title_emoji(track.title)
            if hit:
                yield hit
            hit = genre_emoji(await self.artist_genres(track))
            if hit:
                yield hit
        default = self.emoji_rules.default()
        if default:
            yield default, "твоё правило *"
        if AUTO_EMOJI and track.cover_url:
            cover = await asyncio.to_thread(download, track.cover_url)
            yield color_heart(cover_color(cover)), "цвет обложки"

    async def status_emoji(self, track: Track) -> tuple[int | None, str]:
        """Какой эмодзи поставить в статус и почему. (None, …) — стандартный 🎧."""
        async for value, reason in self._emoji_candidates(track):
            emoji_id = await self.resolve_emoji(value)
            if emoji_id:
                return emoji_id, f"{parse_emoji(value)[1]} — {reason}"
            log.warning("Не нашёл премиум-эмодзи «%s» (%s) — пробую следующий вариант", value, reason)
        return None, "🎧 — по умолчанию"

    async def _apply_status_now(self):
        # правило поменяли для того, что сейчас в профиле, — не ждём следующего трека
        async with self.lock:
            track = self.current
            if not self.paused and track and track.uri == self.profile.track_uri:
                emoji_id, self.emoji_reason = await self.status_emoji(track)
                await self.profile.refresh_status(track, emoji_id)

    async def cmd_emoji(self, arg, message):
        tokens = arg.split()
        if not tokens:
            return "Как: .emoji 🔥 (текущий трек), .emoji Имя артиста 🔥, .emoji ~слово 🌧, .emoji * 🎶. Все правила — .emojis"
        emoji_token, key = tokens[-1], " ".join(tokens[:-1])
        custom = [e for e in (message.entities or []) if e.type == enums.MessageEntityType.CUSTOM_EMOJI]
        if custom:  # премиум-эмодзи из набора — берём ровно его
            value = f"{custom[-1].custom_emoji_id}:{emoji_token}"
        elif await self.resolve_emoji(emoji_token) is not None:
            value = emoji_token
        else:
            return (f"Не нашёл премиум-эмодзи для «{emoji_token}». Эмодзи должен быть последним — "
                    f"например .emoji {key or 'Имя артиста'} 🔥, или выбери эмодзи из набора в панели")
        comment = ""
        if not key:
            if not self.current:
                return "Сейчас ничего не играет. Для артиста: .emoji Имя артиста 🔥"
            key, comment = self.current.uri, f"{self.current.artist} — {self.current.title}"
        self.emoji_rules.set(key, value, comment)
        await self._apply_status_now()
        return f"{emoji_html(value)} теперь для: {html.escape(describe_rule(key, comment))}", enums.ParseMode.HTML

    async def cmd_unemoji(self, arg, message):
        key = arg
        if not key:
            if not self.current:
                return "Сейчас ничего не играет. Убрать правило: .unemoji Имя артиста (или ~слово, или *)"
            key = self.current.uri
        if not self.emoji_rules.remove(key):
            return "Такого правила нет. Все правила — .emojis"
        await self._apply_status_now()
        return "✅ Правило убрано"

    async def cmd_emojis(self, arg, message):
        items = self.emoji_rules.items()
        if not items:
            return "Правил нет — везде 🎧. Добавить: .emoji Имя артиста 🔥"
        lines = [f"{emoji_html(v)} — {html.escape(describe_rule(k, c))}" for k, v, c in items]
        return "Эмодзи-статус:\n" + "\n".join(lines), enums.ParseMode.HTML

    async def _hide_if_banned(self):
        # если в профиле прямо сейчас то, что забанили, — убираем сразу
        async with self.lock:
            shown = self.profile.track_uri
            if shown and (self.blacklist.has(shown)
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
