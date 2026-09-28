"""Ставит обложку текущего трека из Spotify на аватарку Telegram, название — в био,
🎧 в эмодзи-статус и красит профиль в цвет обложки."""
import asyncio
from collections import deque
import colorsys
import io
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests
import spotipy
from dotenv import load_dotenv
from PIL import Image
from pyrogram import Client, raw, types
from pyrogram.errors import AuthKeyUnregistered, FloodWait, RPCError
from spotipy.oauth2 import SpotifyOAuth, SpotifyOauthError

BASE_DIR = Path(__file__).resolve().parent  # всё (сессия, токены, state) лежит рядом со скриптом
load_dotenv(BASE_DIR / ".env")

log = logging.getLogger("spotify_tg")


def env_flag(name: str, default: bool) -> bool:
    return os.getenv(name, str(int(default))).strip().lower() in ("1", "true", "yes", "on")


SPOTIFY_CACHE = BASE_DIR / ".cache"
STATE_FILE = BASE_DIR / "state.json"  # помним, что поменяли, и исходный профиль между перезапусками
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
        self._save()


# ---------- main loop ----------

async def run():
    sp = make_spotify()
    app = Client(
        os.getenv("TG_SESSION", "my_account"),
        api_id=int(os.environ["TG_API_ID"]),
        api_hash=os.environ["TG_API_HASH"],
        workdir=str(BASE_DIR),
    )

    async with app:
        profile = Profile(app)
        await profile.init()
        loop = asyncio.get_running_loop()
        idle_since: float | None = None
        throttled_uri: str | None = None  # чтобы писать в лог про лимит один раз на трек
        updates: deque[float] = deque()  # когда меняли профиль (для лимита MAX_UPDATES / UPDATE_WINDOW)
        log.info("Запущен, слежу за Spotify каждые %s с", POLL_INTERVAL)

        try:
            while True:
                delay = POLL_INTERVAL
                try:
                    track = await asyncio.to_thread(fetch_current_track, sp)
                    if track is None:
                        idle_since = idle_since or loop.time()
                        if profile.active and loop.time() - idle_since >= IDLE_TIMEOUT:
                            log.info("Музыка не играет — возвращаю профиль")
                            await profile.restore()
                    else:
                        idle_since = None
                        while updates and loop.time() - updates[0] >= UPDATE_WINDOW:
                            updates.popleft()
                        rate_wait = UPDATE_WINDOW - (loop.time() - updates[0]) if len(updates) >= MAX_UPDATES else 0
                        # быстро листаешь треки — профиль не дёргается: трек должен играть MIN_LISTEN секунд
                        if track.uri != profile.track_uri and track.progress >= MIN_LISTEN and rate_wait <= 0:
                            await profile.show(track)
                            updates.append(loop.time())
                            log.info("Сейчас играет: %s — %s", track.artist, track.title)
                        else:
                            if profile.active and profile.status_expiring:
                                await profile.refresh_status(track)  # длинная пауза между сменами, повтор трека
                            if track.uri != profile.track_uri:
                                if rate_wait > 0 and throttled_uri != track.uri:
                                    throttled_uri = track.uri
                                    log.info("Слишком часто листаешь — обновлю профиль через %.0f с", rate_wait)
                                # проснёмся ровно когда новый трек можно ставить
                                wait = max(MIN_LISTEN - track.progress, rate_wait)
                                delay = min(POLL_INTERVAL, max(wait + 0.2, 0.5))
                except (spotipy.SpotifyException, SpotifyOauthError, requests.RequestException, RPCError) as e:
                    log.warning("Ошибка, попробую ещё раз: %s", e)
                except Exception:
                    log.exception("Неожиданная ошибка, продолжаю работать")
                await asyncio.sleep(delay)
        finally:
            log.info("Выключаюсь — возвращаю профиль")
            try:
                await profile.restore()
            except Exception as e:
                log.error("Не удалось вернуть профиль (%s) — вернётся при следующем запуске", e)


if __name__ == "__main__":
    sys.stderr.reconfigure(errors="replace")  # иероглифы в названиях не должны ронять логи
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except AuthKeyUnregistered:
        log.error("Сессия Telegram больше не действует. Удали %s.session и запусти снова — "
                  "попросит номер телефона и код.", os.getenv("TG_SESSION", "my_account"))
