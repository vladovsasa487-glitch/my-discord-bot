# Импорты
import asyncio
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import re
import shutil
import threading
import discord
from discord.ext import commands
import yt_dlp

# Путь к ffmpeg.exe (используется на Windows, если файл существует; на Linux берётся из PATH)
FFMPEG_PATH = r"C:\Users\user\AppData\Roaming\JetBrains\PyCharm2026.3\HomeFiles\ffmpeg.exe"
COOKIES_FILE_PATH = "cookies.txt"

STOP_WORDS_PATTERN = (
    r"(?:"
    r"стоп|останови(?:сь)?|останавливай(?:ся)?|"
    r"хватит|достаточно|довольно|баста|отбой|"
    r"выруби|выключи|отруби|отключи(?:сь)?|офф|off|"
    r"выйди|уйди|вали|свали|свободен|гуляй|покеда|пока|"
    r"закругляй(?:ся)?|завязывай|тормози|пауза\s+стоп|"
    r"тихо|тише|замолчи|заткнись|не\s+надо|не\s+включай\s+больше|очисти\s+очередь"
    r")"
)


def resolve_ffmpeg() -> str:
    if os.path.isfile(FFMPEG_PATH):
        return FFMPEG_PATH
    return shutil.which("ffmpeg") or "ffmpeg"


def prepare_cookies_file() -> str | None:
    cookies_env = os.getenv("YTDLP_COOKIES", "").strip()
    if cookies_env:
        with open(COOKIES_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(cookies_env)
        return COOKIES_FILE_PATH
    if os.path.isfile(COOKIES_FILE_PATH):
        return COOKIES_FILE_PATH
    return None


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return


def start_web_server() -> None:
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"HTTP-сервер запущен на порту {port}")


# Вспомогательные функции (поиск трека и стоп-команды)
def parse_macan_command(content: str) -> tuple[bool, str]:
    match = re.search(r"(?:^|[^\wа-яё])макан(?:$|[^\wа-яё])", content, flags=re.IGNORECASE)
    if not match:
        return False, ""

    tail = content[match.end():].strip(" ,.:;!?-—\n\t")
    tail = re.sub(
        r"^(?:пожалуйста\s+)?(?:включи|поставь|сыграй|найди|запусти|вруби)(?:\s+(?:мне|нам))?(?:\s+(?:трек|песню|музыку|клип))?\s*",
        "",
        tail,
        flags=re.IGNORECASE,
    ).strip(" ,.:;!?-—\n\t")

    return True, tail


def is_macan_stop_command(content: str) -> bool:
    cleaned = re.sub(
        r"\b(?:пожалуйста|плиз|pls|брат|бро)\b",
        " ",
        content.strip(),
        flags=re.IGNORECASE,
    )
    return bool(
        re.search(
            rf"\bмакан\b[\s,.:;!?-—]*(?:{STOP_WORDS_PATTERN})\b",
            cleaned,
            flags=re.IGNORECASE,
        )
    )


intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

BASE_YDL_OPTIONS = {
    "format": "bestaudio[ext=m4a]/bestaudio/best",
    "noplaylist": True,
    "quiet": False,
    "no_warnings": False,
    "extractor_args": {
        "youtube": {
            "player_client": ["web", "mweb"],
        }
    },
}

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

FFMPEG_OPTIONS = {
    "before_options": "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


def _try_extract(query: str, extra_opts: dict | None = None) -> dict | None:
    opts = dict(BASE_YDL_OPTIONS)
    cookiefile = prepare_cookies_file()
    if cookiefile:
        opts["cookiefile"] = cookiefile
    if extra_opts:
        opts.update(extra_opts)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=False)

    if not info:
        return None

    if "entries" in info:
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            return None
        info = entries[0]

    stream_url = info.get("url")
    if not stream_url:
        return None

    headers = dict(DEFAULT_HEADERS)
    if isinstance(info.get("http_headers"), dict):
        headers.update(info["http_headers"])

    return {
        "title": info.get("title") or "Без названия",
        "url": stream_url,
        "headers": headers,
        "source": info.get("extractor_key") or "unknown",
    }

