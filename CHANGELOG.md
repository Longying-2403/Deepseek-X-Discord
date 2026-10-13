# 更新记录 · Changelog

每个版本的主要变化，中文在前、English below。

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

[1.4.0]: https://github.com/Longying-2403/Deepseek-X-Discord/compare/v1.3.1...v1.4.0
[1.3.1]: https://github.com/Longying-2403/Deepseek-X-Discord/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/Longying-2403/Deepseek-X-Discord/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/Longying-2403/Deepseek-X-Discord/compare/v1.0.0...v1.2.0
[1.0.0]: https://github.com/Longying-2403/Deepseek-X-Discord/releases/tag/v1.0.0
