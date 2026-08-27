# PA — Discord Soundboard PA System (Redbot cog)

Broadcasts a soundboard sound to every populated voice channel in a server, one
channel at a time (or several in parallel, if you configure helper bots) —
like an old-school building PA system, but for Discord voice channels.

## Features

- `/pa` (slash) and `!pa` (text) — pick a soundboard sound and broadcast it
- Walks every populated voice channel, skipping empty ones, the AFK channel,
  and bot-only channels (all configurable)
- Rescans live while it walks, so a channel that fills up mid-run gets swept
  up too — not just whatever was populated when the run started
- Optional random channel order
- Optional parallel broadcasting using extra bot tokens (see below)
- Per-server access control on top of Discord's own permission system
- A single, clean summary message when it's done: who sent what, to how many
  channels, at what time — with a full per-channel timestamp log tucked
  behind a spoiler tag

## Requirements

- Red-DiscordBot 3.5+
- discord.py 2.5+ (bundled with modern Red) — needed for `send_sound()` /
  soundboard support
- No PyNaCl, ffmpeg, or opus required. The cog never opens a real audio
  session; see **How it works** below.

## Installation

```
[p]addpath /path/to/folder/containing/pa
[p]load pa
```

Then enable and sync the slash command:

```
[p]slash enable pa
[p]slash sync
```

(`enable` and `sync` are separate steps — enabling alone does not push the
command to Discord. If sync says it needs the `applications.commands` scope,
your bot's invite link is missing it: run `[p]inviteset commandscope` then
`[p]invite` for a corrected invite URL.)

## Commands

| Command | Who | Description |
|---|---|---|
| `/pa <sound>` or `!pa <sound>` | See [Access control](#access-control) | Broadcast a soundboard sound to every populated voice channel |
| `!pasounds` | Anyone | List available sound names (custom + Discord defaults) — useful for the text-prefix form, which has no slash autocomplete |
| `!pastatus` | Anyone | Check progress of a broadcast currently in flight |
| `!pacancel` | Same access as `/pa` | Stop a broadcast in progress. Whichever channel each bot is mid-visit on finishes cleanly; no new channels are claimed after that |
| `!pahelpers` | Admin | Test-connect configured helper bot tokens and report their status |
| `!paset ...` | Admin | Configure the cog (see below) |

### `!paset` settings

| Subcommand | Effect |
|---|---|
| `!paset delay <seconds>` | Time to linger in each channel before moving on (default 5.5s — a bit longer than Discord's longest default soundboard clip) |
| `!paset skipafk <true/false>` | Skip the server's AFK channel |
| `!paset skipbotonly <true/false>` | Skip channels containing only bots |
| `!paset randomize <true/false>` | Randomize channel visit order |
| `!paset cooldown <seconds>` | Minimum time between `/pa` runs in this server; `0` disables it (default) |
| `!paset allowrole <role>` / `denyrole <role>` | Add/remove a role from the `/pa` allow-list |
| `!paset allowuser <user>` / `denyuser <user>` | Add/remove a specific member from the allow-list |
| `!paset showsettings` | Show current settings |

## Access control

Server admins (and the owner) can always run `/pa`. Beyond that, **the
default is open to everyone** — deliberately. For the slash command, the
intended primary gate is Discord's own per-command permission UI (**Server
Settings → Integrations**), which needs no bot code at all. The
`allowrole`/`allowuser` lists are an optional extra restriction on top of
that, mainly useful for the text-prefix form (`!pa`), since text commands
have no equivalent native Discord permission screen.

## Parallel broadcasting (optional)

A single bot token can only hold **one voice state per guild at a time** —
that's a Discord platform limit, not a library one — so one bot can only
ever be in one channel at once. To get real parallelism, you need additional
bot tokens (separate Discord Applications), each acting as its own
"helper" identity with its own voice-state slot.

1. Create 1–4 extra bot Applications at
   https://discord.com/developers/applications, and copy each bot's token.
2. Invite each helper bot to your server with the same voice permissions as
   your main bot (View Channel, Connect, Speak, Use Soundboard).
3. Register the tokens with Red's built-in secure token store (do **not**
   reuse your main bot's own token here):
   ```
   !set api pa token1 <helper1_token> token2 <helper2_token> ...
   ```
4. Run `!pahelpers` to verify each one logs in and is present in the server.
5. Run `!pa <sound>` as normal — it'll report "using N bots in parallel" and
   tag each line of the final report with which bot handled it.

If no tokens are configured, the cog behaves exactly as a solo broadcaster.
Helper connections are only opened for the duration of a single broadcast
and closed immediately afterward — they aren't kept alive between runs.

## How it works (for maintainers)

- **No VoiceClient.** Discord's soundboard API requires the acting bot's
  *voice state* to already be in the target channel (`403: User must be in
  voice channel to send voice channel effect`) — but that's just the
  gateway-level fact of "which channel is this member in," separate from
  actually negotiating an encrypted RTP audio session. Since the cog never
  sends or receives audio itself, it uses the low-level
  `Guild.change_voice_state(channel=..., self_mute=False, self_deaf=False)`
  call instead of `VoiceChannel.connect()`. This sends only the gateway
  voice-state packet — no PyNaCl, opus, or ffmpeg involved. (Being
  muted/deafened blocks soundboard effects outright, hence `self_mute=False,
  self_deaf=False` even though no audio is actually transmitted.)
- **Live rescanning.** The populated-channel list is recomputed on every
  claim rather than snapshotted once, so newly-populated channels are picked
  up mid-run. A run-scoped, lock-guarded set tracks what's already been
  visited this run and is discarded when the run ends.
- **Cross-identity sound triggering.** `send_sound()` reads `sound.guild.id`
  internally for non-default sounds. A sound object fetched by one bot
  identity isn't safely reusable by another, so each identity is handed a
  minimal `_SoundRef` stand-in (same `.id`, `.guild` set to that identity's
  own guild) instead of the original object.

## Known limitations

- Only `discord.VoiceChannel` is covered — Stage Channels are not currently
  swept.
- Helper bots must be invited to every guild you want them to help in, with
  matching permissions, separately from the main bot.
- The default soundboard clip length (~5.2s) drives the default per-channel
  delay; a much longer custom sound could get cut short if you shorten the
  delay too aggressively.
- The cooldown timer is in-memory only and resets on a bot restart -- it's a
  spam guard, not an audit record.