def extract_track_info(query: str) -> dict | None:
    cleaned = query.strip()
    if not cleaned:
        return None

    # 1. Пробуем прямой запрос или поиск на YouTube
    primary_query = cleaned if cleaned.startswith(("http://", "https://")) else f"ytsearch1:{cleaned}"
    try:
        track = _try_extract(primary_query)
        if track:
            return track
    except Exception as exc:
        print(f"YouTube extract ошибка: {exc}")

    # 2. Запасной вариант — поиск через SoundCloud (берём первые 3, чтобы пропустить DRM-треки)
    if not cleaned.startswith(("http://", "https://")):
        for idx in (1, 2, 3):
            try:
                track = _try_extract(f"scsearch{idx}:{cleaned}")
                if track:
                    return track
            except Exception as exc:
                print(f"SoundCloud extract ошибка ({idx}): {exc}")

    return None


players: dict[int, "MusicPlayer"] = {}


# Кнопки управления панелью с музыкой
class MusicControlView(discord.ui.View):
    def __init__(self, player: "MusicPlayer"):
        super().__init__(timeout=None)
        self.player = player

    @discord.ui.button(emoji="⏪", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.player.play_previous()

    @discord.ui.button(emoji="⏸️", style=discord.ButtonStyle.primary)
    async def pause_resume_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self.player.guild.voice_client
        await interaction.response.defer()
        if vc is None:
            return

        if vc.is_playing():
            vc.pause()
        elif vc.is_paused():
            vc.resume()

        if interaction.channel is not None:
            await self.player.move_panel_to_bottom(interaction.channel)

    @discord.ui.button(emoji="⏩", style=discord.ButtonStyle.secondary)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self.player.guild.voice_client
        await interaction.response.defer()
        if vc is not None and (vc.is_playing() or vc.is_paused()):
            vc.stop()

    @discord.ui.button(emoji="🟥", style=discord.ButtonStyle.danger)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        vc = self.player.guild.voice_client
        self.player.queue.clear()
        self.player.history.clear()
        self.player.current = None

        if vc is not None:
            if vc.is_playing() or vc.is_paused():
                vc.stop()
            if vc.is_connected():
                await vc.disconnect(force=True)

        await self.player.delete_panel()


# Музыкальный плеер и плавающая панель
class MusicPlayer:
    def __init__(self, guild: discord.Guild):
        self.guild = guild
        self.queue: deque[dict] = deque()
        self.history: deque[dict] = deque(maxlen=20)
        self.current: dict | None = None
        self.panel_message: discord.Message | None = None
        self.panel_channel_id: int | None = None
        self._skip_history: bool = False

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="Макан • Музыкальная панель", color=discord.Color.blurple())
        if self.current:
            embed.add_field(name="Сейчас играет", value=f"**{self.current['title']}**", inline=False)
        else:
            embed.add_field(name="Сейчас играет", value="Ничего не играет", inline=False)

        if self.queue:
            upcoming = "\n".join(f"{i + 1}. {item['title']}" for i, item in enumerate(list(self.queue)[:5]))
            embed.add_field(name="В очереди", value=upcoming, inline=False)
        else:
            embed.add_field(name="В очереди", value="Пусто", inline=False)
        return embed

    async def move_panel_to_bottom(self, channel: discord.abc.Messageable):
        if self.panel_message is not None:
            try:
                await self.panel_message.delete()
            except discord.HTTPException:
                pass
            self.panel_message = None

        self.panel_channel_id = getattr(channel, "id", None)
        self.panel_message = await channel.send(
            embed=self.build_embed(),
            view=MusicControlView(self),
        )

    async def delete_panel(self):
        if self.panel_message is not None:
            try:
                await self.panel_message.delete()
            except discord.HTTPException:
                pass
            self.panel_message = None

    async def refresh_panel(self):
        if self.panel_channel_id is None:
            return
        channel = self.guild.get_channel(self.panel_channel_id)
        if isinstance(channel, discord.abc.Messageable):
            await self.move_panel_to_bottom(channel)

    async def play_previous(self):
        vc = self.guild.voice_client
        if vc is None or not vc.is_connected() or not self.history:
            return

        prev_track = self.history.pop()
        if self.current is not None:
            self.queue.appendleft(self.current)

        self.queue.appendleft(prev_track)
        self._skip_history = True

        if vc.is_playing() or vc.is_paused():
            vc.stop()
        else:
            await self.play_next()

    async def play_next(self):
        vc = self.guild.voice_client
        if vc is None or not vc.is_connected():
            self.current = None
            self.queue.clear()
            self.history.clear()
            await self.delete_panel()
            return

        if self.current is not None and not self._skip_history:
            self.history.append(self.current)
        self._skip_history = False

        if not self.queue:
            self.current = None
            await self.delete_panel()
            return

        self.current = self.queue.popleft()
        ffmpeg_exe = resolve_ffmpeg()

