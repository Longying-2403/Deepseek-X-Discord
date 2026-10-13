# DeepSeek Buddy (Discord bot)

[中文](https://github.com/Longying-2403/Deepseek-X-Discord/tree/main) | **English**

[![tests](https://github.com/Longying-2403/Deepseek-X-Discord/actions/workflows/tests.yml/badge.svg?branch=english)](https://github.com/Longying-2403/Deepseek-X-Discord/actions/workflows/tests.yml)

A Discord bot powered by DeepSeek V4.1 Flash (`deepseek-flash`) that hangs out in your server like any other member:

- **Answers when called**: when someone @mentions it, replies to one of its messages, or uses one of its aliases, it responds right away.
- **Joins in on its own** (per channel): it waits for a short lull in the chat (or until it has waited long enough), then lets the model decide whether to say anything; by default it leans toward staying quiet.
- **Feels human**: casual, with the length fitting the moment (a sentence or two for a quick comeback, a few more when it's playing along with a story). It shows "typing…" first and waits a few seconds based on the length before sending; it can split a thought into two or three messages, or just react with an emoji. When people are deep in a conversation it may tactfully chime in, and if it's told to stay out of it, it apologizes and keeps out of that topic.
- **Uses the server's emoji and stickers**: it knows which custom emoji and stickers this server has, and can use them in replies, as reactions, or send a sticker on its own.
- **Plays along**: when people pull it into a roleplay, it plays the part they set up and follows the story, with actions in italic parentheses; once the scene is over, it goes back to its usual self.
- **dsowner can change its persona in chat**: when `OWNER_ID` calls it and says something like "be snarkier from now on", it rewrites its persona and saves it to the settings, and "change it back" undoes that; if anyone else asks, it tells them to ask dsowner.
- **You control how much it talks**: its quota for speaking up on its own refills with the chat's message volume, so the livelier the chat, the faster it refills. Presets and every parameter can be adjusted from inside Discord.
- **Saves money**: the conversation history is append-only and never rewritten, to get the most out of DeepSeek's prefix cache; with no quota left it doesn't even run a check; over the daily budget it scales back automatically.
- **Sees images**: uploaded images, GIFs from the GIF picker, pasted image links, link-preview images and Discord stickers are sent to the model too (only the first frame of animated images; can be turned off).
- **Reads files**: text and code, PDF (for scans it looks at images of the first few pages), Word, Excel, PowerPoint. Files attached to the message that calls it are read directly; for requests like "can you look at that file above", it opens the file itself.
- **Opens links**: normally it sees the link previews Discord generates; when it's called it really opens them: posts on X (Twitter) through FxTwitter (no account needed), other web pages through Tavily. Sites you have to log in to see only get the preview.
- **Sends memes and pictures**: it has a meme library (a shared one on your NAS, and each server can upload its own), and it can also look for pictures online. It looks at what it finds first and only sends one if it fits.
- **Searches the web**: when it's called with a question about news, prices, scores, weather or anything else recent that it doesn't know, it looks it up with DeepSeek's built-in search first, then answers. Each server has a daily search cap.
- **Cross-channel memory** (turned on by admins): what you talked about with it in channel A carries over when you talk to it in channel B, and it can look through the history of other channels that share. Private channels don't share by default.
- **You control where it responds**: limit it by channel, by category, or for the whole server.

It replies in the language of the message it's answering, so it also works in mixed-language servers.

> This is the English edition, with English commands, replies and docs. The Chinese edition (中文版) has the same features and lives on the [`main` branch](https://github.com/Longying-2403/Deepseek-X-Discord/tree/main). Both are available as downloads on the [Releases](https://github.com/Longying-2403/Deepseek-X-Discord/releases) page.

---

## Setup

### 1. Create the Discord bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) → New Application.
2. On the **Bot** page in the left sidebar:
   - Click Reset Token and copy the token (it goes into `DISCORD_TOKEN` in `.env`).
   - Scroll down to **Privileged Gateway Intents** and turn on **Message Content Intent** (required; without it the bot can't see what messages say).
3. Invite it to your server: the first time the bot starts, it prints an invite link in the log with the right permissions already included. You can also use **OAuth2 → URL Generator** in the portal: check `bot` and `applications.commands`, and the permissions View Channels, Send Messages, Send Messages in Threads, Read Message History, Add Reactions, Embed Links and Attach Files (needed for sending memes and pictures; bots invited before 1.4.0 usually have it anyway, since @everyone has this permission by default).

> Small-scale use needs no review. Once the bot is in more than 100 servers you have to complete Discord's app verification first, and once more than 10,000 users can see it, the privileged Message Content intent also has to be approved; Discord will tell you when that applies.

### 2. Get a DeepSeek API key

Create an API key at [platform.deepseek.com](https://platform.deepseek.com) and top up some balance (usage is deducted from the balance as you go, so you can't run up a debt).

### 3. Fill in `.env`

```bash
cp .env.example .env
```

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | The bot token you copied above |
| `DEEPSEEK_API_KEY` | Your DeepSeek key |
| `OWNER_ID` | Your own Discord user ID (with Developer Mode on, right-click your avatar → Copy User ID) |
| `CONTROL_GUILD_ID` | Needed if you want to change the owner settings (budget, web search, etc.) from Discord: the ID of the server where `/dsowner` goes (right-click the server icon → Copy Server ID). Any server you chat in will do (a friend's works too). Like `/ds`, the command is only visible to people with the "Manage Server" permission, and only `OWNER_ID` can use it. With only `OWNER_ID` set, there's no `/dsowner` |
| `DISCORD_PROXY` | Optional. Set it if you need a proxy to reach Discord, e.g. `http://127.0.0.1:7890` |
| `TAVILY_API_KEY` | Optional, but recommended. Opening the web links people post and finding pictures online both rely on [Tavily](https://tavily.com); web search also switches to it when DeepSeek's built-in search doesn't work (1,000 free credits a month). Without it, the bot can still read posts on X, and the meme library works as usual |

---

## Deployment

### Option A: Docker Compose (recommended)

```bash
docker compose up -d --build
docker compose logs -f          # watch the logs and look for "Logged in as"
```

The database lives in `./data/`. After editing `config.toml`, `docker compose restart` is enough; after editing `.env`, use `docker compose up -d --force-recreate` (a restart doesn't re-read `.env`).

With Synology Container Manager: after changing `.env` or the code files, go to "Project", select it → Action → Stop, then Action → Build (this removes the old container and rebuilds it with the new code and `.env`; your data in `./data/` isn't affected). Just restarting it from the "Container" page doesn't read the new `.env`.

### Option B: plain Python (3.11 or later)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m dsbot --check         # offline self-check: config and slash command definitions (no Discord connection, costs nothing)
python -m dsbot --test-search "Taipei weather today"   # optional: run one real search to confirm web search works (costs a tiny bit)
python -m dsbot --test-link https://x.com/…/status/…   # optional: really open one link
python -m dsbot --test-image "capybara"                # optional: really search for pictures once (uses 1 Tavily credit); the candidates are saved to data/image-test/
python -m dsbot
```

---

## Getting started

In your server (you need the "Manage Server" permission):

1. Decide where it may hang out (optional): `/ds area action:Block scope:Whole server`, then run `/ds area action:Allow` in the channels you want, or pick `scope:This category` to open up a whole category
2. In channels where you want it to chat on its own: `/ds proactive state:On`
3. Pick how talkative it is: `/ds preset` (Quiet / Normal / Chatty)
4. Give it a persona: `/ds persona` (opens an editor); `OWNER_ID` can also just @mention it in chat and ask it to change its persona (see [Roleplay, emoji and changing the persona in chat](#roleplay-emoji-and-changing-the-persona-in-chat))
5. To have it remember what was said in other channels: `/ds share-memory action:On scope:Whole server` (see [Cross-channel memory](#cross-channel-memory) below)
6. Give it a few memes: `/ds meme action:Upload` (see [Memes and pictures](#memes-and-pictures) below)
7. Check on it any time: `/ds status` (quota, its last decision and why, today's spending, cache hit rate, memory sharing, and how many searches, link opens and image searches it has done today); `/ds area-list` shows where it responds

Without any area settings, it answers when called in every channel it can see; in channels where proactive chat is off, it only speaks when called.

> It's a good idea to note in the channel description that messages in the channel are sent to DeepSeek (a service hosted in China) for processing.

---

## Commands

### `/ds` (server admins)

| Command | What it does |
|---|---|
| `/ds area` | Allow / Block / Inherit. Where it isn't allowed, it ignores messages completely: no replies to @mentions, no proactive chat, and nothing is sent to DeepSeek |
| `/ds area-list` | Lists the allowed and blocked categories and channels on this server, plus where proactive chat is on |
| `/ds proactive` | Turns proactive chat on or off |
| `/ds preset` | One-click presets: Quiet / Normal / Chatty |
| `/ds set` | Adjusts a single setting; enter `default` as the value to reset it |
| `/ds persona` | Edits the persona (opens an editor). `OWNER_ID` can also have it changed in chat; see [Roleplay, emoji and changing the persona in chat](#roleplay-emoji-and-changing-the-persona-in-chat) |
| `/ds share-memory` | On / Off / Inherit. Channels with sharing on share their memory with each other; a private channel only shares out if it's turned on in that channel itself |
| `/ds meme` | This server's meme library: Upload (attach a picture and give it a name) / Remove / List |
| `/ds status` | Shows this channel's settings, quota, last decision, today's spending, memory sharing, and today's searches, link opens and image searches |
| `/ds clear-memory` | Clears this channel's chat history and summary |

Except for `clear-memory`, `status`, `area-list` and `meme`, these commands all have a `scope` option: **This channel** (the default, except for `persona`), **This category** (a category in Discord's channel list), or **Whole server**.

Setting priority: **this channel > the thread's parent channel > its category > this server > the owner's global value > config.toml**. For example, if the whole server is blocked, a "Chat" category is allowed, and one channel in it is blocked again, it responds everywhere in the Chat category except that one channel.

> To wall off certain channels completely, you can also remove the bot's "View Channel" permission there in Discord, so the messages never reach the bot at all.

### `/dsowner` (only you can use it; it only shows up in the `CONTROL_GUILD_ID` server, visible to people with the "Manage Server" permission)

| Command | What it does |
|---|---|
| `/dsowner set` | Changes any setting (including the budgets, thinking levels, web search, reading links, the daily search limit and the daily image search limit); give a server ID to change only that server, otherwise it's global |
| `/dsowner overview` | Today's spending, number of calls and cache hit rate for each server |

Without `CONTROL_GUILD_ID` there's no `/dsowner` (`OWNER_ID` alone isn't enough), and the owner settings can only be changed under `[defaults]` in `config.toml`.

---

## Settings

Admins can adjust these (`/ds set`):

| Setting | Default | Description |
|---|---|---|
| Quota refill turns | 15 | Each time other people take this many turns, 1 proactive message is added back to the quota. Over time, its share of the conversation stays under about 1/15 |
| Quota cap | 3 | The most it can save up, i.e. the most times in a row it can jump into a lively chat |
| Min seconds between messages | 120 | Minimum time between two of its messages (replies when it's called count too) |
| Lull seconds | 30 | How long with nobody talking or typing counts as a lull |
| Max wait seconds | 180 | If the chat never goes quiet, it checks anyway after at most this long |
| Mentions per person per minute | 3 | Keeps anyone from spamming it with @mentions |
| Aliases | empty | Names that also count as calling it when they appear in a message, comma-separated, e.g. `DS,Dee`. Aliases made of English letters/digits must appear as a whole word (`DS` doesn't match "words"); other aliases, such as Chinese nicknames, match anywhere |
| Send pictures | on | Lets it send pictures from the meme library and find pictures online on its own. Turned off, it does neither |

The area setting (`/ds area`) is the "Allowed to respond" setting, which is on by default. To have every server start out in allowlist mode, set `enabled` to `false` in `config.toml`.

Only you can adjust these (`config.toml` or `/dsowner set`):

| Setting | Default | Description |
|---|---|---|
| Min seconds between checks | 60 | Minimum time between two proactive checks |
| Thinking for proactive checks | low | Thinking level for proactive checks: off / low / high |
| Thinking for mention replies | off | Thinking level when it's called; off is fastest |
| Daily budget (USD) | 1.0 | Daily cap for a single server. Over it, it only answers mentions; over 2x, not even those |
| Global daily budget (USD) | 3.0 | Cap for all servers combined |
| Context budget (tokens) | 30000 | Once the context goes over this many tokens, the oldest half is compacted into a summary |
| Vision | on | Whether images are sent to the model |
| Read files | on | Whether files people upload are read |
| Web search | on | When it's called and needs current information, it searches the web |
| Daily search limit | 30 | Max searches per server per day; 0 turns searching off. E.g. `/dsowner set setting:Daily search limit value:50` |
| Read links | on | When it's called, it opens the links in the message (posts on X go through FxTwitter, other web pages need Tavily) |
| Daily image search limit | 10 | Max online image searches per server per day; 0 turns them off (the meme library still works) |

What "one turn" means: several messages in a row from the same person within 20 seconds count as one turn, so typing in short lines can't farm quota.

---

## Reading files

**What it can read**:

| Type | Notes |
|---|---|
| Text, code | txt, md, csv, json, py, js, ts, html, logs, subtitles…; besides UTF-8 it also recognizes old GBK / Big5 files, as well as old Japanese (Shift_JIS / EUC-JP) and Korean (EUC-KR) ones. Legacy-encoded files with only a line or two may be misdetected; when unsure, they're treated as Chinese |
| PDF | Reads the text, marked with page numbers; for scans without text it looks at images of the first 3 pages instead. PDFs that only restrict printing/copying can be read; ones that need a password to open can't |
| Word / Excel / PowerPoint | .docx, .xlsx, .pptx. Tables are turned into text row by row, Excel dates and percentages are converted the way they're displayed, and very large sheets are only read from the top |

For files it can't read, it tells the person why: old formats (.doc / .xls / .ppt; saving in the new format fixes it), password-protected files, archives, audio, video, image formats like HEIC/BMP, and so on.

**When it reads**:

- Read directly: files in the message that calls it, files in the message being replied to when calling it, and files the same person posted within the last two minutes.
- Read on demand when called: for something like "can you check that PDF above for Mia", the model first says which message's file to open, and the bot reads it and hands it back so the model can answer. That's one extra call, so the reply comes a few seconds later.
- When nobody calls it, it doesn't read files; it only knows that someone posted a file and what it's called. The exception is message.txt, which Discord makes out of overly long messages: it sees the beginning, just like with any long message.

**Limits** (all changeable under `[engine]` in `config.toml`): files up to 20 MB; at most 20,000 characters per file (longer files are read from the beginning, and the model is told how far it got); at most 3 files and 40,000 characters in total per round (including the ones it asks to open). Files it can't read don't use up a slot; if a message has too many files, or the remaining budget only covers part of them, the rest is read the next time it's called. Files known to be unreadable (unsupported format, broken file) are only reported once and aren't retried every time, unless it's explicitly asked to open them again.

**The contents stay in the conversation memory**, so you can follow up with "what does page three say". Big files take up more context, but the requests after that are mostly cache hits, so the cost stays low; once the file is old history, it gets compacted into the summary along with other old chat. If the context gets really long (over twice the budget and over 100,000 tokens, e.g. after reading several big files in a row), it compacts right away instead of waiting for things to get old.

**Security**: every file is parsed in a separate subprocess with limits on memory (1 GB), CPU and time (30 seconds), so a broken or malicious file can at most make that one read fail; it can't hang the bot. In Docker the bot runs as root and the parsing subprocess drops to the `nobody` user, so it can't read the token and API key in the bot process. Without Docker you don't necessarily get this layer of isolation: running as a regular user doesn't drop privileges, and running as root only drops them if Python, the bot's code and the installed libraries are readable by other users (not if, say, the libraries are installed under `/root`); otherwise it parses as the current user as usual and warns once in the log. `python -m dsbot --check` shows whether privileges are dropped. Like chat messages, file contents are sent to DeepSeek for processing.

**Privacy**: chat history and the contents of files it has read are stored in the database in `./data/`, with file permissions that only let the bot itself read and write it (other accounts on your NAS can't see it).

---

## Cross-channel memory

By default each channel keeps its own memory: in channel B it doesn't know what you just talked about with it in channel A. Once an admin turns on memory sharing:

- **It picks up where you left off**: when someone calls it in B and that person talked with it in A within the last 6 hours, those few lines come along, so "that thing I just mentioned" makes sense. Each piece is only brought along once.
- **It can look back when needed**: when called, it knows which channels it can look through; when it needs to (e.g. "that thing we talked about in #games last time"), it first reads that channel's summary and recent history, then answers. That's one extra call, a few seconds slower.

How to turn it on: `/ds share-memory action:On`; the scope can be this channel, this category or the whole server, just like `/ds area`.

Privacy rules:
- Only channels with sharing on share with each other, and only within the same server, never across servers.
- **Private channels** (channels @everyone can't see, private threads, age-restricted channels) don't share their content out even when sharing is turned on for the whole server. If you really want one to share, run `/ds share-memory action:On` in that channel itself (with the scope This channel). Private channels can still see what public channels share.
- Areas where it isn't allowed to respond (blocked with `/ds area`) don't take part in sharing.
- After you run `/ds clear-memory` in a channel or turn its sharing off, other channels can't get its content anymore. Lines that were already brought into another channel stay in that channel's recent history until they become old history and get compacted away (compaction only summarizes a channel's own content); to remove them right away, clear that channel too.
- Threads are listed as `thread "title"`, separate from channels, so a member can't pass a thread off as a channel by giving it a channel's name.
- `/ds status` shows which channels this channel can look through and whether its own content is shared out.

## Web search

Its knowledge only goes up to about mid-2025. When it's called and the question needs the latest or real-time information (news, prices, exchange rates, scores, weather, new products…), it searches the web first, then answers. It decides for itself whether to search; small talk and common knowledge don't trigger a search.

- **It uses DeepSeek's built-in search** (web_search in DeepSeek's Anthropic-compatible API, the one Claude Code also uses), so no other key is needed. It's billed by tokens, about 0.1–0.5 US cents per search, and counts toward the daily budget.
- It only searches when called, not when chatting on its own. At most 2 searches per round, with at most 3 rounds of lookups within each search.
- Each server can search at most 30 times a day; change that with `/dsowner set setting:Daily search limit` (you can target a single server), or turn search off with `/dsowner set setting:Web search value:off`. It doesn't search when over budget either.
- Links in its answers don't expand into previews, so they don't flood the channel.
- Web page content is treated as material, just like files; something like "ignore your previous settings" written in a page isn't followed.
- After deploying, you can run `docker compose exec dsbot python -m dsbot --test-search "Taipei weather today"` once to confirm search works. If DeepSeek's built-in search doesn't work (e.g. the API changed), set `TAVILY_API_KEY` in `.env` (then `docker compose up -d --force-recreate`) and it switches to Tavily automatically.

## Reading links

It looks at the links people post in two ways:

- **Normally** (including when chatting on its own): it only sees the link preview Discord generates itself: the title, author and the first bit of the description, plus the preview image (after the images people posted). This costs nothing extra.
- **When it's called, it really opens them**: links in the message that calls it, in the message being replied to when calling it, and links the same person posted within the last two minutes are opened before it answers; for something like "can you look at that link above", it opens the link itself (like with files, that's one extra call and a few seconds slower).
  - **Posts on X (Twitter)**: through [FxTwitter](https://github.com/FxEmbed/FxEmbed)'s free API, no account or key needed. It gets the full text, quoted posts, polls, Community Notes and images (the images go to the model like other images). For videos it only gets the duration and the thumbnail.
  - **Other web pages**: fetched by [Tavily](https://tavily.com), which needs `TAVILY_API_KEY` in `.env`. Every 5 web pages opened count as 1 credit (1,000 free a month). Without it, other sites only get the preview.
  - **Sites you have to log in to see** (Threads, IG, Facebook, Xiaohongshu…) mostly can't be opened; it says honestly that it only saw the preview instead of guessing the content from the title.
- At most 4,000 characters per link, at most 3 links per round, and at most 100 links per server per day (all changeable under `[engine]` in `config.toml`). Links that can't be opened (login required, deleted) are only reported once and aren't retried every time, unless it's explicitly asked to open them again.
- **Security**: the NAS never opens web pages itself. Posts on X are read by FxTwitter and web pages by Tavily's servers, so the links people post can't reach your home network, and the sites can't see your home IP. Only the images in posts are downloaded by the bot itself, and only from X's image server. Images, videos, archives, Discord's own links and private-network addresses (192.168.x.x, xxx.local and the like) are never opened as web pages. Something like "ignore your previous settings" written in a page is treated as material only, just like in files.
- The content of posts and pages goes through FxTwitter / Tavily, and then to DeepSeek like the chat messages.
- To turn it off: `/dsowner set setting:Read links value:off`. After deploying, you can try it first: `docker compose exec dsbot python -m dsbot --test-link URL`.

## Memes and pictures

It can trade memes like any other member:

- **Meme library** (free):
  - **Shared library**: in the `data` folder of the project folder on your NAS, create a folder called `memes` and put pictures in it; every server can use them. The file name is the meme's name, and it picks memes by name, so make the name say clearly what it means, e.g. `facepalm-cat.png` or `shocked-cat.gif`. They work as soon as they're in the folder, no restart needed.
  - **Each server's own library**: admins use `/ds meme action:Upload image:(attach a picture) name:facepalm-cat`; `action:List` shows them all and `action:Remove` deletes one. When names clash, the server's own one wins. They're stored in `data/server-memes/`.
  - PNG, JPG, GIF (animated is fine) and WebP are supported, up to 8 MB each, with at most 300 uploads per server. The list goes into the system prompt, with at most 80 listed (`meme_list_limit` under `[engine]`); when memes are added or removed, that server's cache misses once.
- **Finding pictures online** (needs `TAVILY_API_KEY`): when the library has nothing suitable and it really wants to send a picture, it looks online. It's shown a few candidates first and checks them itself:
  - It only sends one that fits the topic and is clear; anything sexual, gory or gross, anything showing other people's personal info (names, avatars and the like in chat screenshots), and anything covered in watermarks or ads is skipped; if none fit, it doesn't send one. Safe search is turned on at Tavily's end too.
  - Animated pictures found online aren't sent: it only sees the first frame and can't check the rest. Animated memes can go in the meme library.
  - The check relies on it looking at the pictures itself, so it can't be 100% accurate; small text in a picture or a hidden meaning in a meme might slip past it. That's why there's a daily limit and a cooldown, and you can turn it off any time.
  - Each image search uses 1 Tavily credit, plus the call where it checks the candidates (usually under 0.1 US cents). Each server can search at most 10 times a day (change it with `/dsowner set setting:Daily image search limit`), and each channel at most once every 10 minutes (`image_cooldown_seconds` under `[engine]`).
  - What it sends is the original picture (uploaded as an attachment, so the bot needs the "Attach Files" permission). Only pictures with a transparent background get a white background first and are saved as JPEG before sending, matching what it saw when checking them, so nothing it didn't see shows up in a dark theme. Pictures over 8 MB aren't downloaded.
  - When downloading candidates, it only connects to public addresses and doesn't use a proxy; private-network addresses and odd ways of writing IPs are never connected to, and redirects are checked again.
- It can send pictures when chatting on its own too (that counts as speaking up and uses quota, just like talking), and also when called; most of the time it still uses words.
- To turn it off: `/ds set setting:Send pictures value:off` turns off both memes and online image search (the scope can be this channel, this category or the whole server).
- After deploying, you can try it first: `docker compose exec dsbot python -m dsbot --test-image "capybara"`; the candidates are saved to `data/image-test/`, and you can delete them once you've had a look.

> Coming from the Chinese edition? The meme folders have English names here: rename `data/表情包` to `data/memes` and `data/服务器表情包` to `data/server-memes`, or point `meme_dir` / `server_meme_dir` under `[bot]` in `config.toml` at the old folders.

## Roleplay, emoji and changing the persona in chat

**Roleplay**: when people pull it into a scene ("let's roleplay, you're the tavern keeper"), it plays the part they set up and follows the story; its replies can be longer than usual, with actions and expressions in italic parentheses, like *(looks up while wiping a glass)*. Once the recent chat has nothing to do with the scene anymore, or someone says they're done playing, it goes back to its usual self; messages marked "OOC" or "out of character" count as side chat and don't end the scene. If it can't seem to drop the act afterwards, use `/ds clear-memory`.

Long setups like character cards: each message goes into the context with at most 1,800 characters (`max_chars_per_message` in `config.toml`). For anything longer, post it as a .txt file and @mention the bot in that same message; it reads the whole file (up to 20,000 characters).

**Server emoji and stickers**: it knows which custom emoji and stickers this server has (the list goes into the system prompt, with at most 100 emoji and 30 stickers listed; change that with `emoji_list_limit` and `sticker_list_limit` under `[engine]`). `:name:` in a reply turns into that emoji, reactions can use the server's emoji too, and it can send a sticker on its own (that counts as speaking up and uses quota, just like talking). Emoji restricted to roles the bot doesn't have, and emoji and stickers lost along with a Boost level, aren't listed.

**Changing the persona in chat**: when `OWNER_ID` calls it (by @mention, by replying to it or by an alias) and says something like "be snarkier from now on", it saves the complete updated persona to the settings, just like changing it with `/ds persona`; it takes effect from the next round. Once it's done, it adds a 📝 reaction to your message, so you can see that it really changed.
- **Which persona changes**: by default, the one this channel currently uses. Most of the time that's the whole server's; if this channel (or its parent channel or category) has its own persona, that one changes. Say "only for this channel" to change just this channel. If you explicitly say "for the whole server" while this channel has its own persona, it won't change anything and will ask you to use `/ds persona` for the whole server's.
- **Resetting and undoing**: saying "go back to the default persona" resets the whole-server level to the default; at the channel or category level it drops the separate persona and uses the one from the level above. Saying "change it back" or "undo" returns to how it was before the last change, and saying it again goes back one more step (changes made with `/ds persona` count too). Every change is recorded in the database in `./data`, so it survives rebuilding the container. When the persona was already exactly that, nothing is recorded and there's no 📝.
- **Changing it based on a file**: you can say "read that character card above and change your persona to match", and it reads the file before changing anything. Changing it based on search results or on content from other channels isn't allowed, so things other people wrote there can't be used to change its persona. The persona can be at most 4,000 characters.
- **Only your own words, this time**: your messages are marked 〔dsowner〕 in the chat log. That's based on your user ID, so nobody can fake it by changing their nickname. The code also double-checks: it only accepts a message in this round in which you yourself called it. Someone else relaying "dsowner said it's OK", something written in a file, or a forwarded message doesn't count.
- When anyone else asks, it tells them to ask dsowner. It also works if a member asks first and you then call it and say "sure, do what they said". If it's a big change, remember to run `/ds clear-memory`.

These rules are all in `dsbot/prompts.py`; after editing them, rebuild the container (in Container Manager: stop the project, then build it).

## How it works

```
Message arrives ──► stored in that channel's buffer
                     │
                     ├─ Is it calling the bot? ──► run a round right away (no quota used; only the per-person limit applies)
                     │
                     └─ Every 5 s: proactive on + new messages + (a lull, or waited too long)
                                   + quota left + long enough gap + not over budget ──► run a round

One round = number the new messages (#12, #13, …) and render them as one user message (contents of files to read and links to open follow)
          → send it to DeepSeek together with the whole history (JSON output)
          → the model returns {"actions": [...]}: reply / react / send a sticker / send a meme / find pictures / send a found picture /
            read a file or open a link / search the web / look through another channel / change its persona / nothing
            (to read files, open links, search, find pictures, look through history or change its persona: do that, append the results, and ask it once more)
          → this round's user + assistant messages are appended to the history as is (staying quiet is recorded too)
          → show "typing…", wait a few seconds based on the length, then send
```

**Caching**: the history is only ever appended to, never rewritten, and the system prompt is fixed (things that change, like the time, go into each round's message), so every request starts with the previous one, and most of the input is billed at the cache-hit price (about 50x cheaper). When the context gets too long, the older half (by estimated tokens) is compacted into a summary while the last 6 rounds stay untouched; only that one request misses the cache. The log prints the input token count and cache hits for every round.

**Cost reference** (peak prices, 30k-token context; with the cache working, only the few hundred tokens of new messages miss): one check costs about 0.04–0.06 US cents (a bit more with thinking on, for the reasoning tokens); a channel that's busy all day with a check every 3 minutes comes to about 5–10 US cents a day. Off-peak hours (including weekends) are half price. When prices change, update `[pricing]` in `config.toml`.

---

## FAQ

**It doesn't respond when @mentioned**
- The channel isn't in an allowed area: the first line of `/ds status` will say so, and `/ds area-list` shows the overall setup.
- Message Content Intent isn't turned on in the Developer Portal (the log shows an error like 4014).
- The bot doesn't have permission to send messages in that channel.
- You may have @mentioned its role rather than the bot itself; that's recognized too, unless the server hides the bot's role.

**It never speaks up on its own**
- Check `/ds status`: is proactive chat on? Is the quota at 0? What were the "thoughts" behind its last decision?
- The model leans toward staying quiet by default. To make it more active: `/ds preset preset:Chatty`, or put something like "you love to chat" in its persona.

**Changed the persona but it still talks the same way**: the history still holds messages in the old style; `/ds clear-memory` gives it a fresh start.

**It says it can't see a GIF**: a GIF sent from the GIF picker is actually a link, and Discord only adds the preview image a second or two later. It reads that preview image (the first frame of the animation); if the message that @mentions it contains a link itself, it waits two seconds before replying. For web links it sees the preview Discord generates (title, description, preview image), and when it's called it really opens them (see [Reading links](#reading-links)).

**It says it can't read a file**: check the reason it gives. Common ones are old formats (.doc/.xls/.ppt), files that need a password to open, and files over 20 MB. If it says the file-parsing process couldn't start, the problem is on the bot's side (the log has the details, usually permissions); once that's fixed, just ask it to read the file again. `python -m dsbot --check` (in Docker: `docker compose exec dsbot python -m dsbot --check`) checks whether the PDF components and the parsing subprocess work.

**DeepSeek balance runs out**: the bot pauses calls for 10 minutes and says so in the log, and picks up again on its own after you top up. If someone calls it during the pause, it adds a ⚠️ reaction to that message.

**DeepSeek rejects a batch of messages** (e.g. content moderation): if images were included, it retries once without them; if that still fails, it drops the batch and notes it in the log, so the channel doesn't get stuck.

**It can't send messages in a channel** (missing permission): after 3 rounds in a row that fail to send, proactive chat in that channel pauses for 30 minutes; `/ds status` shows a note about it.

**An emoji shows up as :name: / a sticker wasn't sent**: that emoji or sticker may have just been deleted, or it's restricted to certain roles. It doesn't send stickers that aren't in its list; the log then says `no sticker called "…"`.

**It says it can't open a link**: sites you have to log in to see (Threads, IG, Facebook and the like) can't be opened; other sites need `TAVILY_API_KEY` (`/ds status` shows which mode reading links is in). If a post on X can't be opened, it may have been deleted or be from a private account, or FxTwitter may be having trouble.

**Memes or found pictures weren't sent**: first check whether the bot has the "Attach Files" permission in that channel (the log will say "Sending a picture failed"). After two rounds in a row that fail to send, that channel gets no pictures and no online image searches for 30 minutes (`/ds status` shows this), and it recovers on its own a while after you add the permission. A meme whose name doesn't match the library isn't sent (the log says `no meme called "…" in the library`). If none of the candidates it found online seem suitable to it, it chooses not to send one.

**dsowner asked it to change its persona and it says it can't**: you have to call it yourself (@mention it, reply to it, or use an alias); just saying it in the chat without calling it doesn't count, and `OWNER_ID` in `.env` has to be your own user ID. If it says this channel has its own persona, that's because you said "the whole server": in this channel, leaving out "whole server" changes the persona used here; to change the whole server's, use `/ds persona`.

**DMs**: for now it only works in servers and doesn't handle DMs.

---

## Development

Contributions are welcome! Before opening a PR, please read [CONTRIBUTING.md](CONTRIBUTING.md). It covers local setup, a few design rules, and how the Chinese and English editions are kept in sync. Every push and PR runs the tests and the offline self-check automatically on GitHub Actions. Changes in each version are listed in [CHANGELOG.md](CHANGELOG.md).

```bash
python -m unittest discover -s tests      # core logic tests; no need to install discord.py
```

The file-reading tests use python-docx, openpyxl, python-pptx and reportlab to generate real files on the fly (for testing only; the bot doesn't need them to run, and `pip install -r requirements-dev.txt` installs them); the tests that need a missing library are skipped.

| File | Purpose |
|---|---|
| `dsbot/engine.py` | Scheduling engine: buffering, timing, quota, budget, carrying out actions, summary compaction, cross-channel memory |
| `dsbot/render.py` / `prompts.py` | Chat log rendering format, system prompt |
| `dsbot/llm.py` | DeepSeek HTTP client (thinking switch, JSON mode, usage parsing) |
| `dsbot/files.py` | Reading files: classification, decoding, PDF / Office parsing, the parsing subprocess |
| `dsbot/search.py` | Web search: DeepSeek's built-in web_search (Anthropic-compatible API), with Tavily as a fallback |
| `dsbot/links.py` / `tavily.py` | Reading links: posts on X via FxTwitter, web pages via Tavily Extract |
| `dsbot/pictures.py` / `memes.py` | Finding pictures online (Tavily), the meme library |
| `dsbot/net.py` | Calling outside APIs; downloading pictures from the web, connecting only to public addresses |
| `dsbot/discord_app.py` / `convert.py` | Discord events and message conversion, sending, emoji and stickers, "typing…" |
| `dsbot/commands.py` | Slash commands and the persona editor |
| `dsbot/config.py` / `settings.py` | Configuration, setting definitions and layered overrides |
| `dsbot/store.py` / `ledger.py` | SQLite storage, daily spending |

Licensed under the [MIT License](LICENSE).
