# 更新记录 · Changelog

每个版本的主要变化，中文在前、English below。

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

[1.3.0]: https://github.com/Longying-2403/Deepseek-X-Discord/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/Longying-2403/Deepseek-X-Discord/compare/v1.0.0...v1.2.0
[1.0.0]: https://github.com/Longying-2403/Deepseek-X-Discord/releases/tag/v1.0.0