headers = self.current.get("headers") or DEFAULT_HEADERS
user_agent = headers.get("User-Agent") or DEFAULT_HEADERS["User-Agent"]
safe_ua = user_agent.replace("\r", "").replace("\n", "").replace('"', "")
before_opts = (
    f'-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 '
    f'-user_agent "{safe_ua}"'
)

        source = discord.FFmpegPCMAudio(
            self.current["url"],
            executable=ffmpeg_exe,
            before_options=before_opts,
            options="-vn",
        )

        def after_playing(error: Exception | None):
            fut = asyncio.run_coroutine_threadsafe(self.play_next(), bot.loop)
            try:
                fut.result(timeout=10)
            except Exception:
                pass

        vc.play(source, after=after_playing)
        await self.refresh_panel()


# Реакция на обращение к боту
def get_player(guild: discord.Guild) -> MusicPlayer:
    player = players.get(guild.id)
    if player is None:
        player = MusicPlayer(guild)
        players[guild.id] = player
    return player


@bot.event
async def on_ready():
    print(f"Бот запущен как {bot.user} (ID: {bot.user.id})")


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    if member.bot or member.guild is None:
        return

    vc = member.guild.voice_client
    if vc is None or not vc.is_connected() or vc.channel is None:
        return

    # Реагируем только если кто-то вышел из канала бота или перешёл из него в другой
    if before.channel != vc.channel or after.channel == vc.channel:
        return

    # Считаем оставшихся людей (без учёта самого бота)
    remaining_humans = [m for m in vc.channel.members if not m.bot]
    if remaining_humans:
        return

    player = get_player(member.guild)
    player.queue.clear()
    player.history.clear()
    player.current = None

    if vc.is_playing() or vc.is_paused():
        vc.stop()

    await vc.disconnect(force=True)
    await player.delete_panel()


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return

    called, query = parse_macan_command(message.content)
    if not called:
        return

    try:
        await message.delete()
    except discord.HTTPException:
        pass

    player = get_player(message.guild)
    player.panel_channel_id = message.channel.id

    if is_macan_stop_command(message.content):
        vc = message.guild.voice_client
        player.queue.clear()
        player.history.clear()
        player.current = None

        if vc is not None:
            if vc.is_playing() or vc.is_paused():
                vc.stop()
            if vc.is_connected():
                await vc.disconnect(force=True)

        await player.delete_panel()
        return

    if not query:
        await player.move_panel_to_bottom(message.channel)
        return

    voice_state = getattr(message.author, "voice", None)
    voice_channel = getattr(voice_state, "channel", None)
    if voice_channel is None:
        await player.move_panel_to_bottom(message.channel)
        return

    vc = message.guild.voice_client
    if vc is None:
        vc = await voice_channel.connect()
    elif vc.channel != voice_channel:
        await vc.move_to(voice_channel)

    track = await asyncio.to_thread(extract_track_info, query)
    if not track:
        await player.move_panel_to_bottom(message.channel)
        return

    player.queue.append(track)
    if not vc.is_playing() and not vc.is_paused():
        await player.play_next()
    else:
        await player.move_panel_to_bottom(message.channel)


# Запуск бота
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("Не задан DISCORD_TOKEN")

start_web_server()
bot.run(TOKEN)
