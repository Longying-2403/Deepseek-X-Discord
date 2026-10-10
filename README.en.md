# Deepseek-X-Discord

[中文](README.md) | **English**

[![tests](https://github.com/Longying-2403/Deepseek-X-Discord/actions/workflows/tests.yml/badge.svg)](https://github.com/Longying-2403/Deepseek-X-Discord/actions/workflows/tests.yml)

A Discord bot powered by DeepSeek V4.1 Flash (`deepseek-flash`) that hangs out in your server like a regular member:

- **Answers when called**: replies right away when someone @mentions it, replies to one of its messages, or uses one of its aliases.
- **Chimes in on its own** (enabled per channel): waits until the chat goes quiet for a moment (or until it has waited too long), then lets the model decide whether to say something. It leans toward staying silent.
- **Feels human**: short, casual replies; shows "typing…" and waits a few seconds based on length before sending; may split a reply into two or three messages, or just react with an emoji.
- **Controllable chattiness**: proactive messages draw from a quota that refills with the channel's message volume, so the busier the chat, the faster it refills. Presets and individual settings can all be changed inside Discord.
- **Cheap to run**: conversation history is append-only to get the most out of DeepSeek's prefix cache; no model call at all when the quota is empty; automatic fallback when the daily budget runs out.
- **Sees images**: screenshots and memes posted in the chat are sent to the model too (can be turned off).

> **Language note:** the slash commands, settings and log messages are in Chinese; this README explains what each one means. The bot itself replies in whatever language your group is chatting in, and you can write its persona in English.

---

## Setup

### 1. Create a Discord bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) → New Application.
2. On the **Bot** page:
   - Click Reset Token and copy the token (it goes into `DISCORD_TOKEN` in `.env`).
   - Scroll down to **Privileged Gateway Intents** and turn on **Message Content Intent**. This is required; without it the bot can't see what people say.
3. Invite it to your server: when the bot starts, it prints an invite link with the right permissions in its log. Alternatively, go to **OAuth2 → URL Generator**, tick `bot` and `applications.commands`, and grant: View Channels, Send Messages, Send Messages in Threads, Read Message History, Add Reactions, Embed Links.

> No review is needed at small scale. Once the bot is in more than 100 servers, Discord requires app verification; once more than 10,000 users can see it, the Message Content privileged intent also needs approval. Discord will notify you when that happens.

### 2. Get a DeepSeek API key

Create an API key at [platform.deepseek.com](https://platform.deepseek.com) and top up a small balance. Usage is deducted from your prepaid balance, so you can't run up a bill.

### 3. Fill in `.env`

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | The bot token you copied above |
| `DEEPSEEK_API_KEY` | Your DeepSeek API key |
| `OWNER_ID` | Your own Discord user ID (turn on Developer Mode, then right-click your avatar → Copy User ID) |
| `CONTROL_GUILD_ID` | Optional. The ID of your private server; the owner command `/dsowner` is registered only there |
| `DISCORD_PROXY` | Optional. Set this if you need a proxy to reach Discord, e.g. `http://127.0.0.1:7890` |

---

## Deployment

### Option A: Docker Compose (recommended)

```bash
docker compose up -d --build
docker compose logs -f          # look for the "已登录" (logged in) line
```

The database lives in `./data/`. After editing `config.toml`, run `docker compose restart`.

### Option B: Plain Python (3.11 or later)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m dsbot --check         # offline self-check: config and slash command definitions (no Discord connection, no cost)
python -m dsbot
```

---

## Getting started

In your server (requires the Manage Server permission):

1. In a channel where you want it to join conversations on its own: `/ds 主动 开关:开` (proactive → on)
2. Pick how chatty it is: `/ds 档位` (preset: 安静 quiet / 普通 normal / 话痨 chatty)
3. Give it a persona: `/ds 人设` (opens an editor)
4. Check on it any time: `/ds 状态` (status: quota, its last decision and reasoning, today's spending, cache hit rate)

In channels where proactive mode is off, it only speaks when called.

> Consider telling your members in the channel description that messages in this channel are sent to DeepSeek (a service hosted in China) for processing.

---

## Commands

Discord shows command and option names in Chinese; the English in parentheses is a translation. Commands that take a `范围` (scope) option let you pick 本频道 (this channel) or 全服务器 (whole server).

### `/ds` (server admins)

| Command | What it does |
|---|---|
| `/ds 主动` (proactive) | Turn proactive mode on or off. Applies to this channel by default, or can be set as the server-wide default |
| `/ds 档位` (preset) | Apply a preset in one go: 安静 quiet / 普通 normal / 话痨 chatty |
| `/ds 设置` (settings) | Change a single setting; enter `默认` or `default` as the value to reset it |
| `/ds 人设` (persona) | Edit the persona (whole server or this channel only) |
| `/ds 状态` (status) | Show this channel's settings, quota, last decision and today's spending |
| `/ds 清空记忆` (clear memory) | Clear this channel's conversation history and summary |

Settings priority: **this channel > the thread's parent channel > this server > the owner's global value > config.toml**.

### `/dsowner` (you only; registered only in the `CONTROL_GUILD_ID` server)

| Command | What it does |
|---|---|
| `/dsowner 设置` (settings) | Change any setting, including budgets and thinking levels. Fill in `服务器id` (server ID) to change one server; leave it empty for the global default |
| `/dsowner 总览` (overview) | Today's spending, call count and cache hit rate for each server |

Without `CONTROL_GUILD_ID`, the owner commands aren't registered; edit `config.toml` directly instead.

---

## Settings

Admins can change these with `/ds 设置`:

| Name in Discord | `config.toml` key | Default | Description |
|---|---|---|---|
| 额度恢复轮数 | `refill_turns` | 15 | Restores one proactive message for every this many turns by other people. Over time, the bot's share of messages stays below about 1/15 |
| 额度上限 | `quota_capacity` | 3 | The most quota it can save up, i.e. how many times in a row it can chime in during a lively chat |
| 最短发言间隔秒 | `min_speak_gap` | 120 | Minimum seconds between two of its messages (replies to mentions count too) |
| 冷场秒数 | `quiet_seconds` | 30 | Seconds with nobody talking or typing before the chat counts as quiet |
| 最长等待秒 | `max_wait_seconds` | 180 | If the chat never goes quiet, the longest it waits before deciding anyway |
| 每人每分钟点名 | `mention_rate_limit` | 3 | How many times per minute each person can call it, to stop mention spam |
| 别名 | `aliases` | empty | Names that also count as calling it, comma-separated, e.g. `DS,小D` |

Only you can change these (`config.toml` or `/dsowner 设置`):

| Name in Discord | `config.toml` key | Default | Description |
|---|---|---|---|
| 最短判断间隔秒 | `min_eval_interval` | 60 | Minimum seconds between two proactive decisions |
| 主动判断思考 | `proactive_thinking` | low | Thinking level for proactive decisions: off / low / high |
| 点名回复思考 | `mention_thinking` | off | Thinking level when it's called; off is fastest |
| 每日预算美元 | `daily_budget_usd` | 1.0 | Daily spending cap per server, in USD. Once exceeded it only answers mentions; at twice the cap it stops answering those too |
| 全局每日预算美元 | `global_daily_budget_usd` | 3.0 | Daily spending cap across all servers, in USD |
| 上下文预算 | `context_budget_tokens` | 30000 | Once the context exceeds this many tokens, the oldest half is compressed into a summary |
| 看图 | `vision` | on | Whether images are sent to the model |

On/off settings accept `on` / `off` as well as `开` / `关`.

What counts as a "turn": several messages one person sends within 20 seconds count as one turn, so typing in short bursts doesn't refill the quota faster.

---

## How it works

```
message arrives ──► stored in that channel's buffer
                     │
                     ├─ is it calling the bot? ──► run a round now (uses no quota; only the per-person rate limit applies)
                     │
                     └─ check every 5 s: proactive on + new messages + (quiet OR waited too long)
                                         + quota left + gap long enough + within budget ──► run a round

one round = number the new messages (#12, #13…) and render them as one user message
          → send it to DeepSeek together with the full history (JSON output)
          → the model returns {"actions": [...]}: reply / react / do nothing
          → append this round's user + assistant messages to the history unchanged (silence is recorded too)
          → show "typing…", wait a few seconds based on length, then send
```

**Caching**: the history is only ever appended to, never rewritten, and the system prompt is fixed (anything that changes, like the time, goes into each round's message). So every request starts with the previous request, and most input tokens are billed at the cache-hit price, which is about 50 times cheaper. When the context gets too long, the oldest half is compressed into a summary in one go, so the cache misses only that once. The log prints the input token count and cache hits for every round.

**Cost estimate** (peak-hour prices, 30k-token context; with a healthy cache only the few hundred tokens of new messages miss): about 0.04–0.06 US cents per decision (a bit more with thinking on, because of reasoning tokens). A channel chatting all day, with a decision every 3 minutes, costs about 5–10 cents a day. Off-peak hours (including weekends) are half price. If prices change, update `[pricing]` in `config.toml`.

---

## FAQ

**It doesn't respond when @mentioned**
- Message Content Intent isn't turned on in the Developer Portal (the log shows an error such as 4014).
- The bot doesn't have permission to send messages in that channel.
- You may have mentioned its role rather than the bot itself. That works too, unless the server hides its role.

**Proactive mode never says anything**
- Check `/ds 状态`: Is proactive mode on? Is the quota at 0? What was its reasoning on the last decision?
- The model leans toward silence by default. To make it more active, try `/ds 档位 话痨` (chatty), or write "you love to chat" into its persona.

**Changed the persona, but it still talks the same way**: its earlier messages in the history are in the old style. Run `/ds 清空记忆` (clear memory) to start fresh.

**DeepSeek balance runs out**: the bot pauses API calls for 10 minutes and logs a warning, and resumes on its own after you top up. If someone calls it while it's paused, it adds a ⚠️ reaction to their message.

**DeepSeek rejects a batch of messages** (e.g. content moderation): if the batch had images, it retries once without them. If that still fails, it drops the batch and logs it, so the channel doesn't get stuck.

**It can't post in a channel** (missing permissions): after 3 failed rounds in a row, proactive mode in that channel pauses for 30 minutes, and `/ds 状态` shows a notice.

**Direct messages**: it only works in servers for now and ignores DMs.

---

## Development

Contributions are welcome! Before opening a PR, please read [CONTRIBUTING.md](CONTRIBUTING.md) (written in Chinese; issues and PRs in English are fine). It covers local setup and a few design rules. Every push and PR runs the tests and the offline self-check automatically on GitHub Actions.

```bash
python -m unittest discover -s tests      # core logic tests; no need to install discord.py
```

| File | Purpose |
|---|---|
| `dsbot/engine.py` | Scheduler: buffering, timing, quota, budget, carrying out actions, summary compression |
| `dsbot/render.py` / `prompts.py` | Chat log rendering format, system prompt |
| `dsbot/llm.py` | DeepSeek HTTP client (thinking toggle, JSON mode, usage parsing) |
| `dsbot/discord_app.py` / `convert.py` | Discord events and message conversion, sending, reactions, "typing…" |
| `dsbot/commands.py` | Slash commands and the persona editor |
| `dsbot/config.py` / `settings.py` | Config, setting definitions and layered overrides |
| `dsbot/store.py` / `ledger.py` | SQLite storage, daily spending |

Licensed under the [MIT License](LICENSE).
