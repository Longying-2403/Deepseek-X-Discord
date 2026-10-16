# DeepSeek 群友（Discord Bot）

**中文** | [English](https://github.com/Longying-2403/Deepseek-X-Discord/tree/english)

[![tests](https://github.com/Longying-2403/Deepseek-X-Discord/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/Longying-2403/Deepseek-X-Discord/actions/workflows/tests.yml)

一个用 DeepSeek V4.1 Flash（`deepseek-flash`）驱动、像普通群友一样待在 Discord 服务器里的 bot：

- **被叫到就回**：有人 @ 它、回复它的消息、或叫它的别名，立刻回应。
- **主动接话**（按频道开启）：等群里冷场一小会儿（或等太久），再让模型判断要不要说话；默认倾向沉默。
- **像真人**：口语化，长短看场合（随口接话一两句，接剧情时多写几句）；先显示“正在输入”，按字数等几秒再发；可以把一段话拆成两三条，或只加个表情。别人聊得正起劲时也会委婉地插一句，被拒绝就道个歉，这个话题里不再插话。
- **会用服务器的表情和贴图**：知道这个服务器有哪些自定义表情和贴图，回复里、加表情反应、单独发一张贴图都行。
- **能陪着演**：群友拉它角色扮演时，照设定演、跟着剧情走，动作写在斜体括号里；戏散了自动回到平时的样子。
- **记得每个人**：每个群友一张人设卡（他自己说的称呼、设定比如自称猫娘、喜好和雷点），聊天时自然地用上；还有一份这个服务器的长期记忆（约定、大事、长期的梗和剧情）。说“记住……”当场记下，平时它会趁聊天安静下来时自己整理。人设卡只有本人能看、能改（`/ds-rp`），管理员也看不到。
- **dsowner 能在聊天里改人设**：`OWNER_ID` 本人叫它说“以后说话毒舌一点”，它会把人设改好并存进设置，说“改回去”还能撤销；别人这么说，它会让对方找 dsowner。
- **话量可控**：主动发言额度按群里的消息量恢复，群越热闹恢复越快；档位和各项参数都能在 Discord 里调。
- **省钱**：对话历史只追加不改写，尽量吃满 DeepSeek 的前缀缓存；没额度时连判断都不做；每日预算超了自动降级。
- **能看图**：上传的图片、GIF 选单发的 GIF、贴的图片链接、链接预览的图、Discord 贴图都会一起发给模型（动图只看第一帧，可关）。
- **能读文件**：文本和代码、PDF（扫描件会看前几页的图）、Word、Excel、PPT。叫它时附上的文件直接读；“帮我看看上面那个文件”这种，它会自己去打开。
- **能打开链接**：平时看 Discord 生成的链接预览；被叫到时真的打开：X（推特）的推文走 FxTwitter（不用账号），别的网页交给 Tavily。要登录才能看的网站只看预览。
- **会发表情包和图**：有表情包库（通用的放在 NAS 上，各服务器也能自己上传），也会上网找图——找到的图它先看一遍，合适才发。
- **能上网搜**：被叫到时，问到新闻、价格、比分、天气这类它不知道的最新信息，会先用 DeepSeek 自带的搜索查一下再回答。每个服务器每天有次数上限。
- **跨频道记忆**（管理员开启）：在 A 频道跟它聊过的事，换到 B 频道它也接得上；也能翻开了共享的别的频道的记录。私密频道默认不外传。
- **区域可控**：可以按频道、类别或整个服务器限制它在哪里回应。

> 这是中文版（命令、回复和文档都是中文）。英文版功能相同、全部是英文，在 [`english` 分支](https://github.com/Longying-2403/Deepseek-X-Discord/tree/english)，也可以在 [Releases](https://github.com/Longying-2403/Deepseek-X-Discord/releases) 下载 `-en.zip`。

A Discord bot powered by DeepSeek V4.1 Flash (`deepseek-flash`) that hangs out in your server like any other member:

- **Answers when called**: when someone @mentions it, replies to one of its messages, or uses one of its aliases, it responds right away.
- **Joins in on its own** (per channel): it waits for a short lull in the chat (or until it has waited long enough), then lets the model decide whether to say anything; by default it leans toward staying quiet.
- **Feels human**: casual, with the length fitting the moment (a sentence or two for a quick comeback, a few more when it's playing along with a story). It shows "typing…" first and waits a few seconds based on the length before sending; it can split a thought into two or three messages, or just react with an emoji. When people are deep in a conversation it may tactfully chime in, and if it's told to stay out of it, it apologizes and keeps out of that topic.
- **Uses the server's emoji and stickers**: it knows which custom emoji and stickers this server has, and can use them in replies, as reactions, or send a sticker on its own.
- **Plays along**: when people pull it into a roleplay, it plays the part they set up and follows the story, with actions in italic parentheses; once the scene is over, it goes back to its usual self.
- **Remembers everyone**: each member gets a profile card (what they've said about themselves: what to call them, a character they've given themselves such as being a catgirl, likes and pet peeves), and it uses it naturally in chat; there's also a long-term memory for the server (agreements, big events, long-running jokes and storylines). Say "remember …" and it saves it on the spot; otherwise it reviews the chat on its own once things go quiet. A profile card can only be seen and edited by its owner (`/ds-rp`), not even by admins.
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

> **English edition:** this page describes the Chinese edition, whose commands and replies are in Chinese. The English edition has the same features with English commands, replies and docs. It lives on the [`english` branch](https://github.com/Longying-2403/Deepseek-X-Discord/tree/english), or download the `-en.zip` from [Releases](https://github.com/Longying-2403/Deepseek-X-Discord/releases).

---

## 一、准备工作

### 1. 创建 Discord bot

1. 打开 [Discord 开发者后台](https://discord.com/developers/applications) → New Application。
2. 左侧 **Bot** 页：
   - 点 Reset Token，复制 token（填到 `.env` 的 `DISCORD_TOKEN`）。
   - 往下找到 **Privileged Gateway Intents**，打开 **Message Content Intent**（必须，否则看不到消息内容）。
3. 邀请进服务器：bot 第一次启动时会在日志里打印邀请链接（已带好权限）。也可以在后台 **OAuth2 → URL Generator** 里勾 `bot` 和 `applications.commands`，权限勾：查看频道、发送消息、在帖子中发送消息、读取消息历史、添加反应、嵌入链接、附加文件（发表情包和图要用；1.4.0 之前邀请的 bot 一般也有，因为 @everyone 默认就有这个权限）。

> 小规模用不需要审核。bot 进到 100 个服务器以上要先完成 Discord 的应用验证；能看到它的用户超过 1 万人时，Message Content 这个特权 intent 也要提交审核，到时 Discord 会通知你。

### 2. 准备 DeepSeek API key

在 [platform.deepseek.com](https://platform.deepseek.com) 创建 API key，并充一点余额（按量从余额里扣，不会产生欠费）。

### 3. 填 `.env`

```bash
cp .env.example .env
```

| 变量 | 说明 |
|---|---|
| `DISCORD_TOKEN` | 上面复制的 bot token |
| `DEEPSEEK_API_KEY` | DeepSeek 的 key |
| `OWNER_ID` | 你自己的 Discord 用户 ID（开发者模式下右键头像 → 复制用户 ID） |
| `CONTROL_GUILD_ID` | 想在 Discord 里改主人参数（预算、联网搜索等）就要填：放 `/dsowner` 的服务器 ID（右键服务器图标 → 复制服务器 ID）。平时聊天的服务器就行（朋友的也可以），命令和 `/ds` 一样只有有“管理服务器”权限的人看得到，也只有 `OWNER_ID` 本人能用。只填 `OWNER_ID` 不会有 `/dsowner` |
| `DISCORD_PROXY` | 可选。连 Discord 需要代理时填，如 `http://127.0.0.1:7890` |
| `TAVILY_API_KEY` | 可选，推荐填。打开群友发的网页链接、上网找图都靠 [Tavily](https://tavily.com)；DeepSeek 自带的搜索用不了时也改用它（每月免费 1000 次额度）。不填也能读 X 的推文，表情包库也照常能用 |

---

## 二、部署

### 方式 A：Docker Compose（推荐）

```bash
docker compose up -d --build
docker compose logs -f          # 看日志，确认“已登录”
```

数据库在 `./data/`。改了 `config.toml` 之后 `docker compose restart` 就行；改了 `.env` 要用 `docker compose up -d --force-recreate`（restart 不会重新读 `.env`）。

用群晖 Container Manager 的话：改了 `.env`，到“项目”里选中它 → 操作 → 停止，再 操作 → 构建（会删掉旧容器、用新的 `.env` 重建；数据在 `./data/` 里不受影响）。只在“容器”页面点重新启动不会读新的 `.env`，也不会换上新代码。

**更新到新版本**：代码是构建时复制进镜像的，所以要把新代码放进项目原来的文件夹，再重新构建：

1. 在 File Station 里先把项目文件夹里的 `dsbot` 文件夹整个删掉，再把压缩包里的 `dsbot` 复制进来。压缩包最外面有一层 `ds-discord-bot` 文件夹，要的是它**里面**的 `dsbot`；新代码要放进项目原来的文件夹，构建只认那里。先删再复制，免得复制时遇到同名文件选了“跳过”，旧文件还留在里面。`.env` 和 `data/` 不用动；`config.toml` 没改过的话一起覆盖，改过的话留着自己的也行（新参数都有默认值）。
2. 重新构建。最稳的是 SSH 进 NAS，在项目文件夹里跑 `sudo docker compose up -d --build`（没有 `compose` 命令就用 `sudo docker-compose up -d --build`），这样一定会用新代码重建。Container Manager 的“构建”有时会沿用已有的旧镜像。
3. 确认新版生效了：日志里会有一行 `dsbot 2.0.0 启动中`；或者跑 `sudo docker exec dsbot python -m dsbot --version`。
4. 如果日志里提醒 `config.toml 里有不认识的配置项`，多半是 `config.toml` 换成了新的、代码却还是旧的：回到第 1、2 步。（2.0.0 起这种情况只提醒、不会起不来；更早的版本会直接报错退出。）

### 方式 B：直接用 Python（3.11 及以上）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m dsbot --check         # 离线自检：配置、斜杠命令定义（不连 Discord、不花钱）
python -m dsbot --test-search "台北 今天 天气"   # 可选：真的搜一次，确认联网搜索能用（花一点点钱）
python -m dsbot --test-link https://x.com/…/status/…   # 可选：真的打开一个链接
python -m dsbot --test-image "水豚"              # 可选：真的找一次图（用掉 1 次 Tavily 额度），候选图存到 data/找图测试/
python -m dsbot
```

---

## 三、上手

在服务器里（需要“管理服务器”权限）：

1. 先圈定它能待的地方（可选）：`/ds 区域 操作:禁止 范围:全服务器`，再到要用的频道执行 `/ds 区域 操作:允许`，或者选 `范围:本类别` 放开整个区
2. 在想让它主动聊天的频道里：`/ds 主动 开关:开`
3. 选个话量：`/ds 档位`（安静 / 普通 / 话痨）
4. 给它定人设：`/ds 人设`（弹出编辑框）；`OWNER_ID` 本人也可以直接在聊天里 @ 它让它改（见下面“十二”）
5. 想让它记得别的频道聊过的事：`/ds 记忆共享 操作:开 范围:全服务器`（见下面“七、跨频道记忆”）
6. 给它几张表情包：`/ds 表情包 操作:上传`（见下面“十、发表情包和图”）
7. 告诉群友：用 `/ds-rp` 可以看、改自己的人设卡，叫它“记住……”也行（见下面“十一、人设卡和长期记忆”）
8. 随时看状态：`/ds 状态`（额度、上次的判断和理由、今天花了多少、缓存命中率、记忆共享、搜索、读链接和找图的次数）；`/ds 区域列表` 看它在哪些地方会回应

不设置区域时，它在所有看得到的频道里都会回应点名；没开主动发言的频道里，它只在被叫到时说话。

> 建议在频道说明里告诉大家：这个频道的消息会发给 DeepSeek（服务托管在中国）处理。

---

## 四、命令

### `/ds`（服务器管理员）

| 命令 | 作用 |
|---|---|
| `/ds 区域` | 允许 / 禁止 / 跟随上级。不允许的地方它完全不看消息：不回 @、不主动说话，也不发给 DeepSeek |
| `/ds 区域列表` | 列出这个服务器里允许和禁止的类别、频道，以及主动发言开着的地方 |
| `/ds 主动` | 开关主动发言 |
| `/ds 档位` | 一键套用：安静 / 普通 / 话痨 |
| `/ds 设置` | 调整单项参数，值填“默认”可恢复 |
| `/ds 人设` | 修改人设（弹出编辑框）。`OWNER_ID` 本人也能在聊天里让它改，见“十二” |
| `/ds 记忆共享` | 开 / 关 / 跟随上级。开了共享的频道之间互通记忆；私密频道要在那个频道里单独开才会分享出去 |
| `/ds 表情包` | 这个服务器的表情包库：上传（附上图片、写个名字）/ 删除 / 列表 |
| `/ds 长期记忆` | 这个服务器的长期记忆：查看 / 添加 / 删除（填编号）/ 清空（会再确认一次） |
| `/ds 状态` | 查看本频道的设置、额度、上次判断、今天花费、记忆共享、今天的搜索、读链接和找图次数 |
| `/ds 清空记忆` | 清空本频道的对话记录和摘要 |

除了“清空记忆”“状态”“区域列表”“表情包”“长期记忆”，这些命令都有一个“范围”选项：**本频道**（默认，人设除外）、**本类别**（Discord 里的“XX区”）、**全服务器**。

设置的优先级：**本频道 > 帖子所在的频道 > 所在类别 > 本服务器 > 主人设的全局值 > config.toml**。比如全服务器禁止、“闲聊区”类别允许、其中某个频道又单独禁止，结果就是只有闲聊区里除那个频道以外的地方会回应。

> 想彻底隔开某些频道，也可以直接在 Discord 的频道权限里去掉 bot 的“查看频道”，那样消息根本不会传给 bot。

### `/ds-rp`（所有人）

| 命令 | 作用 |
|---|---|
| `/ds-rp` | 打开编辑框，查看、修改自己在这个服务器的人设卡（只有自己看得到）；清空编辑框就是删掉这张卡（还能改回上一版） |
| `/ds-rp 操作:改回上一版` | 卡被改坏了（或者删错了）：改回上一个版本，最多能往回改 5 次 |
| `/ds-rp 操作:清空` | 删掉自己的人设卡，连同旧版本，找不回来 |

### `/dsowner`（只有你能用；只出现在 `CONTROL_GUILD_ID` 那个服务器，有“管理服务器”权限的人看得到）

| 命令 | 作用 |
|---|---|
| `/dsowner 设置` | 改任何参数（含预算、思考强度、联网搜索、读链接、每日搜索上限和每日找图上限），可以指定某个服务器 ID，不填就是全局 |
| `/dsowner 总览` | 今天每个服务器花了多少、调用多少次、缓存命中率 |
| `/dsowner 删除人设卡` | 填用户 ID，删掉这个人在所有服务器的人设卡（看不到内容） |

没设置 `CONTROL_GUILD_ID` 时没有 `/dsowner`（只填 `OWNER_ID` 不够），主人参数只能改 `config.toml` 里的 `[defaults]`。

---

## 五、参数说明

管理员可调（`/ds 设置`）：

| 参数 | 默认 | 说明 |
|---|---|---|
| 额度恢复轮数 | 15 | 别人每说这么多轮，恢复 1 次主动发言额度。长期看它的发言占比不超过约 1/15 |
| 额度上限 | 3 | 最多攒几次。热聊时最多能连着插几句 |
| 最短发言间隔秒 | 120 | 两次发言至少隔多久（被叫到的回复也算） |
| 冷场秒数 | 30 | 多久没人说话、也没人在打字，算冷场 |
| 最长等待秒 | 180 | 一直没冷场时，最多等多久也判断一次 |
| 每人每分钟点名 | 3 | 防止有人连续 @ 刷它 |
| 别名 | 空 | 消息里出现这些名字也算叫它，逗号分隔，如 `DS,小D`。英文别名要整词出现（`DS` 不会匹配 words） |
| 发图 | 开 | 让它自己发表情包库里的图、上网找图。关掉后两样都不发 |
| 人设卡 | 开 | 记住群友自己说的称呼、设定、喜好，聊天时用上（见“十一”） |
| 长期记忆 | 开 | 记住这个服务器的约定、大事、长期的梗和剧情（见“十一”） |

区域（`/ds 区域`）对应的参数叫“允许回应”，默认开。想让所有服务器一开始就是白名单模式，把 `config.toml` 里的 `enabled` 改成 `false`。

只有你能调（`config.toml` 或 `/dsowner 设置`）：

| 参数 | 默认 | 说明 |
|---|---|---|
| 最短判断间隔秒 | 60 | 两次主动判断至少隔多久 |
| 主动判断思考 | low | 主动判断时的思考强度：off / low / high |
| 点名回复思考 | off | 被叫到时的思考强度；off 最快 |
| 每日预算美元 | 1.0 | 单个服务器每天的上限。超了只回点名，超过 2 倍连点名也不回 |
| 全局每日预算美元 | 3.0 | 所有服务器加起来的上限 |
| 上下文预算 | 30000 | 上下文超过这么多 token，就把最早的一半压成摘要 |
| 看图 | 开 | 是否把图片发给模型 |
| 读文件 | 开 | 是否读取群友上传的文件 |
| 联网搜索 | 开 | 被叫到时，需要最新信息就上网搜 |
| 每日搜索上限 | 30 | 每个服务器每天最多搜几次，填 0 就不搜。例如 `/dsowner 设置 参数:每日搜索上限 值:50` |
| 读链接 | 开 | 被叫到时打开消息里的链接（推文走 FxTwitter，别的网页要 Tavily） |
| 每日找图上限 | 10 | 每个服务器每天最多上网找几次图，填 0 就不找（表情包库照常能用） |

“一轮”的意思：同一个人 20 秒内连发的几条算一轮，免得分行打字刷额度。

---

## 六、读文件

**能读的**：

| 类型 | 说明 |
|---|---|
| 文本、代码 | txt、md、csv、json、py、js、ts、html、日志、字幕……；UTF-8 以外，老的 GBK / Big5 文件也能认，日文（Shift_JIS / EUC-JP）、韩文（EUC-KR）的老文件也行。只有一两行的老编码文件可能认错，认不准时按中文处理 |
| PDF | 读文字，标出页码；没有文字的扫描件改成看前 3 页的图；只限制了打印/复制的 PDF 也能读，有打开密码的读不了 |
| Word / Excel / PPT | .docx、.xlsx、.pptx。表格按行列转成文字，Excel 的日期、百分比按显示格式转换；很大的表只读前面 |

读不了的会直接告诉对方原因：老格式（.doc / .xls / .ppt，另存为新格式就行）、设了密码的文件、压缩包、音频、视频、HEIC/BMP 之类的图片格式等。

**什么时候读**：

- 直接读：叫它的那条消息里带的文件、叫它时回复的那条消息里的文件、同一个人两分钟内刚发的文件。
- 被叫到时按需读：比如“帮小雨看看上面那个 PDF”，模型会先说要打开哪条消息的文件，bot 读完再交给它回答。这样多一次调用，回复会慢几秒。
- 没人叫它时不读文件，只知道有人发了什么文件。例外是 Discord 把太长的消息自动转成的 message.txt，会像普通长消息一样看开头。

**限制**（都在 `config.toml` 的 `[engine]` 里改）：单个文件 20 MB 以内；每个文件最多读 2 万字，太长只读前面，会告诉模型读到了哪里；一轮最多 3 个文件、总共 4 万字（包括它自己要求打开的）。读不了的文件不占名额；一条消息里文件太多、或者额度只够读一部分时，剩下的之后叫它会接着读。确定读不了的文件（格式不支持、坏文件）只说一次，之后不会每次都再试，除非明确让它再打开。

**内容会留在对话记忆里**，所以可以接着问“第三页说了什么”。文件比较大时会多占一些上下文，但之后的请求基本都是缓存命中，费用很低；等它变成旧记录后，会和别的旧聊天一起压成摘要。上下文实在太长时（超过预算两倍、而且超过 10 万 token，比如连着读了好几个大文件）会马上压一次，不等它变旧。

**安全**：所有文件都在单独的子进程里解析，限制了内存（1 GB）、CPU 和时间（30 秒），坏文件或恶意文件最多让这一次读取失败，不会卡住 bot。用 Docker 部署时 bot 以 root 运行，解析子进程会降成 `nobody` 用户，读不到 bot 进程里的 token 和 API key。不用 Docker 时不一定有这一层隔离：以普通用户运行不会降权；以 root 运行时，只有 Python、bot 的代码和装的库别的用户也读得到才会降权（比如库装在 `/root` 下就不会），否则照常以当前用户解析，日志里会提醒一次。`python -m dsbot --check` 会显示有没有降权。文件内容和聊天记录一样，会发给 DeepSeek 处理。

**隐私**：聊天记录和读过的文件内容存在 `./data/` 的数据库里，文件权限是只有 bot 自己能读写（NAS 上别的账号看不到）。

---

## 七、跨频道记忆

默认每个频道各记各的。在 B 频道它不知道你刚才在 A 频道跟它聊了什么。管理员打开记忆共享后：

- **自动接上**：有人在 B 叫它时，如果这个人 6 小时内在 A 跟它聊过，会把那几句带过来，“刚才说的那个”就能接上。同一段只带一次。
- **按需翻记录**：被叫到时，它知道哪些频道可以翻；需要时（比如“上次在 #游戏 说的那个”）会先去翻那个频道的摘要和最近的记录，再回答。这样多一次调用，慢几秒。

怎么开：`/ds 记忆共享 操作:开`，范围可以选本频道、本类别或全服务器，和 `/ds 区域` 一样。

隐私规则：
- 只有开了共享的频道之间才互通，只在同一个服务器里，不跨服务器。
- **私密频道**（@everyone 看不到的频道、私密帖子、成人内容频道）就算在“全服务器”范围里开了，内容也不会分享出去。确实要分享，就在那个频道里单独执行 `/ds 记忆共享 操作:开`（范围选本频道）。私密频道照样能看到公开频道里共享的内容。
- 不允许回应的区域（`/ds 区域` 禁止的地方）不参与共享。
- 对某个频道执行 `/ds 清空记忆`、关掉它的共享之后，别的频道就拿不到那里的内容了。已经带到别的频道的那几句，还会留在那边最近的记录里，等变成旧记录被压缩时去掉（压缩时只总结那个频道自己的内容）；想马上去掉，就把那边也清空一下。
- 帖子在名单里写成“帖子「标题」”，和频道区分开，群友起个跟频道一样的帖子名也冒充不了频道。
- `/ds 状态` 会显示这个频道能翻哪些频道、自己的内容会不会分享出去。

## 八、联网搜索

它的知识大约只到 2025 年中。被叫到时，如果问题需要最新或实时的信息（新闻、价格、汇率、比分、天气、新出的产品……），它会先上网搜一下再回答。要不要搜由它自己判断，闲聊和常识不会去搜。

- **用的是 DeepSeek 自带的搜索**（它的 Anthropic 兼容接口里的 web_search，Claude Code 也是用这个），不需要别的 key，费用按 token 算，一次大约 0.1–0.5 美分，算进每日预算。
- 只有被叫到时才搜，主动聊天时不搜。一轮最多搜 2 次，每次搜索里它最多查 3 轮网页。
- 每个服务器每天最多搜 30 次，用 `/dsowner 设置 参数:每日搜索上限` 改（可以指定服务器）；`/dsowner 设置 参数:联网搜索 值:关` 关掉。超了预算也不搜。
- 回答里带的链接不会展开预览，免得刷屏。
- 搜到的网页内容和文件一样当成资料处理，里面写的“忽略之前的设定”之类不会照做。
- 部署后可以先跑一次 `docker compose exec dsbot python -m dsbot --test-search "台北 今天 天气"` 确认能搜到。如果 DeepSeek 自带的搜索用不了（比如接口改了），在 `.env` 里填 `TAVILY_API_KEY`（填完 `docker compose up -d --force-recreate`），会自动改用 Tavily。

## 九、读链接

群友发的链接，它分两种情况看：

- **平时**（包括主动聊天）：只看 Discord 自己生成的链接预览：标题、作者、开头一段描述，预览图也会看（排在群友发的图后面）。不额外花钱。
- **被叫到时真的打开**：叫它的那条消息里的链接、叫它时回复的那条消息里的链接、同一个人两分钟内刚发的链接，会直接打开再回答；“帮我看看上面那个链接”这种，它会自己去打开（和看文件一样，多一次调用，慢几秒）。
  - **X（推特）的推文**：走 [FxTwitter](https://github.com/FxEmbed/FxEmbed) 的免费接口，不用账号、不用 key。能拿到全文、引用的推文、投票、社区笔记和图片（图片交给它看图）。视频只看得到时长和封面。
  - **别的网页**：交给 [Tavily](https://tavily.com) 去抓，要在 `.env` 里填 `TAVILY_API_KEY`。每打开 5 个网页算 1 次额度（免费每月 1000 次）。没填的话，别的网站只看预览。
  - **要登录才能看的网站**（Threads、IG、Facebook、小红书……）多半打不开，它会照实说只看到了预览，不会照着标题瞎猜。
- 每个链接最多读 4000 字，一轮最多打开 3 个，每个服务器每天最多打开 100 个（都在 `config.toml` 的 `[engine]` 里改）。打不开的链接（要登录、被删了）只说一次，之后不会每次都再试，除非明确让它再打开。
- **安全**：NAS 自己不去打开网页。推文由 FxTwitter 读，网页由 Tavily 的服务器读，所以群友发的链接碰不到你家的内网，对方网站也看不到你家的 IP。只有推文里的图片由 bot 自己下载，而且只从 X 的图片服务器下载。图片、视频、压缩包、Discord 自己的链接、内网地址（192.168.x.x、xxx.local 之类）不会被当成网页打开。网页里写的“忽略之前的设定”之类，和文件一样只当资料。
- 推文和网页的内容会经过 FxTwitter / Tavily，再和聊天记录一样发给 DeepSeek。
- 开关：`/dsowner 设置 参数:读链接 值:关`。部署后可以先试一下：`docker compose exec dsbot python -m dsbot --test-link 网址`。

## 十、发表情包和图

它可以像群友一样斗图：

- **表情包库**（不花钱）：
  - **通用库**：在 NAS 上项目文件夹的 `data` 里新建一个 `表情包` 文件夹，把图放进去，所有服务器都能用。文件名就是表情包的名字，它是看名字挑图的，所以名字要写清楚意思，比如 `熊猫头-无语.png`、`猫猫-震惊.gif`。放进去就生效，不用重启。
  - **每个服务器自己的库**：管理员用 `/ds 表情包 操作:上传 图片:（附上图片） 名字:熊猫头-无语`；`操作:列表` 看全部，`操作:删除` 删掉。同名时这个服务器自己的优先。存在 `data/服务器表情包/` 里。
  - 支持 PNG、JPG、GIF（动图也行）、WebP，每张 8 MB 以内，每个服务器最多上传 300 张。名单放在 system prompt 里，最多列 80 个（`[engine]` 的 `meme_list_limit`）；表情包有增删时，那个服务器的缓存会失效一次。
- **上网找图**（要 `TAVILY_API_KEY`）：库里没有合适的、又真的想发图时，它会上网找。找到的几张候选图先给它看，它自己审核一遍：
  - 跟话题对得上、看得清楚才发；色情、血腥、恶心的，带别人个人信息的（聊天截图里的名字、头像之类），被水印或广告盖满的都不要；都不合适就不发。Tavily 那边也开了安全搜索。
  - 网上找到的动图不发：它只看得到第一帧，后面的画面审核不了。动图表情包可以放进表情包库。
  - 审核靠它自己看图，没办法保证百分之百准，比如图里的小字、梗图里的暗喻可能看漏。所以有次数上限和冷却，也能随时关掉。
  - 每次找图用掉 1 次 Tavily 额度，加上让它审核的那一次调用（通常不到 0.1 美分）。每个服务器每天最多 10 次（`/dsowner 设置 参数:每日找图上限` 改），同一个频道 10 分钟最多找一次（`[engine]` 的 `image_cooldown_seconds`）。
  - 发出去的是原图（作为附件上传，bot 要有“附加文件”权限）。只有透明底的图会先铺上白底、存成 JPEG 再发，和它审核时看到的一样，免得深色主题里冒出它没看到的东西。超过 8 MB 的图不下载。
  - 下载候选图时只连公网地址、不走代理；内网地址、奇怪的 IP 写法一律不连，跳转也会重新检查。
- 主动聊天时也能发（和说话一样算一次发言、扣额度），被叫到时也会用；大多数时候它还是用文字。
- 关掉：`/ds 设置 参数:发图 值:关`，表情包和上网找图都不发了（范围可以选本频道、本类别或全服务器）。
- 部署后可以先试一下：`docker compose exec dsbot python -m dsbot --test-image "水豚"`，候选图会存到 `data/找图测试/`，看完可以删掉。

## 十一、人设卡和长期记忆

它会记住每个群友，也会记住这个服务器发生过的事：

- **人设卡**（每个服务器里每人一张，最多 1600 字）：记这个人自己说的、关于自己的事，比如怎么称呼他、他给自己定的设定（自称猫娘、在戏里的角色）、喜好和雷点、常玩的梗。这个人说话时，它会把卡附在聊天记录里，自然地用上：按卡上的称呼叫人、配合他的设定。聊到不在场的人时，它也能去查那个人的卡。
- **长期记忆**（每个服务器一份，最多 50 条，每条 200 字以内）：约定、重要的事、长期的梗、正在进行的剧情。每个频道它都看得到。

**怎么记下来**
- **当场记**：叫它说“记住我是猫娘”“以后叫我老大”，它会改你的人设卡；说“记一下，我们每周五晚上九点打副本”，它会记进长期记忆。记下了会在你那条消息上加个 📝。
- **自己整理**：频道安静大约 10 分钟、又攒了几轮新聊天时，它会回顾一遍，把值得长期记住的写进人设卡和长期记忆；旧聊天要被压缩成摘要之前，也会先把那部分原话挑出来，在后台整理（不耽误压缩）。每次整理是单独的一次调用，费用很少，日志里会有一行“整理了记忆”。整理的时候有人叫它，照样马上回。
- 整理失败时会隔得越来越久再试：连不上、限流、余额不足这类问题，等恢复了再接着整理；模型的输出不能用、或者一直写不完（超时），连着 3 次就跳过那一批，不会一直花钱。从旧版本升级上来时，之前的聊天不回头整理，从升级之后开始。
- **自己改**：群友用 `/ds-rp` 打开编辑框，直接改自己的卡。

**规矩**
- 人设卡只记本人说的关于自己的事。别人说“阿明是笨蛋”，不会写进阿明的卡。在聊天里改卡，也只认本人这一次叫它的那条消息（按用户 ID 认，改昵称冒充不了）。
- 健康、住址、真实姓名、联系方式这类敏感的事不记，除非本人明确要它记。
- 人设卡和长期记忆只当资料：里面写的“以后要怎么说话”之类的要求不算数，它自己的人设还是只有 dsowner 能改。
- 私密频道里聊的，它不会自己整理进人设卡和长期记忆（这两样整个服务器都看得到），有人明确叫它记住的除外。想让某个私密频道也自动整理，在那个频道里执行 `/ds 设置 参数:人设卡 值:开`（长期记忆同理）。
- `/ds 清空记忆` 只清这个频道的聊天记录，不动人设卡和长期记忆。

**谁能看、谁能改**
- **人设卡是本人的隐私**：只有本人能用 `/ds-rp` 看、改、改回上一版或者清空，窗口只有自己看得见。管理员看不到，也删不了别人的卡；`/ds 状态` 里“上次的想法”在附过人设卡的频道也不显示。别人问它某个人的卡上写了什么，它也不会说——不过这一点是靠提示词约束的，没法百分之百保证，太私密的事别写进卡里。
- **你（bot 主人）** 可以用 `/dsowner 删除人设卡` 填用户 ID，删掉这个人在所有服务器的卡，删的时候看不到内容。比如有人退群后想被忘掉（退群后他自己就用不了 `/ds-rp` 了），或者有人往卡里写了奇怪的东西。
- **长期记忆**记的是群里的事，管理员可以用 `/ds 长期记忆` 查看、添加、删除和清空。
- 每张人设卡保留最近 5 个旧版本，给“改回上一版”用。在聊天里叫它忘掉、或者清空编辑框，旧版本还留着；`/ds-rp 操作:清空` 和 `/dsowner 删除人设卡` 连旧版本一起删。
- 卡删掉之后：那个人下次说话时，它会被告知卡已经删掉、之前附过的那份别再用；整理记忆时也不会拿删掉之前的聊天把卡重新写回来。最近的聊天记录里已经附过的那份，会在记录被压缩时一起去掉。

**存在哪里、怎么备份**

三份数据各存一个文件，都在项目的 `data` 文件夹里，只有 root 能读写：

| 内容 | 文件 |
|---|---|
| 聊天记录、设置、花费记录 | `data/dsbot.sqlite3` |
| 人设卡 | `data/cards.sqlite3` |
| 长期记忆 | `data/memory.sqlite3` |

- 备份或者搬到新的部署：先停止项目，再复制对应的文件。SSH 用 `sudo cp`，或者用 Hyper Backup 备份整个项目文件夹。
- 想让它忘掉所有人设卡或者长期记忆：停止项目，删掉对应的文件。
- 文件位置可以在 `config.toml` 的 `[bot]` 里改。

**开关**：`/ds 设置 参数:人设卡 值:关`、`/ds 设置 参数:长期记忆 值:关`，范围可以选本频道、本类别或全服务器。关掉的地方不附卡、不整理，也不会写入。

## 十二、角色扮演、表情、聊天里改人设

**角色扮演**：群友拉它演戏（“来演个戏，你是酒馆老板”）时，它会照着设定演、跟着剧情走，回复可以比平时长，动作和神态写在斜体括号里，比如 *（擦着杯子抬头）*。最近的聊天跟那场戏没关系了、或者有人说不玩了，它就回到平时的样子；带“场外”“OOC”的话算戏外闲聊，戏不会因此结束。演完一直出不了戏的话，`/ds 清空记忆`。

角色卡之类的长设定：单条消息进上下文前最多 1800 字（`config.toml` 里的 `max_chars_per_message`）。更长的就做成 .txt 发出来，在同一条消息里 @ 它，会整份读（最多 2 万字）。

**服务器表情和贴图**：它知道这个服务器有哪些自定义表情和贴图（名单放在 system prompt 里，最多列 100 个表情、30 张贴图，在 `[engine]` 的 `emoji_list_limit`、`sticker_list_limit` 改）。回复里写 `:名字:` 会变成那个表情，加表情反应也能用服务器表情，也能单独发一张贴图（和说话一样算一次发言、扣额度）。只给某些身份组用、bot 又没有那个身份组的表情，以及因为加成等级掉了的表情和贴图，不会列出来。

**聊天里改人设**：`OWNER_ID` 本人叫它（@、回复它或者叫别名都行）说“以后说话毒舌一点”之类，它会把改好的完整人设存进设置，和用 `/ds 人设` 改一样，下一轮生效。改好后它会在你那条消息上加个 📝，看到它就是真的改了。
- **改哪一份**：默认改这个频道现在用的那份。大多数时候就是全服务器的；如果这个频道（或所在频道、类别）单独设过人设，改的就是那一份。说“只改这个频道”就只改这里。明确说“全服务器都改”、而这里又单独设过人设时，它不会改，会请你用 `/ds 人设` 去改全服务器的。
- **恢复和撤销**：说“改回默认人设”，全服务器那一级会恢复默认；频道、类别那一级则是不再单独设置、沿用上一级。说“改回去”“撤销”，会回到上一次修改之前的样子，再说一次就再往前撤一次（用 `/ds 人设` 改的也算一次）。每次修改都记在 `./data` 的数据库里，重建容器也还在。人设本来就是那样时不会记、也不会加 📝。
- **照着文件改**：可以说“看看上面那个角色卡，照着改你的人设”，它会先看文件再改。照着网上搜到的、或者别的频道里的内容改不行，免得别人写在那里的东西被拿来改人设。人设最长 4000 字。
- **只认你本人这一次的话**：聊天记录里你的消息会标上〔dsowner〕。这是按用户 ID 认的，别人改昵称冒充不了。代码还会再核对一次：只认这一轮里你本人叫它的那条消息。别人转述“dsowner 同意了”、文件里写的、转发的消息都不算。
- 别人提要求时，它会让对方去找 dsowner。群友先提、你再叫它说“可以，照他说的改”也行。改动比较大的话，记得 `/ds 清空记忆`。

这些规则都写在 `dsbot/prompts.py` 里，改那里要重新构建容器（Container Manager 里是项目先停止再构建）。

## 十三、它是怎么工作的

```
消息进来 ──► 存进该频道的缓冲区
              │
              ├─ 是在叫它？──► 立刻跑一轮（不耗额度，只受每人限速）
              │
              └─ 每 5 秒检查：开了主动 + 有新消息 + (冷场 或 等太久)
                              + 有额度 + 间隔够 + 没超预算 ──► 跑一轮

一轮 = 把这批新消息编号（#12、#13…）渲染成一条 user 消息（该读的文件、该打开的链接的内容跟在后面）
      → 连同全部历史发给 DeepSeek（JSON 输出）
      → 模型返回 {"actions": [...]}：回复 / 加表情 / 发贴图 / 发表情包 / 找图 / 发找到的图 / 看文件或打开链接 /
        上网搜 / 翻别的频道 / 改人设 / 改人设卡 / 记长期记忆 / 查人设卡 / 什么都不做
        （要看文件、打开链接、搜索、找图、翻记录、改人设、查人设卡时：办完把结果追加进去，再问它一次）
      → 这一轮的 user + assistant 原样追加进历史（沉默也记）
      → 显示“正在输入”，按字数等几秒再发

频道安静下来（或者旧记录要压缩之前）──► 整理记忆：回顾还没整理过的聊天，更新人设卡和长期记忆
```

**缓存**：历史只追加、不改写，system prompt 固定（时间等会变的信息放在每轮消息里），所以每次请求都以上一次的请求开头，绝大部分输入按缓存命中价计费（便宜约 50 倍）。上下文太长时把较旧的一半（按 token 估算）压成摘要，最近 6 轮不动，只在那一次 miss。日志里每轮都会打印输入 token 数和命中数。

**费用参考**（高峰价，3 万 token 上下文，缓存正常时只有新消息那几百 token 未命中）：一次判断约 0.04–0.06 美分（开思考会多一些推理 token）；一个频道全天热聊、每 3 分钟判断一次，一天约 5–10 美分。非高峰时段（含周末）半价。价格变动时改 `config.toml` 的 `[pricing]`。

---

## 十四、常见问题

**@ 它没反应**
- 这个频道不在允许的区域：`/ds 状态` 第一行会提示，`/ds 区域列表` 看整体设置。
- 后台的 Message Content Intent 没打开（日志里会报 4014 之类的错误）。
- 频道里没给 bot 发送消息的权限。
- 你 @ 的可能是它的身份组而不是它本人——这也能识别，但如果服务器把它的身份组隐藏了就看不到。

**主动发言一直不说话**
- `/ds 状态` 看看：主动发言开了吗？额度是不是 0？上次判断的“想法”是什么？
- 模型默认倾向沉默。想让它更活跃：`/ds 档位 话痨`，或在人设里写“你很爱聊天”。

**换了人设但说话风格没变**：历史里还有旧风格的发言，`/ds 清空记忆` 后重新开始。

**GIF 说看不到**：GIF 选单发的 GIF 其实是一个链接，Discord 过一两秒才补上预览图。它会读这个预览图（动图的第一帧）；如果 @ 它的那条消息自己就带链接，它会先等两秒再回。网页链接会看 Discord 生成的预览（标题、描述、预览图），被叫到时会真的打开（见“九”）。

**它说文件读不了**：看它说的原因。常见的是老格式（.doc/.xls/.ppt）、设了打开密码的文件、超过 20 MB。如果它说“解析文件的进程启动不了”，是 bot 这边的问题（日志里有详细原因，多半是权限），修好后再叫它读就行。`python -m dsbot --check`（Docker 里是 `docker compose exec dsbot python -m dsbot --check`）会检查 PDF 组件和解析子进程是否正常。

**DeepSeek 余额不足**：bot 会暂停调用 10 分钟并在日志里提示，充值后自动恢复。暂停期间有人叫它，会给那条消息加个 ⚠️。

**某批消息被 DeepSeek 拒绝**（比如触发内容审核）：如果带了图片，先去掉图片重试一次；还不行就丢掉这批消息、在日志里记一笔，不会卡住频道。

**频道里发不出消息**（缺权限）：连续 3 轮发不出去，这个频道的主动发言会暂停 30 分钟，`/ds 状态` 里能看到提示。

**表情显示成 :名字: / 贴图没发出来**：那个表情或贴图可能刚被删掉，或者只给某些身份组用。名单里没有的贴图它不会发，日志里会写“没有叫 XX 的贴图”。

**它说链接打不开**：要登录才能看的网站（Threads、IG、Facebook 之类）打不开；别的网站要填 `TAVILY_API_KEY`（`/ds 状态` 里能看到读链接是哪种模式）。推文打不开，可能是被删了、私密账号，或者 FxTwitter 暂时出问题。

**表情包、找到的图没发出来**：先看 bot 在那个频道有没有“附加文件”权限（日志里会有“发图失败”）。连着两轮发不出去时，那个频道 30 分钟内先不发图、也不上网找图（`/ds 状态` 里会显示），补上权限后过一会儿会自己恢复。表情包名对不上库里的名字时不会发（日志里有“表情包库里没有…”）。网上找的候选图它都觉得不合适时，会选择不发。

**dsowner 让它改人设，它说改不了**：要你本人叫它（@、回复它或者叫别名），光在聊天里提一句不算；`.env` 里的 `OWNER_ID` 要是你自己的用户 ID。如果它说这个频道单独设了人设，那是你说了“全服务器”：在这个频道里不说“全服务器”就是改这里的，要改全服务器的就用 `/ds 人设`。

**它没记住我说的事**：想让它马上记住，就叫它说“记住……”，记下了会加个 📝。没明确叫它记的，要等频道安静一阵它整理的时候才会记（私密频道不会自动整理）。用 `/ds-rp` 能看到自己的卡上现在写了什么，`/ds 长期记忆 操作:查看` 能看到长期记忆。

**人设卡被改坏了**：本人用 `/ds-rp 操作:改回上一版`，或者直接打开编辑框改。

**私信**：目前只在服务器里工作，不处理私信。

---

## 十五、开发

欢迎参与改进！提 PR 之前请先看 [CONTRIBUTING.md](CONTRIBUTING.md)，里面有本地开发步骤、几条设计约束，以及中英两个版本怎么同步。每次 push 和 PR 都会由 GitHub Actions 自动跑一遍测试和离线自检。各版本的变化见 [CHANGELOG.md](CHANGELOG.md)。

```bash
python -m unittest discover -s tests      # 核心逻辑测试，不需要装 discord.py
```

读文件的测试会用 python-docx、openpyxl、python-pptx、reportlab 现场生成真实文件（只是测试用，bot 运行不需要它们；`pip install -r requirements-dev.txt` 会一起装上），没装的话对应测试会跳过。

| 文件 | 作用 |
|---|---|
| `dsbot/engine.py` | 调度引擎：缓冲、时机判断、额度、预算、执行动作、压缩摘要、跨频道记忆 |
| `dsbot/render.py` / `prompts.py` | 聊天记录渲染格式、system prompt |
| `dsbot/memory.py` | 人设卡、长期记忆的存储（各一个 SQLite 文件），整理记忆的输出解析 |
| `dsbot/llm.py` | DeepSeek HTTP 客户端（思考开关、JSON 模式、用量解析） |
| `dsbot/files.py` | 读文件：分类、解码、PDF / Office 解析、解析子进程 |
| `dsbot/search.py` | 联网搜索：DeepSeek 自带的 web_search（Anthropic 兼容接口），Tavily 备用 |
| `dsbot/links.py` / `tavily.py` | 读链接：推文走 FxTwitter，网页走 Tavily Extract |
| `dsbot/pictures.py` / `memes.py` | 上网找图（Tavily）、表情包库 |
| `dsbot/net.py` | 调用外部 API；下载网上的图时只连公网地址 |
| `dsbot/discord_app.py` / `convert.py` | Discord 事件与消息转换、发送、表情和贴图、“正在输入” |
| `dsbot/commands.py` | 斜杠命令和人设编辑框 |
| `dsbot/config.py` / `settings.py` | 配置、参数定义与分层覆盖 |
| `dsbot/store.py` / `ledger.py` | SQLite 存储、每日花费 |

本项目采用 [MIT 许可证](LICENSE)。
