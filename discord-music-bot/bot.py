import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import aiohttp
import discord
import yt_dlp
from discord.ext import commands

# ── Папка для временных mp3 ───────────────────────────────
TEMP_FOLDER = Path("./temp_music")
TEMP_FOLDER.mkdir(exist_ok=True)

# ── Настройки ─────────────────────────────────────────────
# ВСТАВЬ НОВЫЙ ТОКЕН ПОСЛЕ СБРОСА: Developer Portal -> Bot -> Reset Token -> Copy
BOT_TOKEN = "ВСТАВЬ НОВЫЙ ТОКЕН ПОСЛЕ СБРОСА"
PREFIX = "!"
DEFAULT_VOLUME = 0.5

# ── Настройки yt_dlp ──────────────────────────────────────
YDL_OPTIONS = {
    "format": "bestaudio/best",
    "default_search": "ytsearch",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
}

# ── Логирование ───────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

# ── Интенты и бот ─────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

# ── Хранилища по гильдиям ─────────────────────────────────
queues: dict[int, list] = {}
now_playing: dict[int, dict] = {}
volume_levels: dict[int, float] = {}
bass_boost_on: dict[int, bool] = {}
restart_flags: dict[int, bool] = {}  # True = перезапуск по !volume / !bassboost
skip_flags: dict[int, bool] = {}  # True = трек пропущен командой !skip

# ── ДОБАВЛЕНО: Поддержка повтора ──────────────────────────
repeat_current: dict[int, bool] = {}  # True = повтор текущего трека
repeat_queue: dict[int, bool] = {}  # True = повтор всей очереди по кругу


# ── Вспомогательные функции ───────────────────────────────

def get_ffmpeg_options(guild_id: int, is_local: bool = False) -> dict:
    """Опции ffmpeg с учётом громкости/басбуста."""
    vol = volume_levels.get(guild_id, DEFAULT_VOLUME)

    if bass_boost_on.get(guild_id, False):
        af_filter = (
            f"volume={vol},"
            "equalizer=f=50:t=q:w=1:g=15,"
            "equalizer=f=100:t=q:w=1:g=10,"
            "equalizer=f=200:t=q:w=1:g=5"
        )
    else:
        af_filter = f"volume={vol}"

    if is_local:
        return {"before_options": "", "options": f"-vn -af {af_filter}"}

    return {
        "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
        "options": f"-vn -af {af_filter}",
    }


