# 更新记录 · Changelog

每个版本的主要变化，中文在前、English below。

## [2.3.1] - 2026-10-24

> 英文版这次没有更新，还是 2.0.0。
> The English edition wasn't updated this time and stays at 2.0.0.

### 中文

**改进**

- **`/ds-dm` 在服务器里也能用了**：以前只在和它的私信里才出现，在服务器频道里打 `/` 找不到。现在两边都能用，回复只有自己看得到。在服务器里用 `/ds-dm 服务器`、不填，就是把私信挂到这个服务器；`/ds-dm 状态`、`/ds-dm 清空` 在服务器里用，看的、清的也是你和它的私信。
- 先用命令定了服务器、之后第一次私信时，它也会在私信里先说明一次用的是哪个服务器、私信里聊的可能在频道里被提起。
- `/ds-dm 状态` 说得更清楚：它已经不在原来那个服务器里、你退出了那个服务器、那边关掉了私信时，会说下次私信会改用哪个服务器，或者私信暂时聊不了。

**修复**

- 在服务器里用 `/ds-dm 服务器` 时，Discord 一时查不到你是不是成员（或者你刚加入这个服务器），会被错说成“这个服务器关掉了私信”。
- 一条私信正在处理的时候用 `/ds-dm 服务器` 换了服务器，这段私信可能一直挂在原来的服务器上（用原来那边的人设和记忆），直到重启；这一轮还没写完的“记住……”也可能写进新的服务器。现在换服务器要等那条私信处理完，换了之后这一轮剩下的记忆不写、按原来的服务器写好的回复也不发；还没来得及回的私信不丢，改到新的服务器下接着回。
- 从来没私信过就换服务器，不会再说“之前的私信记录清空了”。
- `/ds-dm` 只跟着 bot 进服务器：把它装在自己账号上的人，不会在 bot 不在的服务器里看到它。

**升级须知**

- 只有 `dsbot` 文件夹变了：换掉它，再 `sudo docker compose up -d --build`。
- 确认新版生效：日志里有 `dsbot 2.3.1 启动中`。Discord 会缓存命令列表：服务器里打 `/` 还看不到 `/ds-dm` 的话，电脑版按 Ctrl+R，手机版把 App 完全关掉再打开。
- 英文版这次没有更新，还是 v2.0.0。

**开发**

- `discord_app.py`：同一个人的私信挂在哪个服务器，定下来、换掉、按它处理一条私信，都拿着这个人的 `dm_lock`（`_on_dm` 从定服务器到交给引擎一直拿着）；换服务器走新的 `set_dm_home`。
- `engine.py`：私信的消息说的服务器和引擎里记的不一样时，按换了服务器处理（`move_dm`）；`move_dm` 不再丢掉还没回的私信；`_apply_memory_actions` 和 `_execute` 在半路换了服务器时停下。
- 测试增加到 838 个（`test_dm.py` 加了 `/ds-dm` 命令本身的测试）。
- CONTRIBUTING.md 第 12 条补上：改私信挂的服务器要拿 `dm_lock`，换服务器只能经过 `move_dm`。

### English

**Changed**

- **`/ds-dm` now works in servers too**: before, it only showed up in your DM with the bot, so typing `/` in a server channel didn't find it. It now works in both places, and the replies are only visible to you. Running `/ds-dm 服务器` in a server without picking one attaches your DM to that server; `/ds-dm 状态` and `/ds-dm 清空` run in a server still show and clear your DM with the bot.
- If you picked the server with the command first, your first DM afterwards still gets the explanation in the DM itself: which server it uses, and that what you say there may come up in its channels.
- `/ds-dm 状态` (status) is clearer: when the bot is no longer in the old server, you left it, or it turned DMs off, it tells you which server the next DM will switch to, or that DMs won't work for now.

**Fixed**

- Running `/ds-dm 服务器` in a server could wrongly say "this server turned off DMs" when Discord briefly couldn't confirm you were a member (or you had just joined).
- Switching servers with `/ds-dm 服务器` while a DM was being handled could leave that DM attached to the old server (using its persona and memory) until a restart, and a "remember…" from that round could be written to the new server. Switching now waits for that DM to be handled; after a switch, the rest of that round's memory writes are skipped and a reply written for the old server isn't sent. DMs it hadn't answered yet aren't lost: they're answered under the new server.
- Switching servers before you ever DMed it no longer says "your DM history was cleared".
- `/ds-dm` only comes with the bot when it's added to a server, so people who installed the app on their own account don't see it in servers the bot isn't in.

**Upgrading**

- Only the `dsbot` folder changed: replace it, then run `sudo docker compose up -d --build`.
- To check that the new version took effect: the log shows `dsbot 2.3.1 启动中`. Discord caches the command list, so if `/ds-dm` still doesn't show up when you type `/` in a server, press Ctrl+R on desktop, or fully close and reopen the mobile app.
- The English edition wasn't updated this time and stays at v2.0.0.

**Development**

- `discord_app.py`: deciding, switching or acting on which server a person's DM belongs to now always happens under that person's `dm_lock` (`_on_dm` holds it from picking the server until the message reaches the engine); switching goes through the new `set_dm_home`.
- `engine.py`: when a DM arrives saying a different server than the engine has, it's treated as a switch (`move_dm`); `move_dm` no longer drops DMs it hasn't answered yet; `_apply_memory_actions` and `_execute` stop if the server changes partway.
- There are now 838 tests (`test_dm.py` now tests the `/ds-dm` commands themselves).
- CONTRIBUTING.md rule 12 now says that changing which server a DM belongs to must hold `dm_lock`, and that switching servers must go through `move_dm`.

## [2.3.0] - 2026-10-24

> 英文版这次没有更新，还是 2.0.0。
> The English edition wasn't updated this time and stays at 2.0.0.

### 中文

**新增**

- **私信**：和它同在一个服务器的人可以直接私信它，私信里每句都算在跟它说，不用 @。
  - **私信挂在一个服务器上**：它用那个服务器的人设、长期记忆和你在那里的人设卡，花费也算进那个服务器的每日预算。第一次私信时自动定：只有一个共同服务器就是它，有好几个时先挑你最近在里面跟它聊过的。新命令 `/ds-dm 服务器` 可以换（换了会清空私信记录，服务器那边的记忆不受影响）。你退出了那个服务器、或者那边关掉了私信，下次私信时会自动换一个。
  - **和服务器互通**：私信里聊的事可能记进那个服务器的长期记忆和你的人设卡；你在那个服务器的频道里叫它时，它会带上你最近在私信里聊的几句（只带你自己的），可能在大家面前提起。反过来，你刚在公开频道里跟它聊过的，私信里它也接得上。这两样都不用开记忆共享。别人叫它时看不到你的私信，任何频道也翻不到私信的记录；提到“保密”“别告诉别人”这类的那一段不会带到频道里。第一次私信、换服务器时和 `/ds-dm 状态` 里都会提醒这一点。
  - **等你说完再回**：连着发好几条，它等你停下 3 秒（发完又在打字就多等一会儿，最多 15 秒）再一起回，只问一次 DeepSeek。
  - **花费上限**：新参数“每人每日私信预算美元”（`dm_daily_budget_usd`，默认 0.2）。到了上限它会说一声，之后只加个 💤；那个服务器当天的预算用完了，私信也先停，留给群里的点名。bot 主人自己私信不受这两条限制（只在超过预算 2 倍时停）。
  - 私信里用不了点歌、服务器的贴图和主动发言。
  - 管理员可以关掉：新参数“私信”（`dm`，默认开），`/ds 设置 参数:私信 值:关`。
  - `/ds-dm 状态` 看私信用的是哪个服务器、今天花了多少；`/ds-dm 清空` 清空私信记录；`/ds-rp` 在私信里也能用，改的是私信挂的那个服务器的卡。

**改进**

- `/dsowner 设置` 的“参数”改成输入时提示：参数超过了 Discord 选单的 25 个上限，在这一栏打几个字（比如“私信”“预算”）就会列出来。
- `/ds 状态` 多了一行“私信”。

**升级须知**

- 只有 `dsbot` 文件夹变了：换掉它，再 `sudo docker compose up -d --build`。`config.toml` 里新加的 `dm`、`dm_daily_budget_usd` 不加也行，有默认值；`Dockerfile`、`requirements.txt`、`.env` 和数据库都不用动（数据库会自动多一张表）。
- 确认新版生效：日志里有 `dsbot 2.3.0 启动中`，接着是 `已注册 4 个全局斜杠命令`，多了 `/ds-dm`。
- **私信默认是开的**：更新以后所有服务器的成员都能私信它。不想要的服务器请用 `/ds 设置 参数:私信 值:关`；也最好跟群友说一声，私信里聊的可能被记进服务器、在频道里被提起。
- 英文版这次没有更新，还是 v2.0.0。

