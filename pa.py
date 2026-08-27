import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from typing import List, Optional

import discord
from redbot.core import Config, app_commands, commands
from redbot.core.bot import Red

log = logging.getLogger("red.pa")

DEFAULT_DELAY = 5.5  # seconds to linger in each channel; default soundboard clips run up to ~5.2s
HELPER_READY_TIMEOUT = 20.0  # seconds to wait for a helper bot to finish logging in
MAX_CHANNELS_PER_RUN = 200  # safety net against a channel list that never settles


class _SoundRef:
    """A minimal stand-in for a soundboard sound, usable by any bot identity.

    discord.py's ``VoiceChannel.send_sound()`` reads ``sound.id`` and, for
    non-default sounds, ``sound.guild.id`` (to decide whether to include
    ``source_guild_id`` for cross-server sound sharing). The real sound
    object returned by ``guild.soundboard_sounds`` or
    ``fetch_soundboard_default_sounds()`` is only meaningful to the client
    that fetched it -- so to let a *different* bot identity (a helper client)
    trigger the same sound, we hand it this instead. As long as ``guild``
    matches the channel's own guild (always true for us -- we never trigger
    a sound borrowed from another server), the cross-server branch is
    skipped and this behaves identically to the original sound object.
    """

    __slots__ = ("id", "guild")

    def __init__(self, sound_id: int, guild: discord.Guild) -> None:
        self.id = sound_id
        self.guild = guild