def validate_audio(path: str) -> bool:
    """Быстрая проверка: сможет ли ffmpeg открыть файл."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-v", "error", "-i", str(path), "-f", "null", "-"],
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0:
            logging.error(f"ffmpeg не смог открыть {path}: {result.stderr.decode(errors='ignore')[:300]}")
            return False
        return True
    except FileNotFoundError:
        logging.error("ffmpeg не найден в PATH!")
        return False
    except Exception as e:
        logging.error(f"Ошибка проверки файла {path}: {e}")
        return False


async def search_song(query: str) -> dict | None:
    """Ищет трек через yt_dlp, возвращает словарь с данными (или None)."""
    try:
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            info = ydl.extract_info(query, download=False)

            if "entries" in info:  # ytsearch возвращает "плейлист"
                entries = [e for e in info["entries"] if e]
                if not entries:
                    return None
                info = entries[0]

            url = info.get("url") or info.get("webpage_url")
            if not url:
                return None

            return {
                "title": info.get("title", "Unknown"),
                "url": url,
                "duration": info.get("duration", 0) or 0,
                "webpage_url": info.get("webpage_url", ""),
            }
    except Exception as e:
        logging.error(f"Ошибка поиска: {e}")
        return None


async def play_next(ctx, announce: bool = True):
    """Запускает следующий трек из очереди."""
    guild_id = ctx.guild.id

    if guild_id not in queues or not queues[guild_id]:
        now_playing.pop(guild_id, None)
        return

    vc = ctx.voice_client
    if not vc or not vc.is_connected():
        return

    song = queues[guild_id].pop(0)
    song["started_at"] = time.monotonic()  # чтобы отличать «доиграл» от «крэш»
    now_playing[guild_id] = song

    try:
        is_local = bool(song.get("is_local"))
        opts = get_ffmpeg_options(guild_id, is_local)

        if is_local:
            source = discord.FFmpegPCMAudio(song["url"], **opts)
        else:
            source = discord.FFmpegOpusAudio(song["url"], **opts)

        vc.play(
            source,
            after=lambda e: asyncio.run_coroutine_threadsafe(
                _track_finished(guild_id, ctx, e), bot.loop
            ),
        )
        if announce:
            await ctx.send(f"▶️ Теперь играет: **{song['title']}**")
    except Exception as e:
        logging.error(f"Ошибка запуска трека «{song.get('title')}»: {e}")
        now_playing.pop(guild_id, None)
        try:
            await ctx.send(f"❌ Не удалось воспроизвести **{song.get('title', '?')}**: {e}")
        except Exception:
            pass
        await play_next(ctx)


async def _track_finished(guild_id: int, ctx, error: Exception | None = None):
    """Вызывается после остановки трека: конец / skip / stop / крэш ffmpeg."""

    # Перезапуск по !volume / !bassboost — ничего не трогаем
    if restart_flags.pop(guild_id, False):
        return

    was_skipped = skip_flags.pop(guild_id, False)
    current = now_playing.get(guild_id)

    # ffmpeg сам сообщил об ошибке — трек точно не играл
    if error is not None:
        name = current["title"] if current else "?"
        logging.error(f"Ошибка воспроизведения «{name}»: {error}")
        try:
            await ctx.send(f"⚠️ Не удалось воспроизвести **{name}**.\nПричина: `{error}`")
        except Exception:
            pass
        now_playing.pop(guild_id, None)
        await play_next(ctx)
        return

    # Локальный файл — решаем, реально ли он проигрался
    if current and current.get("is_local"):
        path = current.get("url")
        elapsed = time.monotonic() - current.get("started_at", time.monotonic())

        # Трек «закончился» подозрительно быстро и это НЕ скип -> крэш ffmpeg.
        if not was_skipped and elapsed < 0.75:
            logging.error(
                f"Трек «{current.get('title')}» завершился за {elapsed:.2f}s — "
                f"ffmpeg, похоже, не смог его проиграть. Файл НЕ удалён: {path}"
            )
            try:
                await ctx.send(
                    f"⚠️ **{current.get('title')}** не воспроизвёлся "
                    f"(ffmpeg упал за {elapsed:.2f}s). Файл сохранён в папке `temp_music`.\n"
                    f"Проверь сообщение ffmpeg в консоли."
                )
            except Exception:
                pass
            now_playing.pop(guild_id, None)
            await play_next(ctx)
            return

        # Нормальное завершение или !skip — файл можно удалить
        if path and os.path.exists(path):
            try:
                os.remove(path)
                logging.info(f"Проигранный файл удалён: {path}")
            except PermissionError:
                logging.warning(f"Файл занят ffmpeg, повторю удаление: {path}")
                try:
                    await asyncio.sleep(1.0)
                    os.remove(path)
                except OSError:
                    logging.error(f"Не удалось удалить {path}, файл останется.")
            except OSError as e:
                logging.error(f"Не удалось удалить {path}: {e}")

    # ── ДОБАВЛЕНО: Логика повтора ─────────────────────────
    if current and not was_skipped:
        queues.setdefault(guild_id, [])
        if repeat_current.get(guild_id, False):
            queues[guild_id].insert(0, current)  # Возвращаем в начало
        elif repeat_queue.get(guild_id, False):
            queues[guild_id].append(current)  # Отправляем в конец очереди

    # Играем следующий трек
    await play_next(ctx)


async def _restart_current(ctx):
    """Перезапускает текущий трек с новыми настройками (!volume / !bassboost)."""
    guild_id = ctx.guild.id
    vc = ctx.voice_client
    song = now_playing.get(guild_id)
    if not vc or not song:
        return

    restart_flags[guild_id] = True  # запрещаем авто-переключение/удаление
    queues.setdefault(guild_id, [])
    queues[guild_id].insert(0, song)  # возвращаем трек в начало очереди
    vc.stop()
    await asyncio.sleep(0.3)
    await play_next(ctx, announce=False)


# ── События ───────────────────────────────────────────────

@bot.event
async def on_ready():
    logging.info(f"Бот {bot.user} запущен и готов к работе!")


# ── Команды ───────────────────────────────────────────────

@bot.command(name="play", aliases=["p"])
async def play(ctx: commands.Context, *, query: str = None):
    """Воспроизвести трек: по названию/URL или прикреплённому MP3."""
    user_vc = ctx.author.voice
    if not user_vc or not user_vc.channel:
        await ctx.send("🔊 Сначала зайдите в голосовой канал!")
        return

    vc = ctx.voice_client
    if vc is None:
        vc = await user_vc.channel.connect()
    elif vc.channel != user_vc.channel:
        await vc.move_to(user_vc.channel)

    guild_id = ctx.guild.id
    volume_levels.setdefault(guild_id, DEFAULT_VOLUME)
    bass_boost_on.setdefault(guild_id, False)
    queues.setdefault(guild_id, [])

    # ── MP3 вложение ──
    if ctx.message.attachments:
        attachment = ctx.message.attachments[0]

        if Path(attachment.filename).suffix.lower() != ".mp3":
            await ctx.send("🚫 Прикрепите именно файл **.mp3**.")
            return

        clean_name = re.sub(r"[^\w\-. ]+", "_", Path(attachment.filename).name)
        file_path = TEMP_FOLDER / f"{uuid.uuid4().hex[:8]}_{clean_name}"

        logging.info(f"Скачиваю: {attachment.filename} -> {file_path}")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(attachment.url) as resp:
                    if resp.status != 200:
                        await ctx.send(f"❌ Не удалось скачать файл (HTTP {resp.status}).")
                        return
                    with open(file_path, "wb") as f:
                        while True:
                            chunk = await resp.content.read(1024 * 1024)
                            if not chunk:
                                break
                            f.write(chunk)
        except PermissionError:
            await ctx.send("❌ Нет прав на запись в папку `temp_music`. Проверьте права на папку.")
            file_path.unlink(missing_ok=True)
            return
        except Exception as e:
            await ctx.send(f"❌ Ошибка скачивания: {e}")
            file_path.unlink(missing_ok=True)
            return

        if not file_path.exists() or file_path.stat().st_size == 0:
            await ctx.send("❌ Файл пустой или не сохранился.")
            return

        if not validate_audio(str(file_path)):
            await ctx.send(
                f"❌ ffmpeg не может открыть **{Path(attachment.filename).name}**.\n"
                f"Файл повреждён или имеет нестандартный кодек. Проверь его в плеере."
            )
            file_path.unlink(missing_ok=True)
            return

        queues[guild_id].append({
            "title": Path(attachment.filename).name,
            "url": str(file_path),
            "is_local": True,
            "duration": 0,
        })

        if not vc.is_playing() and not vc.is_paused():
            await play_next(ctx)
        else:
            await ctx.send(f"📥 Добавлено в очередь: **{Path(attachment.filename).name}**")
        return

    # ── Поиск по названию / ссылке ──
    if not query:
        await ctx.send("Используйте: `!play <название трека>` или прикрепите .mp3 файл.")
        return

    await ctx.send("🔍 Ищу трек...")
    song = await search_song(query)
    if not song:
        await ctx.send("❌ Не удалось найти трек. Проверьте название.")
        return

    queues[guild_id].append({
        "title": song["title"],
        "url": song["url"],
        "is_local": False,
        "duration": song["duration"],
    })

    if not vc.is_playing() and not vc.is_paused():
        await play_next(ctx)
    else:
        await ctx.send(f"📥 Добавлено в очередь: **{song['title']}**")


@bot.command(name="skip", aliases=["s", "next"])
async def skip(ctx: commands.Context):
    """Пропустить текущий трек."""
    guild_id = ctx.guild.id
    if ctx.voice_client and ctx.voice_client.is_playing():
        skip_flags[guild_id] = True
        ctx.voice_client.stop()
        await ctx.send("⏭️ Трек пропущен.")
    else:
        await ctx.send("❌ Сейчас ничего не играет.")


@bot.command(name="pause")
async def pause(ctx: commands.Context):
    """Пауза."""
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("⏸️ Пауза.")
    else:
        await ctx.send("❌ Сейчас ничего не играет.")


@bot.command(name="resume", aliases=["r"])
async def resume(ctx: commands.Context):
    """Продолжить."""
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("▶️ Продолжаю.")
    else:
        await ctx.send("❌ Пауза не активна.")


@bot.command(name="stop")
async def stop(ctx: commands.Context):
    """Остановить, очистить очередь и удалить временные mp3."""
    guild_id = ctx.guild.id
    vc = ctx.voice_client

    to_clean = list(queues.get(guild_id, []))
    current = now_playing.get(guild_id)
    if current:
        to_clean.append(current)

    for song in to_clean:
        if song.get("is_local") and song.get("url") and os.path.exists(song["url"]):
            try:
                os.remove(song["url"])
                logging.info(f"Удалён временный файл: {song['url']}")
            except OSError as e:
                logging.warning(f"Не удалён {song['url']}: {e}")

    queues.pop(guild_id, None)
    now_playing.pop(guild_id, None)
    restart_flags.pop(guild_id, None)
    skip_flags.pop(guild_id, None)

    # ── ДОБАВЛЕНО: Очистка флагов повтора при остановке ──
    repeat_current.pop(guild_id, None)
    repeat_queue.pop(guild_id, None)

    if vc:
        vc.stop()
        await vc.disconnect()
        await ctx.send("⏹️ Воспроизведение остановлено, бот отключился.")
    else:
        await ctx.send("❌ Бот не в голосовом канале.")


@bot.command(name="queue", aliases=["q"])
async def queue(ctx: commands.Context):
    """Показать очередь."""
    guild_id = ctx.guild.id
    np_song = now_playing.get(guild_id)
    q = queues.get(guild_id, [])

    if not np_song and not q:
        await ctx.send("📭 Очередь пуста.")
        return

    text = ""
    if np_song:
        text += f"▶️ **Сейчас играет:** {np_song['title']}\n\n"
    text += "**Очередь:**\n"
    if q:
        for i, song in enumerate(q, 1):
            text += f"{i}. {song['title']}\n"
    else:
        text += "_Очередь пуста._"

    await ctx.send(text)


@bot.command(name="volume", aliases=["vol"])
async def volume(ctx: commands.Context, level: int = None):
    """Громкость (0–100)."""
    guild_id = ctx.guild.id

    if level is None:
        current = volume_levels.get(guild_id, DEFAULT_VOLUME)
        await ctx.send(f"🔊 Текущая громкость: {int(current * 100)}%")
        return

    if level < 0 or level > 100:
        await ctx.send("❌ Громкость: от 0 до 100.")
        return

    volume_levels[guild_id] = level / 100

    if ctx.voice_client and ctx.voice_client.is_playing() and now_playing.get(guild_id):
        await _restart_current(ctx)

    await ctx.send(f"🔊 Громкость: {level}%")


@bot.command(name="bassboost", aliases=["bb"])
async def bassboost(ctx: commands.Context):
    """Вкл/выкл басбуст."""
    guild_id = ctx.guild.id
    on = not bass_boost_on.get(guild_id, False)
    bass_boost_on[guild_id] = on

    if ctx.voice_client and ctx.voice_client.is_playing() and now_playing.get(guild_id):
        await _restart_current(ctx)

    await ctx.send("🔊 **Басбуст включён** (+15 дБ на низких частотах)." if on
                   else "🔇 **Басбуст выключен.**")


# ── ДОБАВЛЕНО: Команда повтора ────────────────────────────
@bot.command(name="repeat", aliases=["loop", "повтор"])
async def repeat(ctx: commands.Context, mode: str = ""):
    """Управление повтором: !repeat (текущий трек) или !repeat queue (вся очередь)."""
    guild_id = ctx.guild.id
    queues.setdefault(guild_id, [])
    mode = mode.lower()

    if mode in ["queue", "очередь", "q"]:
        repeat_queue[guild_id] = not repeat_queue.get(guild_id, False)
        if repeat_queue[guild_id]:
            repeat_current[guild_id] = False  # Взаимоисключающие режимы
        await ctx.send(f"🔁 Повтор очереди: **{'включён' if repeat_queue[guild_id] else 'выключен'}**")
    else:
        repeat_current[guild_id] = not repeat_current.get(guild_id, False)
        if repeat_current[guild_id]:
            repeat_queue[guild_id] = False  # Взаимоисключающие режимы
        await ctx.send(f"🔁 Повтор текущего трека: **{'включён' if repeat_current[guild_id] else 'выключен'}**")


@bot.command(name="np")
async def now_playing_cmd(ctx: commands.Context):
    """Что играет сейчас."""
    guild_id = ctx.guild.id
    np_song = now_playing.get(guild_id)
    if np_song:
        bb = "🔊 Басбуст ON" if bass_boost_on.get(guild_id) else "🔇 Басбуст OFF"
        vol = int(volume_levels.get(guild_id, DEFAULT_VOLUME) * 100)

        # ── ДОБАВЛЕНО: Отображение статуса повтора ──
        rep = ""
        if repeat_current.get(guild_id, False):
            rep = " | 🔁 Повтор трека"
        elif repeat_queue.get(guild_id, False):
            rep = " | 🔁 Повтор очереди"

        await ctx.send(f"▶️ **{np_song['title']}**\nГромкость: {vol}% | {bb}{rep}")
    else:
        await ctx.send("❌ Сейчас ничего не играет.")


# ── Запуск ────────────────────────────────────────────────
if __name__ == "__main__":
    token = BOT_TOKEN.strip()

    if shutil.which("ffmpeg") is None:
        print("⚠️ ffmpeg НЕ найден в PATH! Локальные mp3 и ссылки играть не будут.")
        print("   Установите ffmpeg: https://ffmpeg.org/download.html (или: winget install ffmpeg)")
        print()

    if not token or token.startswith("ВАШ_НОВЫЙ") or len(token) < 30:
        print("❌ BOT_TOKEN не задан или похож на заглушку.")
        print("Как получить токен: Developer Portal -> Bot -> Reset Token -> Copy")
        raise SystemExit(1)

    try:
        bot.run(token)
    except discord.LoginFailure:
        print()
        print("❌ Discord отверг токен (401 / Improper token).")
        print("Сгенерируй новый: Developer Portal -> Bot -> Reset Token -> Copy.")