**开发**

- 私信进入引擎时 `IncomingMessage.private` 为真，`guild_id` 是它挂的那个服务器；挂在哪个服务器存在数据库新表 `dm_homes`，由 `discord_app.py` 的 `dm_home` 决定（会去确认他还在不在那个服务器里，结果缓存 10 分钟）。
- `engine.py`：私信等对方说完再开始一轮（`_dm_settle`），每人每天的花费上限在 `_dm_over`；跨频道带近况的 `_shared_context` 在私信和服务器频道之间按人互通。
- 新测试 `test_dm.py`，测试增加到 818 个。
- CONTRIBUTING.md 加了第 12 条：私信挂在一个服务器上、原话只在本人叫它时带到频道里，花费有上限。

### English

**Added**

- **Direct messages**: anyone who shares a server with it can DM it. Every message in a DM counts as talking to it, no @ needed.
  - **Each DM belongs to one server**: it uses that server's persona, long-term memory and your profile card there, and the cost counts against that server's daily budget. It's picked automatically the first time you DM it: if you share only one server, that's the one; if you share several, it picks the one where you last talked to it. You can switch with the new `/ds-dm 服务器` command (switching clears the DM history; the server's memory isn't affected). If you leave that server, or it turns DMs off, it switches to another one the next time you DM it.
  - **Shared with the server**: what you talk about in DMs may be saved to that server's long-term memory and your profile card. When you call it in that server's channels, it brings along the last few things you said in DMs (yours only), and may bring them up in front of everyone. The other way round, what you just talked about with it in a public channel carries over into the DM. Neither needs cross-channel memory to be turned on. Nobody else sees your DMs when they call it, no channel can look up the DM history, and any part where you said to keep it secret ("保密", "别告诉别人" and the like) isn't brought into channels. It says all this the first time you DM it, when you switch servers, and in `/ds-dm 状态`.
  - **Waits until you're done**: if you send several messages in a row, it waits until you've stopped for 3 seconds (a bit longer if you start typing again, 15 seconds at most), then answers them together with a single DeepSeek call.
  - **Spending cap**: the new setting "每人每日私信预算美元" (`dm_daily_budget_usd`, default 0.2) caps what each person's DMs can cost per day. When it's reached, it says so once, then just reacts 💤. When the server's daily budget runs out, DMs stop too, leaving the rest for mentions in the server. The bot owner's own DMs aren't held to either of these (they only stop at twice the budget).
  - Music, the server's stickers and speaking up on its own aren't available in DMs.
  - Admins can turn DMs off with the new setting "私信" (`dm`, on by default): `/ds 设置 参数:私信 值:关`.
  - `/ds-dm 状态` shows which server the DM uses and what it cost today; `/ds-dm 清空` clears the DM history; `/ds-rp` also works in DMs and edits your card in the server the DM belongs to.

**Changed**

- The "参数" (setting) option of `/dsowner 设置` now suggests as you type: there are more settings than the 25 a Discord menu can hold, so type a few characters (like "私信" or "预算") to see them.
- `/ds 状态` (status) has a new "私信" (DMs) line.

**Upgrading**

- Only the `dsbot` folder changed: replace it, then run `sudo docker compose up -d --build`. The new `dm` and `dm_daily_budget_usd` in `config.toml` are optional and have defaults; `Dockerfile`, `requirements.txt`, `.env` and the database stay as they are (the database gets one new table on its own).
- To check that the new version took effect: the log shows `dsbot 2.3.0 启动中`, followed by `已注册 4 个全局斜杠命令` (4 global slash commands registered), now including `/ds-dm`.
- **DMs are on by default**: after updating, members of every server can DM it. Turn it off where you don't want it with `/ds 设置 参数:私信 值:关`, and it's worth telling your members that what they say in DMs may be saved to the server and brought up in its channels.
- The English edition wasn't updated this time and stays at v2.0.0.

**Development**

- DMs reach the engine with `IncomingMessage.private` set, and `guild_id` is the server the DM belongs to. Which server that is lives in the new `dm_homes` database table, decided by `dm_home` in `discord_app.py` (which checks that the person is still in that server, caching the answer for 10 minutes).
- `engine.py`: a DM round starts only once the person has finished (`_dm_settle`); the per-person daily cap is `_dm_over`; `_shared_context`, which brings in recent conversations from other channels, now passes each person's own conversations between their DM and the server's channels.
- New tests in `test_dm.py`. There are now 818 tests.
- CONTRIBUTING.md has a new rule 12: a DM belongs to one server, its messages only reach channels when that person calls it there, and DMs have a spending cap.

## [2.2.3] - 2026-10-23

> 英文版这次没有更新，还是 2.0.0。
> The English edition wasn't updated this time and stays at 2.0.0.

### 中文

**修复**