class PA(commands.Cog):
    """Deliver a soundboard announcement to every populated voice channel.

    Joins each populated voice channel via a bare gateway voice-state update
    (Guild.change_voice_state) -- no VoiceClient, no PyNaCl, no ffmpeg/opus --
    fires the chosen soundboard sound, waits out the delay so it can finish,
    then moves to the next channel. The channel list is rescanned live as it
    walks, so newly-populated channels get swept up too.

    Optionally, additional bot tokens (configured via Red's shared API token
    store) let multiple bot identities each hold their own voice-state slot
    in the same guild simultaneously, walking different channels in
    parallel. A single bot token can only ever be in one voice channel per
    guild at a time -- that's a Discord platform limit, not a library one --
    so this is the only way to get real concurrency here.
    """

    __version__ = "1.1.0"
    __author__ = "you"

    def __init__(self, bot: Red) -> None:
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xBADA55, force_registration=True)
        self.config.register_guild(
            delay=DEFAULT_DELAY,
            skip_afk=True,
            skip_bot_only_channels=True,
            randomize=False,
            allowed_roles=[],
            allowed_users=[],
            cooldown=0.0,
        )
        # Guards against overlapping /pa runs in the same guild.
        self._running_guilds: set = set()
        # Live progress for the in-flight run in each guild, keyed by guild id:
        # {"sound": str, "started_at": int, "pending_estimate": int,
        #  "visited": [(channel, sent, detail, unix_ts, label), ...]}
        # Built up as _run_broadcast walks the channels, and disposed of (popped)
        # once the run finishes -- it's only meant to answer "how far along are we"
        # for a run in progress, not to persist afterward.
        self._progress: dict = {}
        # One cancellation flag per in-flight run, keyed by guild id. Set by
        # !pacancel; workers check it before claiming their next channel, so
        # in-flight visits finish cleanly but no new ones start. Disposed of
        # (popped) when the run ends, same lifecycle as _progress.
        self._cancel_events: dict = {}
        # Monotonic timestamp of when each guild last *started* a broadcast,
        # purely in-memory (a cooldown resetting on a bot restart is fine --
        # it's a spam guard, not an audit record).
        self._last_run_at: dict = {}

    def cog_unload(self) -> None:
        # Nothing persistent is held between runs -- helper clients are
        # spun up and torn down within a single /pa invocation.
        pass

    # ------------------------------------------------------------------
    # Helper-bot pool (optional extra tokens for parallel channels)
    # ------------------------------------------------------------------

    async def _get_helper_tokens(self) -> List[str]:
        """Extra bot tokens configured via `!set api pa token1 ... token2 ...`."""
        tokens_dict = await self.bot.get_shared_api_tokens("pa")

        def sort_key(key: str) -> int:
            try:
                return int(key.replace("token", ""))
            except ValueError:
                return 0

        ordered_keys = sorted(
            (k for k in tokens_dict if k.startswith("token")), key=sort_key
        )
        return [tokens_dict[k] for k in ordered_keys if tokens_dict.get(k)]

    async def _spawn_helpers(self, tokens: List[str]) -> List[discord.Client]:
        """Log in every extra token as a minimal Client, concurrently.

        Logging these in one at a time would mean worst-case wait time grows
        linearly with the number of tokens (8 tokens x a 20s timeout each =
        up to 160s just to start). Kicking off every login at once and then
        waiting on all of them together keeps worst-case wait time to a
        single HELPER_READY_TIMEOUT window no matter how many tokens there
        are. Returns only the ones that made it in time.
        """
        intents = discord.Intents.none()
        intents.guilds = True

        clients: List[discord.Client] = []
        for token in tokens:
            client = discord.Client(intents=intents)
            task = asyncio.ensure_future(client.start(token))
            client._pa_start_task = task  # stash for clean shutdown later
            clients.append(client)

        async def wait_for_one(index: int, client: discord.Client) -> Optional[discord.Client]:
            try:
                await asyncio.wait_for(client.wait_until_ready(), timeout=HELPER_READY_TIMEOUT)
                return client
            except (asyncio.TimeoutError, discord.LoginFailure, discord.HTTPException) as exc:
                log.warning("Helper token %d failed to connect: %s", index, exc)
                await self._close_one_helper(client)
                return None

        results = await asyncio.gather(
            *(wait_for_one(i, c) for i, c in enumerate(clients, start=1))
        )
        return [c for c in results if c is not None]

    async def _close_one_helper(self, client: discord.Client) -> None:
        try:
            await client.close()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            log.warning("Error closing helper client", exc_info=True)
        task = getattr(client, "_pa_start_task", None)
        if task is not None and not task.done():
            task.cancel()

    async def _shutdown_helpers(self, helpers: List[discord.Client]) -> None:
        for client in helpers:
            await self._close_one_helper(client)

    # ------------------------------------------------------------------
    # Sound lookup
    # ------------------------------------------------------------------

    async def _collect_sounds(self, guild: discord.Guild) -> List[discord.BaseSoundboardSound]:
        """Custom guild sounds + Discord's default sound library."""
        sounds: List[discord.BaseSoundboardSound] = list(guild.soundboard_sounds or [])
        try:
            sounds.extend(await self.bot.fetch_soundboard_default_sounds())
        except discord.HTTPException:
            log.warning("Could not fetch default soundboard sounds", exc_info=True)
        return sounds

    async def _resolve_sound_query(
        self, guild: discord.Guild, query: str
    ) -> tuple:
        """Resolve a sound from either a slash-autocomplete ID or a typed name.

        Returns (sound, candidates). ``sound`` is set on an unambiguous match.
        Otherwise ``sound`` is None and ``candidates`` holds 0+ near-matches
        so the caller can report what's available.
        """
        sounds = await self._collect_sounds(guild)

        # Slash autocomplete always sends the numeric sound ID as the value.
        if query.isdigit():
            sound_id = int(query)
            for sound in sounds:
                if sound.id == sound_id:
                    return sound, []

        def is_custom(s: discord.BaseSoundboardSound) -> bool:
            return isinstance(s, discord.SoundboardSound)

        def pick_best(matches: List[discord.BaseSoundboardSound]):
            if len(matches) == 1:
                return matches[0], []
            # If exactly one candidate is this server's own custom sound
            # (as opposed to one of Discord's shared default sounds),
            # that's almost always what was meant -- prefer it.
            custom = [s for s in matches if is_custom(s)]
            if len(custom) == 1:
                return custom[0], []
            return None, matches

        query_stripped = query.strip()
        query_lower = query_stripped.lower()

        # 1. Exact, case-sensitive match.
        exact_case = [s for s in sounds if s.name == query_stripped]
        if exact_case:
            result, candidates = pick_best(exact_case)
            if result is not None:
                return result, []

        # 2. Exact, case-insensitive match.
        exact_ci = [s for s in sounds if s.name.lower() == query_lower]
        if exact_ci:
            result, candidates = pick_best(exact_ci)
            if result is not None:
                return result, []
            return None, candidates

        # 3. Substring match.
        partial = [s for s in sounds if query_lower in s.name.lower()]
        result, candidates = pick_best(partial)
        return result, candidates

    def _populated_voice_channels(
        self,
        guild: discord.Guild,
        *,
        skip_afk: bool,
        skip_bot_only_channels: bool,
    ) -> List[discord.VoiceChannel]:
        channels = []
        for channel in guild.voice_channels:
            if skip_afk and guild.afk_channel is not None and channel.id == guild.afk_channel.id:
                continue
            members = channel.members
            if not members:
                continue
            if skip_bot_only_channels and all(m.bot for m in members):
                continue
            channels.append(channel)
        return channels

    async def _check_pa_access(self, author: discord.abc.User, guild: discord.Guild) -> bool:
        """True if ``author`` may run /pa in this guild.

        Server admins (or the owner) can always run it. Beyond that, the
        default is wide open -- deliberately -- so Discord's own per-command
        permission UI (Server Settings -> Integrations, for the slash command)
        is the primary gate, matching how most guilds already manage command
        access. The allowed_roles/allowed_users lists are an optional,
        additional restriction on top of that, mainly useful for gating the
        text-prefix form (`!pa`), which has no equivalent native Discord UI.
        If neither list has anything in it, everyone is allowed.
        """
        if isinstance(author, discord.Member):
            perms = author.guild_permissions
            if perms.administrator or perms.manage_guild or author.id == guild.owner_id:
                return True

        settings = await self.config.guild(guild).all()
        allowed_roles = set(settings.get("allowed_roles", []))
        allowed_users = set(settings.get("allowed_users", []))
        if not allowed_roles and not allowed_users:
            return True

        if author.id in allowed_users:
            return True
        if isinstance(author, discord.Member) and any(r.id in allowed_roles for r in author.roles):
            return True
        return False

    # ------------------------------------------------------------------
    # The broadcast itself
    # ------------------------------------------------------------------

    async def _visit_one_channel(
        self,
        identity: discord.Client,
        label: str,
        guild_id: int,
        display_channel: discord.VoiceChannel,
        sound_id: int,
    ) -> tuple:
        """One identity joins ``display_channel``, fires the sound, and reports what happened.

        ``display_channel`` comes from the *main* bot's cache (it's the one
        used to decide what's populated); each identity looks up its own
        Guild/Channel objects from its own cache to act on, since permissions
        and voice state are all per-identity.
        """
        ts = int(datetime.now(timezone.utc).timestamp())

        if not display_channel.members:
            return (display_channel, False, "no members present", ts, label)

        identity_guild = identity.get_guild(guild_id)
        if identity_guild is None:
            return (display_channel, False, f"{label} isn't in this server", ts, label)
        identity_channel = identity_guild.get_channel(display_channel.id)
        if identity_channel is None:
            return (display_channel, False, f"{label} can't see this channel", ts, label)

        me = identity_guild.me
        perms = identity_channel.permissions_for(me)
        missing = []
        if not perms.view_channel:
            missing.append("View Channel")
        if not perms.connect:
            missing.append("Connect")
        if not getattr(perms, "speak", True):
            missing.append("Speak")
        if not getattr(perms, "use_soundboard", True):
            missing.append("Use Soundboard")
        if missing:
            return (
                display_channel,
                False,
                f"missing permission(s): {', '.join(missing)}",
                ts,
                label,
            )

        if me.voice is not None and (me.voice.mute or me.voice.deaf):
            return (
                display_channel,
                False,
                "server-muted/deafened -- an admin needs to clear that",
                ts,
                label,
            )

        try:
            await identity_guild.change_voice_state(
                channel=identity_channel, self_mute=False, self_deaf=False
            )
            # Give the gateway a beat to register the state server-side
            # before we hit the HTTP endpoint that checks it.
            await asyncio.sleep(0.75)
        except discord.HTTPException as exc:
            return (display_channel, False, f"couldn't join channel: {exc}", ts, label)

        try:
            await identity_channel.send_sound(_SoundRef(sound_id, identity_guild))
        except discord.Forbidden as exc:
            detail = f"Discord refused (403): {exc.text or 'no reason given'}"
            log.warning("send_sound Forbidden in %s via %s: %s", display_channel.name, label, exc.text)
            return (display_channel, False, detail, ts, label)
        except discord.HTTPException as exc:
            detail = f"HTTP {exc.status}: {exc.text or exc}"
            log.warning("send_sound failed in %s via %s: %s", display_channel.name, label, exc.text)
            return (display_channel, False, detail, ts, label)

        return (display_channel, True, None, ts, label)

    async def _run_broadcast(
        self,
        guild: discord.Guild,
        sound_id: int,
        delay: float,
        *,
        skip_afk: bool,
        skip_bot_only_channels: bool,
        randomize: bool,
        helpers: List[discord.Client],
        cancel_event: asyncio.Event,
    ) -> List[tuple]:
        """Walk populated voice channels with one or more bot identities in parallel.

        Discord's API requires the bot's *voice state* to already be in the
        target channel before it'll accept send_sound() (403: "User must be
        in voice channel to send voice channel effect"). That's a gateway-level
        fact (member.voice.channel_id), separate from actually negotiating an
        encrypted RTP audio session -- and since we never send any audio of
        our own, we only need the former. Guild.change_voice_state() sends
        just that gateway packet without opening a VoiceClient, so this needs
        no PyNaCl/opus/ffmpeg at all.

        A single bot token can only hold one voice state per guild, so each
        extra ``helpers`` client is a genuinely separate bot identity that
        can hold its *own* voice state concurrently -- that's what actually
        parallelizes the walk, not threading or asyncio tricks on one token.

        The channel list is recomputed fresh on every claim rather than
        snapshotted once up front, so a channel that fills up partway through
        a long walk gets swept up too. A local, run-scoped set (behind a
        lock, since multiple identities claim concurrently) tracks which
        channels have already been visited *this run*; it only exists for
        the life of this call. A hard cap (MAX_CHANNELS_PER_RUN) guards
        against a channel list that never settles.

        ``cancel_event``, set by !pacancel, is checked before every claim --
        once set, no worker starts a new channel, but whichever channel each
        worker is already mid-visit on is allowed to finish cleanly.

        Returns a list of (channel, sent: bool, detail: Optional[str], unix_ts: int, label: str).
        """
        results: List[tuple] = []
        visited_ids: set = set()
        lock = asyncio.Lock()
        progress = self._progress[guild.id]

        async def claim_next() -> Optional[discord.VoiceChannel]:
            async with lock:
                if cancel_event.is_set():
                    return None
                if len(visited_ids) >= MAX_CHANNELS_PER_RUN:
                    return None
                pending = [
                    c
                    for c in self._populated_voice_channels(
                        guild, skip_afk=skip_afk, skip_bot_only_channels=skip_bot_only_channels
                    )
                    if c.id not in visited_ids
                ]
                if not pending:
                    return None
                channel = random.choice(pending) if randomize else pending[0]
                visited_ids.add(channel.id)
                progress["pending_estimate"] = len(pending) - 1
                return channel

        async def worker_loop(identity: discord.Client, label: str) -> None:
            while True:
                channel = await claim_next()
                if channel is None:
                    return
                entry = await self._visit_one_channel(identity, label, guild.id, channel, sound_id)
                results.append(entry)
                progress["visited"].append(entry)
                if entry[1]:
                    await asyncio.sleep(delay)

        identities = [self.bot] + helpers
        labels = ["main bot"] + [f"helper {i}" for i in range(1, len(helpers) + 1)]

        try:
            await asyncio.gather(
                *(worker_loop(identity, label) for identity, label in zip(identities, labels))
            )
        finally:
            # Always leave voice when done (or if something went wrong), for
            # every identity that took part, regardless of where each ended up.
            for identity in identities:
                identity_guild = identity.get_guild(guild.id)
                if identity_guild is not None:
                    try:
                        await identity_guild.change_voice_state(channel=None)
                    except discord.HTTPException:
                        pass
        return results

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @commands.hybrid_command(name="pa")
    @commands.guild_only()
    @app_commands.describe(sound="The soundboard sound to broadcast")
    async def pa(self, ctx: commands.Context, sound: str) -> None:
        """Broadcast a soundboard PSA to every populated voice channel."""
        guild = ctx.guild
        if guild is None:
            return

        if not await self._check_pa_access(ctx.author, guild):
            await ctx.send("You don't have permission to trigger a PA announcement here.")
            return

        if guild.id in self._running_guilds:
            await ctx.send("A PA announcement is already in progress in this server.")
            return

        cooldown = await self.config.guild(guild).cooldown()
        last_run = self._last_run_at.get(guild.id)
        if cooldown and last_run is not None:
            elapsed = time.monotonic() - last_run
            if elapsed < cooldown:
                remaining = cooldown - elapsed
                await ctx.send(
                    f"PA is on cooldown for another {remaining:.0f}s -- try again shortly."
                )
                return

        chosen, candidates = await self._resolve_sound_query(guild, sound)
        if chosen is None:
            if candidates:
                names = ", ".join(f"`{s.name}`" for s in candidates[:10])
                await ctx.send(f"That matched more than one sound: {names}. Be more specific.")
            else:
                await ctx.send(
                    "I couldn't find a soundboard sound by that name. "
                    "Run `!pasounds` to see what's available."
                )
            return

        if guild.voice_client is not None:
            await ctx.send(
                "I'm already connected to a voice channel in this server "
                "(maybe playing music) — disconnect me first, then try again."
            )
            return

        settings = await self.config.guild(guild).all()
        initial_channels = self._populated_voice_channels(
            guild,
            skip_afk=settings["skip_afk"],
            skip_bot_only_channels=settings["skip_bot_only_channels"],
        )

        if not initial_channels:
            await ctx.send("There aren't any populated voice channels right now.")
            return

        tokens = await self._get_helper_tokens()
        helpers: List[discord.Client] = []
        last_status: Optional[discord.Message] = None
        if tokens:
            last_status = await self._send_transient(
                ctx, f"Connecting {len(tokens)} helper bot(s) for parallel coverage...", last_status
            )
            helpers = await self._spawn_helpers(tokens)
            if len(helpers) < len(tokens):
                last_status = await self._send_transient(
                    ctx,
                    f"Only {len(helpers)}/{len(tokens)} helper bot(s) connected in time -- "
                    "continuing with those (check the console log for why the rest failed).",
                    last_status,
                )

        worker_count = 1 + len(helpers)
        parallel_note = (
            f" using {worker_count} bots in parallel" if worker_count > 1 else ""
        )
        last_status = await self._send_transient(
            ctx,
            f"📢 Broadcasting **{chosen.name}** to {len(initial_channels)}+ voice channel(s)"
            f"{parallel_note} (channels that fill up mid-walk will be swept up too)...",
            last_status,
        )

        started_at = int(datetime.now(timezone.utc).timestamp())
        self._running_guilds.add(guild.id)
        self._last_run_at[guild.id] = time.monotonic()
        cancel_event = asyncio.Event()
        self._cancel_events[guild.id] = cancel_event
        self._progress[guild.id] = {
            "sound": chosen.name,
            "started_at": started_at,
            "pending_estimate": len(initial_channels),
            "visited": [],
        }
        try:
            results = await self._run_broadcast(
                guild,
                chosen.id,
                settings["delay"],
                skip_afk=settings["skip_afk"],
                skip_bot_only_channels=settings["skip_bot_only_channels"],
                randomize=settings["randomize"],
                helpers=helpers,
                cancel_event=cancel_event,
            )
        finally:
            self._running_guilds.discard(guild.id)
            self._progress.pop(guild.id, None)
            self._cancel_events.pop(guild.id, None)
            await self._shutdown_helpers(helpers)
            # Clear the last transient status message -- the final report
            # (sent next) is the only thing that should stick around.
            if last_status is not None:
                try:
                    await last_status.delete()
                except discord.HTTPException:
                    pass

        cancelled = cancel_event.is_set()
        await self._send_final_report(ctx, chosen.name, results, started_at, cancelled=cancelled)

    async def _send_transient(
        self,
        ctx: commands.Context,
        content: str,
        previous: Optional[discord.Message],
    ) -> discord.Message:
        """Send a status update, deleting the prior transient one (if any) first.

        This keeps only the latest progress line visible at any moment,
        instead of leaving a trail of "connecting...", "broadcasting..." etc.
        messages behind -- the final report is meant to be the only thing
        left once a run finishes.
        """
        if previous is not None:
            try:
                await previous.delete()
            except discord.HTTPException:
                pass
        return await ctx.send(content)

    async def _send_final_report(
        self,
        ctx: commands.Context,
        sound_name: str,
        results: List[tuple],
        started_at: int,
        *,
        cancelled: bool = False,
    ) -> None:
        """The one message that sticks around: who sent what, to how many, when --
        plus the full per-channel log with timestamps tucked behind a spoiler."""
        sent_count = sum(1 for r in results if r[1])
        cancelled_note = " (cancelled early)" if cancelled else ""
        header = (
            f"📢 **{ctx.author.display_name}** sent **{sound_name}** to "
            f"**{sent_count}** channel(s) at <t:{started_at}:F>{cancelled_note}"
        )
        if not results:
            await ctx.send(header)
            return

        multi_worker = len({label for *_rest, label in results}) > 1
        lines = []
        for channel, sent, detail, ts, label in results:
            icon = "✅" if sent else "⏭️"
            line = f"{icon} **{channel.name}** — <t:{ts}:T>"
            if multi_worker:
                line += f" [{label}]"
            if not sent and detail:
                line += f" ({detail})"
            lines.append(line)

        # Pack the header in with the first chunk; only overflow (rare, on
        # very large channel counts) spills into additional messages.
        chunk = header + "\n||"
        for line in lines:
            piece = line + "\n"
            if len(chunk) + len(piece) + 2 > 1900:
                await ctx.send(chunk.rstrip("\n") + "||")
                chunk = "||"
            chunk += piece
        await ctx.send(chunk.rstrip("\n") + "||")

    @pa.autocomplete("sound")
    async def _pa_sound_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        if interaction.guild is None:
            return []
        sounds = await self._collect_sounds(interaction.guild)
        current_lower = current.lower()
        matches = [s for s in sounds if current_lower in s.name.lower()][:25]
        return [app_commands.Choice(name=s.name, value=str(s.id)) for s in matches]

    @commands.command(name="pasounds")
    @commands.guild_only()
    async def pasounds(self, ctx: commands.Context) -> None:
        """List soundboard sound names available to /pa (useful without slash autocomplete)."""
        sounds = await self._collect_sounds(ctx.guild)
        if not sounds:
            await ctx.send("No soundboard sounds found (custom or default).")
            return
        # Custom (this server's) sounds first, then Discord's shared defaults,
        # each labelled so identically-named sounds aren't a surprise later.
        custom = sorted({s.name for s in sounds if isinstance(s, discord.SoundboardSound)})
        default = sorted(
            {s.name for s in sounds if not isinstance(s, discord.SoundboardSound)}
        )

        async def send_chunks(names: List[str]) -> None:
            current = ""
            for name in names:
                piece = f"`{name}`, "
                if len(current) + len(piece) > 1900:
                    await ctx.send(current.rstrip(", "))
                    current = ""
                current += piece
            if current:
                await ctx.send(current.rstrip(", "))

        if custom:
            await ctx.send("**This server's custom sounds:**")
            await send_chunks(custom)
        if default:
            await ctx.send("**Discord's default sounds:**")
            await send_chunks(default)

    @commands.command(name="pastatus")
    @commands.guild_only()
    async def pastatus(self, ctx: commands.Context) -> None:
        """Check progress of a PA announcement currently in flight."""
        progress = self._progress.get(ctx.guild.id)
        if progress is None:
            await ctx.send("No PA announcement is currently running in this server.")
            return
        visited = progress["visited"]
        done = len(visited)
        sent = sum(1 for _c, ok, _d, _t, _l in visited if ok)
        pending = progress.get("pending_estimate", 0)
        last = f" Last: **{visited[-1][0].name}**." if visited else ""
        await ctx.send(
            f"Broadcasting **{progress['sound']}** — {done} channel(s) visited so far "
            f"({sent} played), at least {pending} more waiting.{last}"
        )

    @commands.command(name="pacancel")
    @commands.guild_only()
    async def pacancel(self, ctx: commands.Context) -> None:
        """Stop a PA announcement currently in progress in this server.

        Whichever channel each bot identity is mid-visit on is allowed to
        finish (join, fire the sound, leave) cleanly -- no new channels are
        claimed after that.
        """
        if not await self._check_pa_access(ctx.author, ctx.guild):
            await ctx.send("You don't have permission to cancel a PA announcement here.")
            return
        cancel_event = self._cancel_events.get(ctx.guild.id)
        if cancel_event is None:
            await ctx.send("No PA announcement is currently running in this server.")
            return
        if cancel_event.is_set():
            await ctx.send("Already cancelling -- finishing up the current channel(s).")
            return
        cancel_event.set()
        await ctx.send("Cancelling -- finishing the current channel(s), then stopping.")

    @commands.command(name="pahelpers")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def pahelpers(self, ctx: commands.Context) -> None:
        """Test-connect any configured helper bot tokens and report their status here."""
        tokens = await self._get_helper_tokens()
        if not tokens:
            await ctx.send(
                "No helper tokens configured. Add some with "
                "`!set api pa token1 <token> token2 <token> ...` -- each one is a separate "
                "bot Application/token that must also be invited to this server with the "
                "same voice permissions as the main bot."
            )
            return

        await ctx.send(f"Testing {len(tokens)} helper token(s)...")
        helpers = await self._spawn_helpers(tokens)
        try:
            lines = []
            for i in range(len(tokens)):
                if i >= len(helpers):
                    lines.append(f"- Token {i + 1}: failed to log in (see console log)")
                    continue
                client = helpers[i]
                identity_guild = client.get_guild(ctx.guild.id)
                if identity_guild is None:
                    lines.append(
                        f"- Token {i + 1}: logged in as **{client.user}**, but isn't in "
                        "this server -- invite it with the same voice permissions."
                    )
                else:
                    lines.append(f"- Token {i + 1}: logged in as **{client.user}**, ready here.")
            await ctx.send("\n".join(lines))
        finally:
            await self._shutdown_helpers(helpers)

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    @commands.group(name="paset")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def paset(self, ctx: commands.Context) -> None:
        """Configure the PA cog for this server."""

    @paset.command(name="delay")
    async def paset_delay(self, ctx: commands.Context, seconds: float) -> None:
        """Set how long (in seconds) to wait in each channel before moving to the next."""
        if seconds < 0 or seconds > 60:
            await ctx.send("Pick a delay between 0 and 60 seconds.")
            return
        await self.config.guild(ctx.guild).delay.set(seconds)
        await ctx.send(f"PA delay set to {seconds} seconds per channel.")

    @paset.command(name="skipafk")
    async def paset_skipafk(self, ctx: commands.Context, on_off: bool) -> None:
        """Toggle skipping the server's AFK channel."""
        await self.config.guild(ctx.guild).skip_afk.set(on_off)
        await ctx.send(f"Skipping the AFK channel is now {'on' if on_off else 'off'}.")

    @paset.command(name="skipbotonly")
    async def paset_skipbotonly(self, ctx: commands.Context, on_off: bool) -> None:
        """Toggle skipping channels that only contain bots."""
        await self.config.guild(ctx.guild).skip_bot_only_channels.set(on_off)
        await ctx.send(f"Skipping bot-only channels is now {'on' if on_off else 'off'}.")

    @paset.command(name="randomize")
    async def paset_randomize(self, ctx: commands.Context, on_off: bool) -> None:
        """Toggle randomizing the order channels are visited in."""
        await self.config.guild(ctx.guild).randomize.set(on_off)
        await ctx.send(f"Randomized channel order is now {'on' if on_off else 'off'}.")

    @paset.command(name="cooldown")
    async def paset_cooldown(self, ctx: commands.Context, seconds: float) -> None:
        """Set the minimum time between /pa runs in this server. 0 disables the cooldown."""
        if seconds < 0 or seconds > 3600:
            await ctx.send("Pick a cooldown between 0 and 3600 seconds.")
            return
        await self.config.guild(ctx.guild).cooldown.set(seconds)
        if seconds == 0:
            await ctx.send("PA cooldown disabled.")
        else:
            await ctx.send(f"PA cooldown set to {seconds:.0f}s between runs.")

    @paset.command(name="allowrole")
    async def paset_allowrole(self, ctx: commands.Context, role: discord.Role) -> None:
        """Allow a role to run /pa, in addition to the default (server admins + anyone else)."""
        async with self.config.guild(ctx.guild).allowed_roles() as roles:
            if role.id not in roles:
                roles.append(role.id)
        await ctx.send(
            f"**{role.name}** can now run `/pa`. Note: as long as this is the only "
            "restriction configured, everyone else can too -- add more roles/users, "
            "or restrict the slash command itself in Server Settings > Integrations."
        )

    @paset.command(name="denyrole")
    async def paset_denyrole(self, ctx: commands.Context, role: discord.Role) -> None:
        """Remove a role from the /pa allow-list."""
        async with self.config.guild(ctx.guild).allowed_roles() as roles:
            if role.id in roles:
                roles.remove(role.id)
        await ctx.send(f"Removed **{role.name}** from the `/pa` allow-list.")

    @paset.command(name="allowuser")
    async def paset_allowuser(self, ctx: commands.Context, user: discord.Member) -> None:
        """Allow a specific member to run /pa, in addition to the default."""
        async with self.config.guild(ctx.guild).allowed_users() as users:
            if user.id not in users:
                users.append(user.id)
        await ctx.send(f"**{user.display_name}** can now run `/pa`.")

    @paset.command(name="denyuser")
    async def paset_denyuser(self, ctx: commands.Context, user: discord.Member) -> None:
        """Remove a specific member from the /pa allow-list."""
        async with self.config.guild(ctx.guild).allowed_users() as users:
            if user.id in users:
                users.remove(user.id)
        await ctx.send(f"Removed **{user.display_name}** from the `/pa` allow-list.")

    @paset.command(name="showsettings")
    async def paset_show(self, ctx: commands.Context) -> None:
        """Show the current PA settings for this server."""
        settings = await self.config.guild(ctx.guild).all()
        role_names = []
        for role_id in settings["allowed_roles"]:
            role = ctx.guild.get_role(role_id)
            role_names.append(role.name if role else f"(deleted role {role_id})")
        user_names = []
        for user_id in settings["allowed_users"]:
            member = ctx.guild.get_member(user_id)
            user_names.append(member.display_name if member else f"(unknown user {user_id})")

        access = "anyone (default)" if not role_names and not user_names else "restricted"
        tokens = await self._get_helper_tokens()
        await ctx.send(
            "**PA settings**\n"
            f"- Delay per channel: {settings['delay']}s\n"
            f"- Skip AFK channel: {settings['skip_afk']}\n"
            f"- Skip bot-only channels: {settings['skip_bot_only_channels']}\n"
            f"- Randomize order: {settings['randomize']}\n"
            f"- Cooldown between runs: {settings['cooldown']:.0f}s\n"
            f"- Helper bot tokens configured: {len(tokens)} (run `!pahelpers` to test them)\n"
            f"- Who can run /pa: {access} (server admins can always run it)\n"
            f"  - Allowed roles: {', '.join(role_names) if role_names else 'none'}\n"
            f"  - Allowed users: {', '.join(user_names) if user_names else 'none'}"
        )
