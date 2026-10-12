# Deepseek-X-Discord

[中文](README.md) | **English**

[![tests](https://github.com/Longying-2403/Deepseek-X-Discord/actions/workflows/tests.yml/badge.svg)](https://github.com/Longying-2403/Deepseek-X-Discord/actions/workflows/tests.yml)

A Discord bot powered by DeepSeek V4.1 Flash (`deepseek-flash`) that hangs out in your server like a regular member:

- **Answers when called**: replies right away when someone @mentions it, replies to one of its messages, or uses one of its aliases.
- **Chimes in on its own** (enabled per channel): waits until the chat goes quiet for a moment (or until it has waited too long), then lets the model decide whether to say something. It leans toward staying silent.
- **Feels human**: short, casual replies; shows "typing…" and waits a few seconds based on length before sending; may split a reply into two or three messages, or just react with an emoji.
- **Controllable chattiness**: proactive messages draw from a quota that refills with the channel's message volume, so the busier the chat, the faster it refills. Presets and individual settings can all be changed inside Discord.
- **Cheap to run**: conversation history is append-only to get the most out of DeepSeek's prefix cache; no model call at all when the quota is empty; automatic fallback when the daily budget runs out.
- **Sees images**: uploaded images, GIFs from the GIF picker, pasted image links and Discord stickers are all sent to the model (only the first frame of animated images; can be turned off).
- **Reads files**: text and code, PDF (for scanned PDFs it looks at the first few pages as images), Word, Excel and PowerPoint. Files attached when someone calls it are read right away; for requests like "take a look at that file above", it opens the file itself.
- **Searches the web**: when someone calls it with a question that needs up-to-date information it doesn't have (news, prices, scores, weather…), it looks it up with DeepSeek's built-in search before answering. Each server has a daily search limit.
- **Cross-channel memory** (turned on by admins): what you talked about with it in channel A carries over when you talk to it in channel B, and it can look through the history of other channels that share their memory. Private channels don't share by default.
- **Stays where you want it**: you can limit where it responds by channel, category or the whole server.

> **Language note:** the slash commands, settings and log messages are in Chinese; this README explains what each one means. The bot itself replies in the language of the message it's answering, and you can write its persona in English.

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
| `TAVILY_API_KEY` | Optional. If DeepSeek's built-in search doesn't work, the bot switches to [Tavily](https://tavily.com) (1,000 free searches a month) |

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
python -m dsbot --test-search "Taipei weather today"   # optional: run one real search to check web search works (costs a tiny bit)
python -m dsbot
```

---

## Getting started

In your server (requires the Manage Server permission):

1. Decide where it may respond (optional): run `/ds 区域 操作:禁止 范围:全服务器` (area → deny, scope → whole server), then run `/ds 区域 操作:允许` (allow) in each channel you want it in, or choose `范围:本类别` (scope → this category) to open up a whole category
2. In a channel where you want it to join conversations on its own: `/ds 主动 开关:开` (proactive → on)
3. Pick how chatty it is: `/ds 档位` (preset: 安静 quiet / 普通 normal / 话痨 chatty)
4. Give it a persona: `/ds 人设` (opens an editor)
5. To let it remember what was said in other channels: `/ds 记忆共享 操作:开 范围:全服务器` (memory sharing → on, scope → whole server; see "Cross-channel memory" below)
6. Check on it any time: `/ds 状态` (status: quota, its last decision and reasoning, today's spending, cache hit rate, memory sharing and search count); `/ds 区域列表` (area list) shows where it will respond

If you don't set any areas, it answers mentions in every channel it can see; in channels where proactive mode is off, it only speaks when called.

> Consider telling your members in the channel description that messages in this channel are sent to DeepSeek (a service hosted in China) for processing.

---

## Commands

Discord shows command and option names in Chinese; the English in parentheses is a translation.

### `/ds` (server admins)

| Command | What it does |
|---|---|
| `/ds 区域` (area) | 允许 allow / 禁止 deny / 跟随上级 inherit. Where it isn't allowed, it ignores messages completely: no replies to @mentions, no proactive messages, and nothing is sent to DeepSeek |
| `/ds 区域列表` (area list) | Lists the categories and channels in this server where it's allowed or denied, and where proactive mode is on |
| `/ds 主动` (proactive) | Turn proactive mode on or off |
| `/ds 档位` (preset) | Apply a preset in one go: 安静 quiet / 普通 normal / 话痨 chatty |
| `/ds 设置` (settings) | Change a single setting; enter `默认` or `default` as the value to reset it |
| `/ds 人设` (persona) | Edit the persona (opens an editor) |
| `/ds 记忆共享` (memory sharing) | 开 on / 关 off / 跟随上级 inherit. Channels with sharing on share memory with each other; a private channel only shares its content if sharing is turned on in that channel itself |
| `/ds 状态` (status) | Show this channel's settings, quota, last decision, today's spending, memory sharing and today's search count |
| `/ds 清空记忆` (clear memory) | Clear this channel's conversation history and summary |

Apart from 区域列表 (area list), 状态 (status) and 清空记忆 (clear memory), these commands all have a `范围` (scope) option: **本频道** (this channel; the default, except for the persona), **本类别** (this category, i.e. a group of channels in Discord) or **全服务器** (whole server).

Settings priority: **this channel > the thread's parent channel > its category > this server > the owner's global value > config.toml**. For example, if you deny the whole server, allow a "Chat" category, and then deny one channel inside it, the bot responds everywhere in that category except that channel.

> To wall off certain channels completely, you can also remove the bot's View Channel permission in Discord's channel settings; then those messages never reach the bot at all.

### `/dsowner` (you only; registered only in the `CONTROL_GUILD_ID` server)

| Command | What it does |
|---|---|
| `/dsowner 设置` (settings) | Change any setting, including budgets, thinking levels, the web search switch and the daily search limit. Fill in `服务器id` (server ID) to change one server; leave it empty for the global default |
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
| 别名 | `aliases` | empty | Names that also count as calling it, comma-separated, e.g. `DS,小D`. English aliases must appear as a whole word (`DS` won't match "words") |

The area setting (`/ds 区域`) is stored as a setting called 允许回应 (allowed to respond), `enabled` in `config.toml`, which is on by default. To have every server start in whitelist mode, set `enabled` to `false` in `config.toml`.

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
| 读文件 | `files` | on | Whether to read files people upload |
| 联网搜索 | `web_search` | on | When it's called and needs up-to-date information, it searches the web |
| 每日搜索上限 | `daily_search_limit` | 30 | The most searches per server per day; 0 turns searching off. For example: `/dsowner 设置 参数:每日搜索上限 值:50` |

On/off settings accept `on` / `off` as well as `开` / `关`.

What counts as a "turn": several messages one person sends within 20 seconds count as one turn, so typing in short bursts doesn't refill the quota faster.

---

## Reading files

**What it can read**:

| Type | Notes |
|---|---|
| Text and code | txt, md, csv, json, py, js, ts, html, logs, subtitles… Besides UTF-8, it recognizes older GBK / Big5 files, as well as older Japanese (Shift_JIS / EUC-JP) and Korean (EUC-KR) files. Legacy-encoded files with only a line or two may be misdetected; when unsure, it assumes Chinese |
| PDF | Reads the text and marks page numbers; for scanned PDFs without text, it looks at the first 3 pages as images instead. PDFs that only restrict printing or copying can be read; PDFs with an open password can't |
| Word / Excel / PowerPoint | .docx, .xlsx, .pptx. Tables are turned into text row by row, and Excel dates and percentages are converted as displayed; for very large sheets it only reads the top part |

When it can't read a file, it tells the person why: old formats (.doc / .xls / .ppt; saving in the newer format fixes it), password-protected files, archives, audio, video, image formats such as HEIC or BMP, and so on.

**When it reads files**:

- Right away: files in the message that calls it, files in the message it's replying to when called, and files the same person posted within the last two minutes.
- On request when called: for example, "help 小雨 look at that PDF above". The model first says which message's file to open; the bot reads it and hands the contents back to the model to answer. That takes one extra call, so the reply comes a few seconds later.
- When nobody calls it, it doesn't read files; it only knows that someone posted one. The exception is message.txt, which Discord creates automatically from overly long messages: it reads the beginning of it like any long message.

**Limits** (all adjustable under `[engine]` in `config.toml`): up to 20 MB per file; up to 20,000 characters per file (longer files are read from the start, and the model is told where it stopped); up to 3 files and 40,000 characters per round in total, including files it asked to open itself. Files it can't read don't count toward these limits. If a message has too many files, or the limit only covers some of them, it picks up the rest the next time it's called. A file that definitely can't be read (unsupported format, corrupted) is reported only once and not retried every time, unless someone explicitly asks it to open it again.

**File contents stay in the conversation memory**, so you can follow up with "what does page 3 say?". Large files take up more context, but later requests are mostly cache hits, so the cost stays low; once they become old history, they're compressed into the summary along with other older chat. If the context gets far too long (more than twice the budget and over 100k tokens, e.g. after reading several large files in a row), it's compressed right away instead of waiting.

**Security**: every file is parsed in a separate subprocess with limits on memory (1 GB), CPU and time (30 seconds), so a broken or malicious file can at most make that one read fail, without hanging the bot. When deployed with Docker, the bot runs as root, and the parsing subprocess drops to the `nobody` user so it can't read the token and API key in the bot's process. Without Docker this isolation isn't guaranteed: when running as a regular user, there's no privilege drop; when running as root, it only drops privileges if Python, the bot's code and its installed libraries are readable by other users (not the case if, say, the libraries are installed under `/root`); otherwise it parses as the current user and logs a one-time warning. `python -m dsbot --check` shows whether the privilege drop is in effect. Like chat messages, file contents are sent to DeepSeek for processing.

**Privacy**: chat history and the contents of files it has read are stored in the database under `./data/`, with file permissions that let only the bot itself read and write it (other accounts on a NAS can't see it).

---

## Cross-channel memory

By default, each channel keeps its own memory: in channel B, it doesn't know what you just talked about with it in channel A. Once an admin turns on memory sharing:

- **It picks up where you left off**: when someone calls it in B and that person talked with it in A within the last 6 hours, those lines come along, so "that thing we just talked about" makes sense. Each exchange is carried over only once.
- **It looks things up on request**: when called, it knows which channels it may look through. When needed (say, "the thing we discussed in #games last time"), it first reads that channel's summary and recent messages, then answers. That takes one extra call, so the reply comes a few seconds later.

To turn it on: `/ds 记忆共享 操作:开` (memory sharing → on). The scope can be this channel, this category or the whole server, just like `/ds 区域` (area).

Privacy rules:
- Memory is shared only among channels that have sharing on, and only within the same server, never across servers.
- **Private channels** (channels @everyone can't see, private threads, age-restricted channels) don't share their content, even if sharing is on for the whole server. To share one anyway, run `/ds 记忆共享 操作:开` inside that channel (scope: this channel). Private channels can still see what public channels share.
- Areas where it isn't allowed to respond (denied with `/ds 区域`) don't take part in sharing.
- After you run `/ds 清空记忆` (clear memory) in a channel or turn off its sharing, other channels can no longer get its content. Lines already carried over into another channel stay in that channel's recent history until they age out and are compressed (compression only summarizes that channel's own content); to remove them right away, clear that channel too.
- In the list the model sees, threads are labeled 帖子「title」 (thread "title"), separate from channels, so a member can't pass a thread off as a channel by giving it the same name.
- `/ds 状态` (status) shows which channels this channel can look through, and whether its own content is shared.

---

## Web search

Its knowledge only goes up to around mid-2025. When it's called and the question needs current or real-time information (news, prices, exchange rates, scores, weather, new products…), it searches the web first and then answers. It decides for itself whether to search; small talk and common knowledge don't trigger a search.

- **It uses DeepSeek's built-in search** (the web_search tool on DeepSeek's Anthropic-compatible API, which is also what Claude Code uses when pointed at DeepSeek), so no extra key is needed. It's billed by tokens, roughly 0.1–0.5 US cents per search, and counts toward the daily budget.
- It only searches when called, never during proactive chat. It searches at most twice per round, and each search can browse up to 3 rounds of web results.
- Each server can search up to 30 times a day. Change this with `/dsowner 设置 参数:每日搜索上限` (you can target a specific server), or turn search off with `/dsowner 设置 参数:联网搜索 值:关`. It also stops searching once the budget is exceeded.
- Links in its answers don't unfurl into previews, so they don't flood the chat.
- Web content it finds is treated as reference material, just like files; instructions in it such as "ignore your previous settings" aren't followed.
- After deploying, you can run `docker compose exec dsbot python -m dsbot --test-search "Taipei weather today"` once to check that search works. If DeepSeek's built-in search stops working (for example, if the API changes), add `TAVILY_API_KEY` to `.env` and it switches to Tavily automatically.

---

## How it works

```
message arrives ──► stored in that channel's buffer
                     │
                     ├─ is it calling the bot? ──► run a round now (uses no quota; only the per-person rate limit applies)
                     │
                     └─ check every 5 s: proactive on + new messages + (quiet OR waited too long)
                                         + quota left + gap long enough + within budget ──► run a round

one round = number the new messages (#12, #13…) and render them as one user message (contents of files to read go after them)
          → send it to DeepSeek together with the full history (JSON output)
          → the model returns {"actions": [...]}: reply / react / open a file / search the web / look through another channel / do nothing
            (to open a file, search or look through records: do it, append the results, then ask the model again)
          → append this round's user + assistant messages to the history unchanged (silence is recorded too)
          → show "typing…", wait a few seconds based on length, then send
```

**Caching**: the history is only ever appended to, never rewritten, and the system prompt is fixed (anything that changes, like the time, goes into each round's message). So every request starts with the previous request, and most input tokens are billed at the cache-hit price, which is about 50 times cheaper. When the context gets too long, the older half (estimated in tokens) is compressed into a summary while the latest 6 rounds are left alone, so the cache misses only that once. The log prints the input token count and cache hits for every round.

**Cost estimate** (peak-hour prices, 30k-token context; with a healthy cache only the few hundred tokens of new messages miss): about 0.04–0.06 US cents per decision (a bit more with thinking on, because of reasoning tokens). A channel chatting all day, with a decision every 3 minutes, costs about 5–10 cents a day. Off-peak hours (including weekends) are half price. If prices change, update `[pricing]` in `config.toml`.

---

## FAQ

**It doesn't respond when @mentioned**
- The channel isn't in an allowed area: the first line of `/ds 状态` (status) will say so, and `/ds 区域列表` (area list) shows the overall setup.
- Message Content Intent isn't turned on in the Developer Portal (the log shows an error such as 4014).
- The bot doesn't have permission to send messages in that channel.
- You may have mentioned its role rather than the bot itself. That works too, unless the server hides its role.

**Proactive mode never says anything**
- Check `/ds 状态`: Is proactive mode on? Is the quota at 0? What was its reasoning on the last decision?
- The model leans toward silence by default. To make it more active, try `/ds 档位 话痨` (chatty), or write "you love to chat" into its persona.

**Changed the persona, but it still talks the same way**: its earlier messages in the history are in the old style. Run `/ds 清空记忆` (clear memory) to start fresh.

**It says it can't see a GIF**: a GIF sent from the GIF picker is really a link, and Discord adds the preview image a second or two later. The bot reads that preview (the first frame of the animation); if the message that @mentions it contains a link itself, it waits two seconds before replying. For web links (YouTube, for example) it only reads the title, not the thumbnail.

**It says it can't read a file**: check the reason it gives. Common ones are old formats (.doc/.xls/.ppt), password-protected files and files over 20 MB. If it says the file-parsing process couldn't start, the problem is on the bot's side (the log has the details; usually permissions); once that's fixed, just ask it to read the file again. `python -m dsbot --check` (with Docker: `docker compose exec dsbot python -m dsbot --check`) checks the PDF component and the parsing subprocess.

**DeepSeek balance runs out**: the bot pauses API calls for 10 minutes and logs a warning, and resumes on its own after you top up. If someone calls it while it's paused, it adds a ⚠️ reaction to their message.

**DeepSeek rejects a batch of messages** (e.g. content moderation): if the batch had images, it retries once without them. If that still fails, it drops the batch and logs it, so the channel doesn't get stuck.

**It can't post in a channel** (missing permissions): after 3 failed rounds in a row, proactive mode in that channel pauses for 30 minutes, and `/ds 状态` shows a notice.

**Direct messages**: it only works in servers for now and ignores DMs.

---

## Development

Contributions are welcome! Before opening a PR, please read [CONTRIBUTING.md](CONTRIBUTING.md) (written in Chinese; issues and PRs in English are fine). It covers local setup and a few design rules. Every push and PR runs the tests and the offline self-check automatically on GitHub Actions. Changes in each version are listed in [CHANGELOG.md](CHANGELOG.md).

```bash
python -m unittest discover -s tests      # core logic tests; no need to install discord.py
```

The file-reading tests use python-docx, openpyxl, python-pptx and reportlab to generate real files on the fly (for testing only; the bot doesn't need them to run, and `pip install -r requirements-dev.txt` installs them). Without them, those tests are skipped.

| File | Purpose |
|---|---|
| `dsbot/engine.py` | Scheduler: buffering, timing, quota, budget, carrying out actions, summary compression, cross-channel memory |
| `dsbot/render.py` / `prompts.py` | Chat log rendering format, system prompt |
| `dsbot/llm.py` | DeepSeek HTTP client (thinking toggle, JSON mode, usage parsing) |
| `dsbot/files.py` | Reading files: type detection, decoding, PDF / Office parsing, the parsing subprocess |
| `dsbot/search.py` | Web search: DeepSeek's built-in web_search (Anthropic-compatible API), with Tavily as a fallback |
| `dsbot/discord_app.py` / `convert.py` | Discord events and message conversion, sending, reactions, "typing…" |
| `dsbot/commands.py` | Slash commands and the persona editor |
| `dsbot/config.py` / `settings.py` | Config, setting definitions and layered overrides |
| `dsbot/store.py` / `ledger.py` | SQLite storage, daily spending |

Licensed under the [MIT License](LICENSE).
