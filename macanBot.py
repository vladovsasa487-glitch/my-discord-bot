# Импорты
import asyncio
import os
import re
from collections import deque
import discord
from discord.ext import commands
import yt_dlp
import shutil


def resolve_ffmpeg() -> str:
    if os.path.isfile(FFMPEG_PATH):
        return FFMPEG_PATH
    return shutil.which("ffmpeg") or "ffmpeg"

# Путь к ffmpeg.exe
FFMPEG_PATH = r"C:\Users\user\AppData\Roaming\JetBrains\PyCharm2026.3\HomeFiles\ffmpeg.exe"

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

YDL_OPTIONS = {
    "format": "bestaudio[acodec=opus]/bestaudio[ext=m4a]/bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": False,
}

FFMPEG_OPTIONS = {
    "before_options": "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


def resolve_ffmpeg() -> str:
    if os.path.isfile(FFMPEG_PATH):
        return FFMPEG_PATH
    return "ffmpeg"


def extract_track_info(query: str) -> dict | None:
    search_query = (
        query.strip()
        if query.strip().startswith(("http://", "https://"))
        else f"ytsearch1:{query.strip()}"
    )
    with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
        info = ydl.extract_info(search_query, download=False)

    if not info:
        return None

    if "entries" in info:
        entries = info.get("entries") or []
        if not entries:
            return None
        info = entries[0]

    if not info or not info.get("url"):
        return None

    return {
        "title": info.get("title") or "Без названия",
        "url": info["url"],
    }


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
        source = discord.FFmpegPCMAudio(
            self.current["url"],
            executable=ffmpeg_exe,
            **FFMPEG_OPTIONS,
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

bot.run(TOKEN)