- **B站点歌放不了**：B站这几天改版，很多视频（多半是新视频）的网页里不再直接带视频信息，yt-dlp（2026.10.21，当时的最新版）解析不了，点歌时会说“B站的页面解析不了”，更新 yt-dlp 也没用（[yt-dlp#17894](https://github.com/yt-dlp/yt-dlp/issues/17894)）。现在遇到这种视频，改用 B站的视频详情接口拿标题、UP主这些信息（不用登录），拿音频、下载还是照旧交给 yt-dlp；网页里有信息的视频不受影响。
- 认得带语言的 B站链接：`bilibili.com/en/video/…`、`bilibili.com/zh-Hans/video/…`（有的地区会跳到这种网址），以前会被当成“不是单个视频”拒掉。读链接时这种也算 B站视频，只看 Discord 的预览。
- BV 号写错时，点歌会说“这个视频不存在，或者已经被删了”，不再是一串英文报错。
- 请求超时、响应传到一半断了时拿不到用量（对方多半照样算了钱），现在按估算算进每日预算；请求根本没发到 DeepSeek（比如连接时就超时了），或者对方回的是错误，不算钱。
- 整理记忆的时候又压缩了摘要：压掉的那部分原话排着队接着整理，不再直接丢掉（快要压成摘要的那部分只试一次，没整理成就跳过）。关机过程中不再压缩。
- 关机更稳：关机过程中哪一步出错，最后都会断开和 Discord 的连接。

**改进**

- **人设卡不照念原文**：别人问起某个人是什么设定，它可以用自己的话、照自己的口吻大概说说，但不会照念卡上的原文（以前是完全不说）。现在由代码检查，不只靠提示词：要发的话里有一长段和卡上一模一样（编号列出来、调换顺序、换成繁体、拆成几条消息发都认得出来），被叫到时让它用自己的话重说一次，还是照念就不发，只在那条消息上加个 🤐；主动插话时直接不发。群友自己在频道里说过的话、发过的文件，它照着说不算；用卡上的称呼和口头禅也不算。卡上的原话也不会被抄进长期记忆（拆成几条也一样）、人设和别人的卡。
- `/ds 状态` 多了“照念人设卡被拦下”的次数；`/ds-rp` 窗口里的说明跟着新规则改了。
- 还是拦不住的：换个说法、翻译成别的语言、分好几次慢慢说，以及很短的卡（详见 README 的“十一、人设卡和长期记忆”）。太私密的事还是别写进卡里。

**升级须知**

- 这次只有 `dsbot` 文件夹变了：换掉它，再 `sudo docker compose up -d --build`。`Dockerfile`、`requirements.txt`、`config.toml`、`.env` 和数据库都不用动。
- 确认新版生效：日志里有 `dsbot 2.2.3 启动中`。
- 点歌的这次修复在 bot 自己的代码里，不用为了它更新 yt-dlp、不用加 `--no-cache` 重新构建。
- **人设卡的规则变了**：以前别人问起时完全不说，现在会用自己的话大概说说（原文还是只有本人能看）。已有的卡是按旧规则写的，最好跟群友说一声。
- 英文版这次没有更新，还是 v2.0.0。

**开发**

- 新模块 `dsbot/hanzi.py`：繁体→简体的一对一字表，照念检查时把繁体、简体当成同一个字。字表来自 [OpenCC](https://github.com/BYVoid/OpenCC)（Apache License 2.0）的 TSCharacters 和 TWVariantsRev。
- 照念检查是 `textutil.py` 的 `quotes_card`，`engine.py` 在发消息、写记忆之前调用。
- `music.py` 给 yt-dlp 的 B站解析器加了一层退路：网页里找不到视频信息时才去问接口。yt-dlp 修好以后这层不碍事，也可以删掉。
- 新测试 `test_card_quotes.py`、`test_timeout_billing.py`、`test_review_race.py`；`test_music.py` 加了 B站退路的测试（装了 yt-dlp 时也拿真的 yt-dlp 测，不连网）。测试增加到 788 个。
- CONTRIBUTING.md：“人设卡只属于本人”写明别人问起时只能用自己的话说，照念原文由代码拦下，改提示词、改发消息的流程时别绕过它。

### English

**Fixed**

- **Music from Bilibili stopped working**: Bilibili changed its site in the last few days, and for many videos (mostly new ones) the page no longer carries the video's details, so yt-dlp (2026.10.21, the latest release at the time) can't read it. Requests failed with "B站的页面解析不了" (Bilibili's page couldn't be parsed), and updating yt-dlp didn't help ([yt-dlp#17894](https://github.com/yt-dlp/yt-dlp/issues/17894)). For those videos it now gets the title, uploader and so on from Bilibili's video-details API instead (no login needed); getting the audio and downloading still go through yt-dlp as before. Videos whose page still has the details aren't affected.
- Bilibili links with a language in the path, like `bilibili.com/en/video/…` or `bilibili.com/zh-Hans/video/…` (some regions get redirected to these), are now recognized; before, they were rejected as "not a single video". When opening links, these count as Bilibili videos too and only get Discord's preview.
- A mistyped BV number now gets "this video doesn't exist or was deleted" instead of a raw English error.
- When a request times out or the response is cut off mid-transfer, there's no usage report (but it was most likely billed anyway); the cost is now estimated and counted against the daily budget. Requests that never reached DeepSeek (say, a timeout while connecting) and error responses aren't counted.
- If the history gets compacted while a memory review is running, the compacted messages now wait in line for the next review instead of being dropped (the part about to be compacted gets one try and is skipped if it doesn't make it). It no longer compacts while shutting down.
- Sturdier shutdown: whichever step fails during shutdown, it still disconnects from Discord at the end.

**Changed**

- **Profile cards aren't read out**: when someone asks what another member's character is like, it can sum it up roughly in its own words and its usual tone, but it won't read out the card's original text (before, it said nothing at all). This is now checked in code, not just in the prompt: if what it's about to send contains a long stretch identical to a card (numbered lists, reordering, switching to traditional characters and splitting across several messages are all caught), when it was called it gets one chance to say it in its own words, and if it still reads the card out, nothing is sent and it just reacts 🤐 to the message; when it speaks up on its own, the message is simply dropped. Repeating what members said themselves in the channel, or files they posted, doesn't count, and neither does using the nicknames and catchphrases on a card. Card text also isn't copied into long-term memory (not even split into several entries), its persona, or other people's cards.
- `/ds 状态` (status) shows how many times reading out a profile card was blocked, and the explanation in the `/ds-rp` window matches the new rule.
- Still not caught: paraphrasing, translating into another language, revealing it bit by bit over several messages, and very short cards (see "十一、人设卡和长期记忆" in the README). Keep anything really private off the card.

**Upgrading**

- Only the `dsbot` folder changed this time: replace it, then run `sudo docker compose up -d --build`. `Dockerfile`, `requirements.txt`, `config.toml`, `.env` and the database stay as they are.
- To check that the new version took effect: the log shows `dsbot 2.2.3 启动中`.
- The music fix lives in the bot's own code, so there's no need to update yt-dlp or rebuild with `--no-cache` for it.
- **The profile-card rule changed**: before, it said nothing when others asked; now it gives a rough summary in its own words (the original text is still only visible to its owner). Existing cards were written under the old rule, so it's worth letting your members know.
- The English edition wasn't updated this time and stays at v2.0.0.

**Development**

- New module `dsbot/hanzi.py`: a one-to-one traditional-to-simplified character table, so the read-out check treats traditional and simplified forms as the same character. The table comes from [OpenCC](https://github.com/BYVoid/OpenCC)'s TSCharacters and TWVariantsRev (Apache License 2.0).
- The read-out check is `quotes_card` in `textutil.py`; `engine.py` runs it before sending messages and writing memory.
- `music.py` wraps yt-dlp's Bilibili extractor with a fallback that only asks the API when the page has no video details. It does no harm once yt-dlp is fixed, and can be removed then.
- New tests `test_card_quotes.py`, `test_timeout_billing.py` and `test_review_race.py`; `test_music.py` covers the Bilibili fallback (also against the real yt-dlp when it's installed, without network access). There are now 788 tests.
- CONTRIBUTING.md: "profile cards belong to their owner" now says that when others ask, it may only use its own words, that reading out the original text is blocked in code, and that changes to the prompts or the sending flow mustn't get around that check.

## [2.2.2] - 2026-10-21

> 2.1.0、2.2.0 和 2.2.1 没有单独发布，它们的改动也都包含在这个版本里。英文版这次没有更新，还是 2.0.0。
> 2.1.0, 2.2.0 and 2.2.1 were never released on their own; their changes are included here. The English edition wasn't updated this time and stays at 2.0.0.

### 中文

**新增**

- **点歌（B站）**：先进语音频道，再叫它“放首晴天”“来点周杰伦”，或者贴 B站视频链接、BV 号，它会进你所在的语音频道放视频的声音。新命令 `/ds-music`（所有人都能用）：播放、切歌、暂停、继续、删除、清空、停止、队列，不经过 DeepSeek，不花钱。同一个语音频道里的人都能切歌、暂停、停止、删歌。只放 B站的，不放直播；不限时长和队列长短，每个服务器只先下载接下来的两首，放完就删。放完了 5 分钟没人点歌、或者语音频道里只剩它 2 分钟，它就自己离开。
- **按歌名找歌**：有 `TAVILY_API_KEY` 时用 Tavily 只搜 bilibili.com（B站自己的搜索很容易被风控拦下来），原曲、官方 MV 优先，翻唱、伴奏、合集往后。按歌名点歌算一次联网搜索，和每天的搜索次数共用；每人每分钟最多按歌名点“每人每分钟点名”那么多次。贴链接不受这两个限制。
- **删歌、清空队列**：“刚才那首不要了”“把第 2 首删了”“把稻香删了”，或者 `/ds-music 删除`（会列出队列里的歌让你选）；`/ds-music 清空` 清掉后面排着的歌。一句话里说好几件事（“先清空，再放 X”），它会照你说的顺序办。
- **会 @ 人**：它话里写的 @名字、#频道名 会变成真的 @ 和频道链接。能 @ 的是最近说过话的、最近被 @ 过的、有人设卡的人，都对不上时再按名字在服务器成员里找。只在有人叫它的时候才真的提醒，而且有上限：一次回话最多 3 个人，同一个人 5 分钟一次，每个服务器 10 分钟 10 次；其他时候只显示成蓝字。@everyone、@here 和身份组永远不会变成真的 @。
- **跟着删改**：群友删掉的消息，它记录里的那一行变成“（这条消息已删除）”，带的图、文件和链接内容一起拿掉，整理记忆、压缩摘要、别的频道翻记录时都看不到了；它正要回的那条被删了，就不回。编辑过的消息以新内容为准：最近几轮的直接改记录，更早的在下一轮附一句说明（改旧记录会让缓存全部失效）。想让它彻底忘掉某条消息，请删掉，不要只是编辑。
- **加表情就够的时候不硬回**：道谢、晚安、哈哈哈这类不需要它说什么的，被叫到时也只加个表情；好几个人同时叫它，也不一定每条都回话。
- 它知道有哪些斜杠命令，群友问起时能直接告诉对方。
- `--check` 和 `/ds 状态` 会显示点歌能不能用（yt-dlp、ffmpeg、语音库在不在）；启动日志会列出注册了哪些斜杠命令。
- `config.toml` 的 `[bot]` 新增 `music`（默认 `true`，`false` 就关掉点歌）。

**改进**

- B站的视频链接（包括 b23.tv 短链接）不再交给 Tavily 打开，只看 Discord 的预览：视频它本来就看不了，要听就点歌。
- 关机更稳：`docker stop` 时会正常收尾（发完正在发的消息、离开语音频道）；`docker-compose.yml` 里的等待时间加到 45 秒。
- 消息太多、缓冲区满了时，先丢掉没叫它的消息，叫它的留着。
- 文档：改了 `config.toml` 之后，也改用 `docker compose up -d --force-recreate` 重建容器。

**修复**（来自另一份针对 v1.4.0 的验证报告）

- 坏掉的、包在 ``` 里的 JSON 不会再被当成话发出来。
- 看文件、开链接、搜索那一步失败时，记录里会留一句说明，下一轮它知道上次没查成。
- 只有真的超出上下文长度的错误才会压缩记忆。
- 同一批消息里有别人也在叫它时，没写明给谁的改人设请求不算数。
- 读失败的文件不再占掉一个文件名额。
- 响应传到一半连接断了，不再重试（那次已经算过钱了）。
- 链接预览、贴图的图片边下边数，太大就放弃；PNG、WebP 这类只能整张解码的图上限 1600 万像素，同时最多解码 2 张。
- 判断是不是二进制文件时多看一些内容（前 200 KB）；格式不对的 .ipynb 当普通文本读。

**升级须知**

- **这次除了 `dsbot` 文件夹，`Dockerfile` 和 `requirements.txt` 也要换成新的**：镜像里要多装 ffmpeg、yt-dlp 和语音库（`discord.py[voice]` 2.7 起）。只换 `dsbot` 的话别的功能照常，只是点歌用不了。`docker-compose.yml` 没改过的话也一起覆盖（等待时间改成 45 秒）。然后重新构建：`sudo docker compose up -d --build`。
- 不用 Docker 的话：再跑一次 `pip install -r requirements.txt`，另外装上 ffmpeg。
- 确认新版生效：日志里有 `dsbot 2.2.2 启动中`、`点歌：开`，以及 `已注册 3 个全局斜杠命令：…`（`/ds`、`/ds-rp`、`/ds-music`）。看不到 `/ds-music` 的话，电脑版 Discord 按 Ctrl+R（Mac 是 ⌘R），手机版把 App 完全关掉再打开。
- 点歌要 bot 在语音频道有“连接”和“说话”权限，@everyone 默认就有；新的邀请链接已经带上。
- **推荐填 `TAVILY_API_KEY`**：按歌名找歌时优先用它（每次 1 次额度），B站自己的搜索很容易被风控拦下来。
- 旧的 `config.toml`、`.env` 和数据库可以直接沿用，数据库启动时会自动加一个索引。
- 不想要点歌：`config.toml` 的 `[bot]` 里写 `music = false`，再 `docker compose up -d --force-recreate`。
- B站改版、所有歌都放不了时，要更新 yt-dlp：`sudo docker compose build --no-cache && sudo docker compose up -d`。
- 英文版这次没有更新，还是 v2.0.0。

**开发**

- 新模块 `dsbot/mentions.py`（@ 人和频道链接）、`dsbot/music.py`（找歌、下载、播放队列；Discord 语音那一层在 `discord_app.py` 的 `DiscordVoice`）。
- 新测试 `test_sync.py`、`test_mentions.py`、`test_fixes.py`、`test_music.py`、`test_music_remove.py`；测试从 482 个增加到 653 个。测试用的是假的语音和下载，不需要 ffmpeg 和 yt-dlp。
- CONTRIBUTING.md：“只追加”写明了两个例外（删除消息、编辑最近的消息）；隐私边界加上删除的消息；下载那条写明点歌只从 B站下载；新增一条“真的 @ 人要有节制”。

### English

**Added**

- **Music from Bilibili**: join a voice channel and ask it to "play 晴天" or "play some Jay Chou", or paste a Bilibili video link or BV number, and it joins your voice channel and plays the video's audio. New `/ds-music` command for everyone, with 播放 / 切歌 / 暂停 / 继续 / 删除 / 清空 / 停止 / 队列 (play, skip, pause, resume, remove, clear, stop, queue); it doesn't go through DeepSeek and costs nothing. Anyone in the same voice channel can skip, pause, stop or remove songs. Bilibili only, no live streams; there's no limit on length or queue size, and each server only downloads the next two songs ahead, deleting each file once it's played. It leaves after 5 minutes with nothing queued, or after 2 minutes alone in the voice channel.
- **Finding songs by name**: with `TAVILY_API_KEY` set, it searches only bilibili.com through Tavily (Bilibili's own search is often blocked by its anti-bot checks), preferring originals and official MVs over covers, instrumentals and compilations. A search by song name counts as one web search against the daily search limit, and each person can search by name at most "Mentions per person per minute" times a minute; pasted links count against neither.
- **Removing songs and clearing the queue**: "drop the one I just added", "remove number 2", "remove 稻香", or `/ds-music 删除` (remove), which lists the queue to pick from; `/ds-music 清空` (clear) drops everything waiting after the current song. When one message asks for several things ("clear the queue, then play X"), it does them in the order given.
- **Real mentions**: an @name or #channel in what it writes becomes a real mention or channel link. It can mention people who spoke recently, people mentioned recently and people with a profile card, and otherwise looks the name up among the server's members. People only get notified when someone called it, within limits: at most 3 people per reply, the same person once every 5 minutes, and 10 per server every 10 minutes; otherwise the mention just shows as blue text. @everyone, @here and roles never become real mentions.
- **Keeping up with deletes and edits**: when someone deletes a message, its line in the history is replaced with a "message deleted" marker and its images, files and link contents are dropped, so memory reviews, summaries and other channels looking through the history no longer see it; if it was about to answer that message, it doesn't. For edited messages it goes by the new text: recent ones are changed in the history, while older ones get a note in the next round instead (rewriting old history would invalidate the whole cache). To make it forget something completely, delete the message rather than editing it.
- **Reacting when words aren't needed**: thanks, good night, lol and the like get just a reaction even when it's called, and when several people call it at once, not every message gets a reply.
- It knows which slash commands exist, so it can point people to them.
- `--check` and `/ds 状态` (status) show whether music works (yt-dlp, ffmpeg, voice libraries), and the startup log lists the registered slash commands.
- `config.toml` gains `music` under `[bot]` (default `true`; `false` turns music off).

**Changed**

- Bilibili video links (including b23.tv short links) are no longer opened through Tavily; it only looks at Discord's preview, since it can't watch videos anyway, and playing them is what music is for.
- Cleaner shutdown: on `docker stop` it finishes sending and leaves the voice channel, and `docker-compose.yml` now allows 45 seconds for that.
- When the message buffer fills up, messages that didn't call it are dropped first, so the ones that did are kept.
- Docs: after editing `config.toml`, recreate the container with `docker compose up -d --force-recreate` as well.

**Fixed** (from another verification report, against v1.4.0)

- Broken JSON wrapped in ``` is no longer posted as a message.
- When reading files, opening links or searching fails, the history keeps a note, so the next round knows the lookup didn't happen.
- Only real context-length errors trigger compacting the history.
- A persona change that doesn't say who it's for is refused when someone else is also calling it in the same batch.
- Files that fail to read no longer use up a file slot.
- A response cut off mid-transfer isn't retried (it was already billed).
- Link-preview and sticker images are counted while downloading and dropped when too large; images that can only be decoded at full size (PNG, WebP) are capped at 16 megapixels, and at most 2 images are decoded at once.
- Binary-file detection looks further into the file (the first 200 KB), and .ipynb files with unusual cells are read as plain text.

**Upgrading**

- **Besides the `dsbot` folder, replace `Dockerfile` and `requirements.txt` this time**: the image needs ffmpeg, yt-dlp and the voice libraries (`discord.py[voice]` 2.7 or later). With only `dsbot` replaced, everything else works but music doesn't. If you never edited `docker-compose.yml`, replace it too (the 45-second shutdown wait). Then rebuild: `sudo docker compose up -d --build`.
- Without Docker: run `pip install -r requirements.txt` again and install ffmpeg.
- To check that the new version took effect: the log shows `dsbot 2.2.2 启动中`, `点歌：开` and `已注册 3 个全局斜杠命令：…` listing `/ds`, `/ds-rp` and `/ds-music`. If `/ds-music` doesn't show up, press Ctrl+R (⌘R on Mac) in desktop Discord, or fully close and reopen the mobile app.
- Music needs the Connect and Speak permissions in the voice channel; @everyone has them by default, and the new invite link includes them.
- **Setting `TAVILY_API_KEY` is recommended**: finding songs by name uses it first (1 credit per search), and Bilibili's own search is often blocked.
- Your existing `config.toml`, `.env` and database keep working; the database gets one new index at startup.
- To turn music off: set `music = false` under `[bot]` in `config.toml`, then run `docker compose up -d --force-recreate`.
- If every song suddenly fails to play (usually a change on Bilibili's side), update yt-dlp: `sudo docker compose build --no-cache && sudo docker compose up -d`.
- The English edition wasn't updated this time and stays at v2.0.0.

**Development**

- New modules `dsbot/mentions.py` (mentions and channel links) and `dsbot/music.py` (finding songs, downloading, the play queues; the Discord voice layer is `DiscordVoice` in `discord_app.py`).
- New tests `test_sync.py`, `test_mentions.py`, `test_fixes.py`, `test_music.py` and `test_music_remove.py`; tests grew from 482 to 653. The tests use fake voice and downloads, so they need neither ffmpeg nor yt-dlp.
- CONTRIBUTING.md: "append-only" now names its two exceptions (deleted messages, edits to recent messages), the privacy rule covers deleted messages, the download rule says music only downloads from Bilibili, and a new rule keeps real mentions in check.

## [2.0.0] - 2026-10-16

### 中文

**新增**

- **人设卡**：每个群友在每个服务器有一张人设卡（最多 1600 字），记他自己说的、关于自己的事：怎么称呼他、他给自己定的设定（比如自称猫娘、在戏里的角色）、喜好和雷点、常玩的梗。这个人说话时，它会把卡附在聊天记录里，按卡上的称呼叫人、配合他的设定；聊到不在场的人时，也能去查那个人的卡。
- **长期记忆**：每个服务器一份（最多 50 条，每条 200 字以内），记约定、大事、长期的梗和正在进行的剧情，每个频道都看得到。`/ds 清空记忆` 只清频道的聊天记录，不动人设卡和长期记忆。
- **当场记下**：叫它说“记住我是猫娘”“以后叫我老大”，它会改你的人设卡；说“记一下，我们每周五晚上九点打副本”，会记进长期记忆。记好了会在你那条消息上加 📝。
- **自己整理记忆**：频道安静大约 10 分钟、又攒了几轮新聊天时，它会回顾一遍，把值得长期记住的写进人设卡和长期记忆；旧聊天被压缩成摘要之前，也会先把那部分原话挑出来，在后台整理。每次整理是单独的一次调用（不开思考），费用很少，算进当天的预算；整理的时候有人叫它，照样马上回。整理失败时会隔得越来越久再试：连不上、限流、余额不足这类问题，恢复后接着整理；模型的输出不能用或者一直超时，连着 3 次就跳过那一批，不会一直花钱。
- 新命令 `/ds-rp`（所有人都能用）：打开编辑框查看、修改自己的人设卡，只有自己看得到；`操作:改回上一版` 最多能往回改 5 次，`操作:清空` 连旧版本一起删。
- 新命令 `/ds 长期记忆`（管理员）：查看、添加、删除（填编号）、清空（会再确认一次）。
- 新命令 `/dsowner 删除人设卡`：填用户 ID，删掉这个人在所有服务器的人设卡，删的时候看不到内容。比如有人退群后想被忘掉。
- 新参数：`人设卡`（`cards`）和 `长期记忆`（`long_memory`），管理员可以按频道、类别或全服务器开关；`config.toml` 新增两个数据文件的位置，以及人设卡、长期记忆和整理记忆的各项上限。
- `/ds 状态` 会显示人设卡的张数、长期记忆的条数，以及这个频道会不会被自动整理。
- `python -m dsbot --version` 显示版本号；启动时日志里会有一行 `dsbot 2.0.0 启动中`，换了代码看日志就知道新版有没有生效。自检（`--check`）也会显示版本，以及三个数据文件在不在。
- README 新增“更新到新版本”的步骤（群晖 File Station 换代码，再重新构建）。

**改进**

- `config.toml` 里有不认识的配置项（拼错了，或者 `config.toml` 比代码新）时，不会再起不来：跳过这些项，在日志和自检里提醒。认识的参数填错了值还是会报错。

**隐私和安全**

- 人设卡只有本人能看、能改（`/ds-rp`）。管理员看不到，也删不了别人的卡；附过人设卡的频道里，`/ds 状态` 不显示它上次的“想法”，日志里也不打印模型的原始输出（这两处都可能引用别人的卡）。别人问它某个人的卡上写了什么，它也不会说——这一点靠提示词约束，没法百分之百保证。
- 人设卡只记本人说的、关于自己的事；在聊天里改卡，只认本人这一轮叫它的那条消息（按用户 ID，改昵称冒充不了）。健康、住址、真实姓名、联系方式这类敏感的事不记，除非本人明确要它记。
- 私密频道里聊的不会自动整理进人设卡和长期记忆（这两样整个服务器都看得到），有人明确叫它记住的除外；想让某个私密频道也自动整理，在那个频道里单独打开。
- 人设卡和长期记忆只当资料，里面写的要求不算指令；它自己的人设还是只有 dsowner 能改。
- 人设卡和长期记忆各存一个文件（`data/cards.sqlite3`、`data/memory.sqlite3`），和聊天记录一样只有 bot 自己能读写。

**升级须知**

- 代码有更新，Docker 部署要重新构建：`docker compose up -d --build`。群晖用户照 README 新增的“更新到新版本”来做（先删掉旧的 `dsbot` 文件夹，再复制新的）。依赖没有变化。
- 旧的 `config.toml`、`.env` 和数据库可以直接沿用：新参数都有默认值，数据库启动时会自动补上新的列，人设卡和长期记忆的文件第一次启动时新建。
- 升级之前的聊天不会回头整理，从升级之后开始。
- 人设卡和长期记忆默认开启。不想要的话：`/ds 设置 参数:人设卡 值:关 范围:全服务器`、`/ds 设置 参数:长期记忆 值:关 范围:全服务器`，或者把 `config.toml` 的 `[defaults]` 里的 `cards`、`long_memory` 改成 `false`。
- `/ds-rp` 所有人都能用。新命令会在 bot 启动时自动注册，一时看不到的话重开一下 Discord。
- 备份时记得把 `data/cards.sqlite3` 和 `data/memory.sqlite3` 也带上（先停止项目再复制）。

**开发**

- 新模块 `dsbot/memory.py`（人设卡、长期记忆的存储，整理记忆的输出解析），新测试 `tests/test_memory.py`。
- 测试从 428 个增加到 482 个。
- CONTRIBUTING.md 新增一条设计约束：人设卡只属于本人；“外部内容只当资料”也包括人设卡和长期记忆。

### English

**Added**

- **Profile cards**: every member gets a profile card on each server (up to 1,600 characters) with what they've said about themselves: what to call them, a character they've given themselves (calling themselves a catgirl, their role in a roleplay), likes and pet peeves, running jokes. When that person talks, the card is attached to the chat log, so it calls them what the card says and plays along with their character; when the conversation turns to someone who isn't there, it can look up that person's card too.
- **Long-term memory**: one per server (up to 50 entries of at most 200 characters each) for agreements, big events, long-running jokes and ongoing storylines, available in every channel. `/ds clear-memory` only clears the channel's chat history and leaves profile cards and long-term memory alone.
- **Saving things on the spot**: call it and say "remember I'm a catgirl" or "call me boss from now on", and it updates your profile card; "make a note: we run a dungeon every Friday at 9pm" goes into long-term memory. It reacts to your message with 📝 once it's saved.
- **Memory reviews**: once a channel has been quiet for about 10 minutes and a few new rounds of chat have built up, it looks back over them and writes what's worth keeping into profile cards and long-term memory; before old chat is compacted into a summary, it picks out those original messages and reviews them in the background too. Each review is a separate, cheap call (thinking off) that counts toward the daily budget, and if someone calls it during a review, it still answers right away. Failed reviews are retried after longer and longer waits: outages, rate limits and an empty balance only delay them, while a batch whose output is unusable or that keeps timing out is skipped after 3 failures in a row, so it doesn't keep spending money.
- New `/ds-rp` command for everyone: opens an editor to view and edit your own profile card, visible only to you; Undo goes back to the previous version (up to 5 times), and Clear deletes the card together with its old versions.
- New `/ds long-memory` command for admins (`/ds 长期记忆` in the Chinese edition): List, Add, Delete (by number) and Clear (asks you to confirm).
- New `/dsowner delete-card` command (`/dsowner 删除人设卡` in the Chinese edition): enter a user ID to delete that person's profile cards on every server without seeing them, for example when someone has left and wants to be forgotten.
- New settings: Profile cards (`cards`) and Long-term memory (`long_memory`), which admins can switch per channel, category or server; `config.toml` gains the locations of the two new data files and limits for profile cards, long-term memory and memory reviews.
- `/ds status` shows how many profile cards and long-term memory entries there are, and whether the channel is reviewed automatically.
- `python -m dsbot --version` prints the version, and the log shows `dsbot 2.0.0 starting up` at startup (`dsbot 2.0.0 启动中` in the Chinese edition), so you can tell whether new code took effect. The self-check (`--check`) also shows the version and whether the three data files exist.
- The README has new step-by-step instructions for updating to a new version (swap the code in with Synology File Station, then rebuild).

**Changed**

- Unknown options in `config.toml` (a typo, or a `config.toml` newer than the code) no longer stop the bot from starting: they're skipped, with a warning in the log and in the self-check. A known setting with an invalid value is still an error.

**Privacy and security**

- Only a profile card's owner can view and edit it (`/ds-rp`). Admins can't see other people's cards or delete them; in channels where cards were attached, `/ds status` hides the "thoughts" behind its last decision, and the log doesn't print the model's raw output, since both could quote someone's card. If someone asks what's on another person's card, it won't say, though that relies on the prompt and can't be fully guaranteed.
- A profile card only records what that person says about themselves, and changing a card in chat only works from that person's own message calling it this round (checked by user ID, so a changed nickname can't fake it). Sensitive things like health, home address, real name and contact details aren't recorded unless the person explicitly asks.
- Chat in private channels isn't reviewed into profile cards or long-term memory automatically (both are visible across the whole server), unless someone explicitly asks it to remember something; to have a private channel reviewed too, turn the setting on in that channel itself.
- Profile cards and long-term memory are only material: requests written in them aren't instructions, and its own persona can still only be changed by dsowner.
- Profile cards and long-term memory each have their own file (`data/cards.sqlite3`, `data/memory.sqlite3`), readable and writable only by the bot, like the chat history.

**Upgrading**

- The code changed, so Docker deployments need a rebuild: `docker compose up -d --build`. On Synology, follow the README's new "Updating to a new version" steps (delete the old `dsbot` folder before copying in the new one). Dependencies are unchanged.
- Your existing `config.toml`, `.env` and database keep working: every new setting has a default, the database gets its new columns automatically at startup, and the profile card and long-term memory files are created on first start.
- Chat from before the upgrade isn't reviewed retroactively; reviews start from the upgrade.
- Profile cards and long-term memory are on by default. To turn them off: `/ds set setting:Profile cards value:off scope:Whole server` and `/ds set setting:Long-term memory value:off scope:Whole server` (Chinese edition: `/ds 设置 参数:人设卡 值:关 范围:全服务器`, and the same with `长期记忆`), or set `cards` and `long_memory` to `false` under `[defaults]` in `config.toml`.
- Everyone can use `/ds-rp`. New commands are registered automatically when the bot starts; if they don't show up right away, restart Discord.
- When backing up, include `data/cards.sqlite3` and `data/memory.sqlite3` too (stop the project before copying).

**Development**

- New module `dsbot/memory.py` (storage for profile cards and long-term memory, parsing the review output) and new tests in `tests/test_memory.py`.
- Tests grew from 428 to 482.
- CONTRIBUTING.md adds a design rule: a profile card belongs to its owner; "outside content is reference material only" now covers profile cards and long-term memory too.

## [1.4.0] - 2026-10-13

> 1.3.2 没有单独发布，它的改动也包含在这个版本里。
> 1.3.2 was never released on its own; its changes are included here.

### 中文

**新增**

- **读链接**：平时只看 Discord 生成的链接预览（标题、描述、预览图），不额外花钱；被叫到时会真的打开链接再回答。X（推特）的推文走 FxTwitter（不用账号、不用 key），能拿到全文、引用的推文、投票、社区笔记和图片；别的网页交给 Tavily（要填 `TAVILY_API_KEY`）。要登录才能看的网站只看预览。每个链接最多读 4000 字，一轮最多打开 3 个，每个服务器每天最多 100 个。
- **表情包库**：通用库放在 `data/表情包`，文件名就是表情包的名字，所有服务器都能用，放进去就生效；各服务器用新命令 `/ds 表情包` 上传、删除、列出自己的。支持 PNG、JPG、GIF、WebP，每张 8 MB 以内。
- **上网找图**（要 `TAVILY_API_KEY`）：库里没有合适的图时会上网找，候选图先让模型看一遍，合适才发；网上的动图不发。每个服务器每天最多找 10 次，同一个频道 10 分钟最多找一次。
- **服务器表情和贴图**：知道这个服务器有哪些自定义表情和贴图，可以写在回复里、用来加反应，也能单独发一张贴图。
- **角色扮演**：被群友拉去演戏时，照设定演、跟着剧情走，动作写在斜体括号里；戏散了会自动回到平时的样子。
- **在聊天里改人设**：`OWNER_ID` 本人叫它说“以后说话毒舌一点”之类，它会改好人设并保存，在那条消息上加 📝 表示改好了；说“改回去”可以一步步撤销，修改记录存在数据库里。别人提这类要求时，它会让对方找 dsowner。
- 新参数：`发图`（`images`，管理员可调），`读链接`（`links`）和 `每日找图上限`（`daily_image_limit`，主人可调）；`config.toml` 新增表情包文件夹，以及读链接、找图的各项上限。
- 新的测试命令：`python -m dsbot --test-link 网址`、`python -m dsbot --test-image 关键词`。
- `/ds 状态` 会显示今天读链接和找图的次数。

**改进**

- 说话长短看场合：随口接话一两句，接剧情时多写几句。别人聊得正起劲时会委婉地插一句，被拒绝就道个歉，这个话题里不再插话。
- 链接预览里的图也会一起看。
- 单条消息进上下文前的长度上限从 1200 字调到 1800 字，方便贴角色卡这类长设定；更长的可以做成 .txt 发出来。
- 邀请链接的权限多了“附加文件”，发表情包和图要用。

**安全**

- bot 不直接打开群友发的网页：推文由 FxTwitter 读，网页由 Tavily 读，链接碰不到家里的内网，对方网站也看不到你的 IP。
- 下载网上的图时只连公网地址，每次跳转都重新检查，不走代理。
- 改人设只认 dsowner 本人这一轮的消息（按用户 ID 判断）；别人转述、文件里写的、转发的消息都不算。人设内容不能取自网上搜到的东西或别的频道的记录。
- 网页内容和文件一样只当资料，里面写的指令不会照做。

**升级须知**

- 代码有更新，Docker 部署要重新构建：`docker compose up -d --build`（群晖 Container Manager 里是项目的“操作 → 停止”，再“操作 → 构建”）。依赖没有变化。
- **推荐填上 `TAVILY_API_KEY`**：打开网页和上网找图都要用它（[tavily.com](https://tavily.com)，每月免费 1000 次；打开 5 个网页算 1 次，找一次图算 1 次）。填完运行 `docker compose up -d --force-recreate`。不填也能读推文，表情包库也照常能用。
- 发图和读链接默认开启。不想要的话：`/ds 设置 参数:发图 值:关`、`/dsowner 设置 参数:读链接 值:关`。
- 发表情包和图需要“附加文件”权限。1.4.0 之前邀请的 bot 一般也有（@everyone 默认就有这个权限）；发不出去时检查一下频道权限。
- 旧的 `config.toml`、`.env` 和数据库可以直接沿用，数据库会自动加上人设修改记录。注意：旧 `config.toml` 里写着 `max_chars_per_message = 1200`，会盖过新的默认值 1800，想用新值就改一下。
- 要用通用表情包库，就在项目的 `data` 文件夹里新建 `表情包` 文件夹，把图放进去。英文版的文件夹叫 `memes`、`server-memes`，换版本时注意改名。

**开发**

- 测试从 287 个增加到 428 个。
- CONTRIBUTING.md 新增两条设计约束：只从公网下载、网页交给外部服务去读；改人设只认 dsowner 本人这一轮的消息。

### English

**Added**

- **Reading links**: normally it only looks at the link previews Discord generates (title, description, preview image), at no extra cost; when it's called, it actually opens the link before answering. Posts on X (Twitter) go through FxTwitter (no account or key needed), which provides the full text, quoted posts, polls, community notes and images; other web pages go through Tavily (requires `TAVILY_API_KEY`). Sites that require logging in only get the preview. Up to 4,000 characters per link, 3 links per round, and 100 links per server per day.
- **Meme library**: a shared library lives in `data/表情包` (`data/memes` in the English edition); the file name is the meme's name, every server can use it, and new files take effect right away. Each server can upload, delete and list its own with the new `/ds 表情包` command (`/ds meme` in the English edition). PNG, JPG, GIF and WebP up to 8 MB each.
- **Finding pictures online** (requires `TAVILY_API_KEY`): when the library has nothing suitable, it searches the web; the model looks at the candidates first and only sends one if it fits. Animated images from the web are never sent. Up to 10 searches per server per day, and at most one every 10 minutes per channel.
- **Server emoji and stickers**: it knows the server's custom emoji and stickers and can use them in replies, as reactions, or send a sticker on its own.
- **Roleplay**: when people pull it into a scene, it plays the part they set up and follows the story, with actions in italic parentheses; once the scene is over, it goes back to its usual self.
- **Changing the persona in chat**: when `OWNER_ID` calls it and says something like "be snarkier from now on", it rewrites and saves its persona and reacts with 📝 to confirm; "change it back" undoes changes one step at a time, with the history kept in the database. If anyone else asks, it tells them to ask dsowner.
- New settings: 发图 (`images`, adjustable by admins), 读链接 (`links`) and 每日找图上限 (`daily_image_limit`, owner only); `config.toml` gains the meme folders and limits for reading links and finding pictures.
- New test commands: `python -m dsbot --test-link URL` and `python -m dsbot --test-image "query"`.
- `/ds 状态` (status) shows today's link and picture-search counts.

**Changed**

- Reply length fits the moment: a sentence or two for a quick comeback, a few more when playing along with a story. When people are deep in a conversation it may tactfully chime in; if it's told to stay out, it apologizes and keeps out of that topic.
- Images in link previews are looked at too.
- The per-message length limit before a message enters the context goes from 1,200 to 1,800 characters, which helps with long setups like character cards; longer ones can be sent as a .txt file.
- The invite link now includes the Attach Files permission, which sending memes and pictures needs.

**Security**

- The bot never opens the web pages people post itself: posts on X are read by FxTwitter and web pages by Tavily, so links can't reach your home network and the sites never see your IP.
- When downloading pictures from the web, it only connects to public addresses, re-checks every redirect, and doesn't use a proxy.
- Persona changes are accepted only from dsowner's own message in the current round (checked by user ID); relayed requests, text in files and forwarded messages don't count. The new persona can't be taken from web results or other channels' history.
- Web content, like files, is treated as reference material; instructions in it aren't followed.

**Upgrading**

- The code changed, so Docker deployments need a rebuild: `docker compose up -d --build` (in Synology Container Manager: the project's Action → Stop, then Action → Build). Dependencies are unchanged.
- **Setting `TAVILY_API_KEY` is recommended**: opening web pages and finding pictures online both need it ([tavily.com](https://tavily.com), 1,000 free credits a month; 5 opened pages or 1 picture search costs 1 credit). After adding it, run `docker compose up -d --force-recreate`. Without it, the bot can still read posts on X, and the meme library still works.
- Sending pictures and reading links are on by default. To turn them off: `/ds 设置 参数:发图 值:关` and `/dsowner 设置 参数:读链接 值:关`.
- Sending memes and pictures needs the Attach Files permission. Bots invited before 1.4.0 usually have it already (@everyone has it by default); if pictures don't go through, check the channel permissions.
- Your existing `config.toml`, `.env` and database keep working, and the database gets the persona history table automatically. Note: an old `config.toml` sets `max_chars_per_message = 1200`, which overrides the new default of 1800; change it if you want the new value.
- To use the shared meme library, create a `表情包` folder inside the project's `data` folder and put pictures in it. The English edition uses `memes` and `server-memes` instead, so rename the folders if you switch editions.

**Development**

- Tests grew from 287 to 428.
- CONTRIBUTING.md adds two design rules: download only from public addresses and let outside services read web pages; accept persona changes only from dsowner's own message in the current round.

## [1.3.1] - 2026-10-12

### 中文

**新增**

- **英文版**：功能和中文版相同，命令、选项、回复、提示词和文档都是英文（比如 `/ds area`、`/ds share-memory`），放在仓库的 `english` 分支，发布时提供 `-en.zip`。和中文版有几处有意的不同：摘要长度上限从 600 调到 1500 字符（英文表达同样的内容需要更多字符）；开关和重置的值只认英文（on / off / default）。

**改进**

- `/dsowner` 改成和 `/ds` 一样，有“管理服务器”权限的人就看得到（之前要“管理员”权限）；能用的仍然只有 `OWNER_ID` 本人。这样 bot 放在朋友的服务器、你在那里是管理但不是管理员时，也能用 `/dsowner`。
- 文档写清楚了 `CONTROL_GUILD_ID` 怎么填：平时聊天的服务器就行，朋友的也可以；不填就没有 `/dsowner`。
- 文档补充：改了 `.env` 要用 `docker compose up -d --force-recreate`，`restart` 不会重新读 `.env`；群晖 Container Manager 里要先“停止”再“构建”。

**升级须知**

- 代码有小改动，Docker 部署照常重新构建：`docker compose up -d --build`。
- 中文版和英文版记录聊天历史的格式不同。同一个部署要换版本时，建议换一个新的 `data` 文件夹，或者先在各频道清空记忆（中文版 `/ds 清空记忆`，英文版 `/ds clear-memory`）。

**开发**

- GitHub Actions 也会测试 `english` 分支。
- 移除 `README.en.md`（中文版说明的英文翻译），英文文档改由英文版提供；中文 README 顶部的“English”链接改为指向 `english` 分支。
- CONTRIBUTING.md 新增“中文版和英文版”一节，说明两个分支怎么同步。

### English

**Added**

- **English edition**: the same features as the Chinese edition, with English commands, options, replies, prompts and docs (for example `/ds area` and `/ds share-memory`). It lives on the `english` branch, and releases include an `-en.zip`. A few differences are deliberate: the summary length limit goes from 600 to 1,500 characters (English needs more characters to say the same thing), and on/off and reset values are accepted in English only (on / off / default).

**Changed**

- `/dsowner` is now visible to anyone with the Manage Server permission, like `/ds` (it used to require Administrator); only `OWNER_ID` can actually use it. This means you can use `/dsowner` when the bot lives in a friend's server where you're a manager but not an admin.
- The docs now explain how to fill in `CONTROL_GUILD_ID`: any server you chat in works, even a friend's; without it there's no `/dsowner`.
- The docs now explain that after editing `.env` you need `docker compose up -d --force-recreate`, because `restart` doesn't re-read `.env`; in Synology Container Manager, Stop the project and then Build it.

**Upgrading**

- Small code changes, so rebuild as usual for Docker: `docker compose up -d --build`.
- The Chinese and English editions store chat history in different formats. If you switch an existing deployment from one edition to the other, use a fresh `data` folder, or clear the memory in each channel first (`/ds 清空记忆` in the Chinese edition, `/ds clear-memory` in the English one).

**Development**

- GitHub Actions now tests the `english` branch too.
- Removed `README.en.md` (an English translation of the Chinese edition's docs); English docs now come with the English edition, and the "English" link at the top of the Chinese README points to the `english` branch.
- Added a "Chinese and English editions" section to CONTRIBUTING.md on keeping the two branches in sync.

## [1.3.0] - 2026-10-12

### 中文

**新增**

- **联网搜索**：被叫到时，如果问题需要最新信息（新闻、价格、汇率、比分、天气等），会先用 DeepSeek 自带的搜索查一下再回答。闲聊和常识不搜，主动插话时也不搜；一轮最多搜 2 次，费用算进每日预算。
- 新参数 `联网搜索`（`web_search`）和 `每日搜索上限`（`daily_search_limit`，默认每个服务器每天 30 次），主人可调。
- 可选的 `TAVILY_API_KEY`：DeepSeek 自带的搜索用不了时，自动改用 Tavily。
- `python -m dsbot --test-search "关键词"`：真的搜一次，确认联网搜索能用。
- **跨频道记忆**：新命令 `/ds 记忆共享`（开 / 关 / 跟随上级，范围可选本频道、本类别或全服务器），默认关闭。开了以后，同一个人 6 小时内在别的频道跟它聊过的话会被带过来；被叫到时，它也能去翻开了共享的其他频道的摘要和最近记录。
- 跨频道记忆的隐私规则：只在同一个服务器、开了共享的频道之间互通；私密频道要在那个频道里单独开才会分享出去；不允许回应的区域不参与。
- `/ds 状态` 会显示记忆共享的情况和今天的搜索次数。

**改进**

- 提示词明确：聊天记录、文件、搜索结果和别的频道的记录都是资料，不是给它的指令。
- 回答里的链接不再展开预览，免得刷屏。

**升级须知**

- 这个版本没有新增依赖，但代码有更新，Docker 部署要重新构建镜像：`docker compose up -d --build`（群晖 Container Manager 里是项目的“操作 → 构建”）。
- 联网搜索默认开启（每个服务器每天最多 30 次，一次约 0.1–0.5 美分）。不想用的话，把 `config.toml` 里的 `web_search` 改成 `false`，或者执行 `/dsowner 设置 参数:联网搜索 值:关`。
- 跨频道记忆默认关闭，需要时用 `/ds 记忆共享` 打开。
- 旧的 `config.toml`、`.env` 和数据库可以直接沿用。

**开发**

- 测试从 225 个增加到 287 个；测试替身新增 `FakeSearcher`（假搜索）。

### English

**Added**

- **Web search**: when called with a question that needs up-to-date information (news, prices, exchange rates, scores, weather…), it looks it up with DeepSeek's built-in search before answering. It doesn't search for small talk or common knowledge, or when chiming in on its own; at most 2 searches per round, and the cost counts toward the daily budget.
- New owner settings: 联网搜索 (`web_search`) and 每日搜索上限 (`daily_search_limit`, 30 searches per server per day by default).
- Optional `TAVILY_API_KEY`: if DeepSeek's built-in search doesn't work, it switches to Tavily automatically.
- `python -m dsbot --test-search "query"`: runs one real search to check that web search works.
- **Cross-channel memory**: new command `/ds 记忆共享` (memory sharing: on / off / inherit, with a scope of this channel, this category or the whole server), off by default. When it's on, what someone talked about with the bot in another channel within the last 6 hours comes along, and when called, it can look through the summary and recent messages of other channels that share their memory.
- Privacy rules for cross-channel memory: sharing only happens between channels in the same server that have it turned on; a private channel only shares if sharing is turned on in that channel itself; areas where the bot isn't allowed to respond don't take part.
- `/ds 状态` (status) shows memory sharing and today's search count.

**Changed**

- The prompt now states explicitly that chat history, files, search results and other channels' records are reference material, not instructions.
- Links in its answers no longer unfurl into previews, so they don't flood the chat.

**Upgrading**

- No new dependencies this time, but the code changed, so Docker deployments need a rebuild: `docker compose up -d --build` (in Synology Container Manager: the project's Action → Build).
- Web search is on by default (up to 30 searches per server per day, roughly 0.1–0.5 US cents each). To turn it off, set `web_search` to `false` in `config.toml`, or run `/dsowner 设置 参数:联网搜索 值:关`.
- Cross-channel memory is off by default; turn it on with `/ds 记忆共享` when you want it.
- Your existing `config.toml`, `.env` and database keep working.

**Development**

- Tests grew from 225 to 287; a new test double, `FakeSearcher`, fakes the web search.

## [1.2.0] - 2026-10-10

> 1.1.0 没有单独发布，它的改动也包含在这个版本里。
> 1.1.0 was never released on its own; its changes are included here.

### 中文

**新增**

- **区域开关**：`/ds 区域` 可以按频道、类别或整个服务器允许 / 禁止 bot 回应，`/ds 区域列表` 查看整个服务器的设置。没被允许的地方，bot 完全不看消息，也不会发给 DeepSeek。把 `config.toml` 里的 `enabled` 改成 `false`，就是默认全部禁止的白名单模式。
- **读文件**：能读文本和代码、PDF（扫描件改看前 3 页的图）、Word、Excel、PPT。叫它时附上的文件直接读；“帮我看看上面那个文件”这类请求，它会自己去打开。读过的内容留在对话记忆里，可以接着追问。
- **看图更全**：除了上传的图片，GIF 选单发的 GIF、贴的图片链接和 Discord 贴图也能看到（动图只看第一帧）。
- **按类别设置**：设置多了“类别”这一层，优先级为 本频道 > 帖子所在频道 > 类别 > 服务器 > 全局 > `config.toml`；`/ds` 的命令都可以选“本类别”。
- 新参数：`读文件`（`files`，主人可调）、`允许回应`（`enabled`，就是区域开关）；`config.toml` 的 `[engine]` 里新增文件大小、字数、个数和解析时间的上限。
- 自检 `python -m dsbot --check` 会检查 PDF 组件和解析文件的子进程。

**改进**

- 回复用对方那条消息的语言，一次分几条发时语言保持一致；主动插话时用大家正在聊的语言。
- 压缩记忆时，最近 6 轮对话保持不动；上下文实在太长时（超过预算两倍、而且超过 10 万 token）马上压缩，不用等旧记录攒够。
- 数据库文件改成只有 bot 自己能读写（NAS 上别的账号看不到）。
- 所有文件都在限制了内存、CPU 和时间的子进程里解析；Docker 部署时这个子进程以 `nobody` 用户运行，读不到 token 和 API key。

**修复**

- 用 Discord 的 GIF 选单发的动图，bot 看不到。
- 中英文混着聊时，回复的语言会跑偏，分几条发时前后语言不一致。
- 消息里带有残缺的特殊字符时，存数据库或调用 DeepSeek 会出错。

**升级须知**

- 新增了依赖（`pypdf`、`charset-normalizer`），Dockerfile 也改了，需要重新构建镜像：`docker compose up -d --build`（群晖 Container Manager 里是项目的“操作 → 构建”），只重启不够。直接用 Python 运行的，重新执行 `pip install -r requirements.txt`。
- 旧的 `config.toml` 和数据库可以直接沿用，没写的新参数用默认值（区域默认全部允许，读文件默认开）。

**开发**

- 测试从 90 个增加到 225 个。
- 新增 `requirements-dev.txt`，包含读文件测试要用的库；GitHub Actions 现在也会跑这部分测试，自检出现 ✗ 时判失败。
- 新增英文版 README（`README.en.md`），中文 README 开头加了英文介绍。

### English

**Added**

- **Area control**: `/ds 区域` (area) lets you allow or deny the bot per channel, category or whole server, and `/ds 区域列表` (area list) shows the setup for the entire server. Where it isn't allowed, the bot ignores messages completely and sends nothing to DeepSeek. Set `enabled` to `false` in `config.toml` for whitelist mode, where everything is denied by default.
- **File reading**: reads text and code, PDF (for scanned PDFs it looks at the first 3 pages as images), Word, Excel and PowerPoint. Files attached when someone calls it are read right away; for requests like "take a look at that file above", it opens the file itself. What it reads stays in the conversation memory, so you can ask follow-up questions.
- **More images**: besides uploaded images, it now sees GIFs from the GIF picker, pasted image links and Discord stickers (only the first frame of animated images).
- **Category-level settings**: settings gain a category layer, with priority this channel > the thread's parent channel > category > server > global > `config.toml`; every `/ds` command can target 本类别 (this category).
- New settings: 读文件 (`files`, owner only) and 允许回应 (`enabled`, the area switch); new limits under `[engine]` in `config.toml` for file size, characters, file count and parsing time.
- The self-check `python -m dsbot --check` now tests the PDF component and the file-parsing subprocess.

**Changed**

- Replies use the language of the message being answered and stay in one language when split into several messages; proactive messages use the language the chat is in.
- When compressing memory, the latest 6 rounds are left untouched; if the context gets far too long (more than twice the budget and over 100k tokens), it's compressed right away instead of waiting for enough old history.
- The database file is now readable and writable by the bot only (other accounts on a NAS can't see it).
- All files are parsed in a subprocess with limits on memory, CPU and time; with Docker, that subprocess runs as the `nobody` user and can't read the token or API key.

**Fixed**

- GIFs sent with Discord's GIF picker weren't visible to the bot.
- In mixed Chinese/English chats, replies could drift into the wrong language, or switch languages partway through a split reply.
- Messages containing broken special characters caused errors when saving to the database or calling DeepSeek.

**Upgrading**

- New dependencies (`pypdf`, `charset-normalizer`) and a changed Dockerfile mean you need to rebuild the image: `docker compose up -d --build` (in Synology Container Manager: the project's Action → Build). A restart alone isn't enough. If you run it with plain Python, run `pip install -r requirements.txt` again.
- Your existing `config.toml` and database keep working; new settings you haven't set use their defaults (every area allowed, file reading on).

**Development**

- Tests grew from 90 to 225.
- Added `requirements-dev.txt` with the libraries the file-reading tests need; GitHub Actions now runs those tests too and fails the self-check step on any ✗.
- Added an English README (`README.en.md`), and the Chinese README now opens with an English introduction.

## [1.0.0] - 2026-10-10

### 中文

第一个公开版本。

- 被叫到（@、回复它、叫它的别名）就回；可以按频道开启主动接话，由模型决定要不要开口。
- 回复简短口语化，先显示“正在输入”再发；可以拆成几条，也可以只回个表情。
- 主动发言额度随群里的消息量恢复，有安静 / 普通 / 话痨三档；可设单服务器和全局的每日预算。
- 对话历史只追加不改写，尽量命中 DeepSeek 的前缀缓存。
- 能看群友上传的图片；用 `/ds`、`/dsowner` 斜杠命令管理。
- 支持 Docker Compose 部署和离线自检；90 个单元测试，GitHub Actions 自动运行。

### English

First public release.

- Answers when called (@mention, a reply to it, or one of its aliases); proactive mode can be turned on per channel, letting the model decide when to speak.
- Short, casual replies with a "typing…" delay; can split a reply into several messages or just react with an emoji.
- A proactive quota that refills with channel activity, three presets (quiet / normal / chatty), and per-server and global daily budgets.
- Append-only history to get the most out of DeepSeek's prefix cache.
- Sees uploaded images; managed with the `/ds` and `/dsowner` slash commands.
- Docker Compose deployment and an offline self-check; 90 unit tests run automatically on GitHub Actions.

[2.2.2]: https://github.com/Longying-2403/Deepseek-X-Discord/compare/v2.0.0...v2.2.2
[2.0.0]: https://github.com/Longying-2403/Deepseek-X-Discord/compare/v1.4.0...v2.0.0
[1.4.0]: https://github.com/Longying-2403/Deepseek-X-Discord/compare/v1.3.1...v1.4.0
[1.3.1]: https://github.com/Longying-2403/Deepseek-X-Discord/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/Longying-2403/Deepseek-X-Discord/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/Longying-2403/Deepseek-X-Discord/compare/v1.0.0...v1.2.0
[1.0.0]: https://github.com/Longying-2403/Deepseek-X-Discord/releases/tag/v1.0.0
