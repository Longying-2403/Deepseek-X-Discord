# DeepSeek 群友（Discord Bot）

**中文** | [English](README.en.md)

[![tests](https://github.com/Longying-2403/Deepseek-X-Discord/actions/workflows/tests.yml/badge.svg)](https://github.com/Longying-2403/Deepseek-X-Discord/actions/workflows/tests.yml)

一个用 DeepSeek V4.1 Flash（`deepseek-flash`）驱动、像普通群友一样待在 Discord 服务器里的 bot：

- **被叫到就回**：有人 @ 它、回复它的消息、或叫它的别名，立刻回应。
- **主动接话**（按频道开启）：等群里冷场一小会儿（或等太久），再让模型判断要不要说话；默认倾向沉默。
- **像真人**：回复短、口语化；先显示“正在输入”，按字数等几秒再发；可以把一段话拆成两三条，或只加个表情。
- **话量可控**：主动发言额度按群里的消息量恢复，群越热闹恢复越快；档位和各项参数都能在 Discord 里调。
- **省钱**：对话历史只追加不改写，尽量吃满 DeepSeek 的前缀缓存；没额度时连判断都不做；每日预算超了自动降级。
- **能看图**：上传的图片、GIF 选单发的 GIF、贴的图片链接、Discord 贴图都会一起发给模型（动图只看第一帧，可关）。
- **能读文件**：文本和代码、PDF（扫描件会看前几页的图）、Word、Excel、PPT。叫它时附上的文件直接读；“帮我看看上面那个文件”这种，它会自己去打开。
- **能上网搜**：被叫到时，问到新闻、价格、比分、天气这类它不知道的最新信息，会先用 DeepSeek 自带的搜索查一下再回答。每个服务器每天有次数上限。
- **跨频道记忆**（管理员开启）：在 A 频道跟它聊过的事，换到 B 频道它也接得上；也能翻开了共享的别的频道的记录。私密频道默认不外传。
- **区域可控**：可以按频道、类别或整个服务器限制它在哪里回应。

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

Setup, commands and settings in English: [README.en.md](README.en.md)

---

## 一、准备工作

### 1. 创建 Discord bot

1. 打开 [Discord 开发者后台](https://discord.com/developers/applications) → New Application。
2. 左侧 **Bot** 页：
   - 点 Reset Token，复制 token（填到 `.env` 的 `DISCORD_TOKEN`）。
   - 往下找到 **Privileged Gateway Intents**，打开 **Message Content Intent**（必须，否则看不到消息内容）。
3. 邀请进服务器：bot 第一次启动时会在日志里打印邀请链接（已带好权限）。也可以在后台 **OAuth2 → URL Generator** 里勾 `bot` 和 `applications.commands`，权限勾：查看频道、发送消息、在帖子中发送消息、读取消息历史、添加反应、嵌入链接。

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
| `CONTROL_GUILD_ID` | 可选。你的私人服务器 ID，主人命令 `/dsowner` 只注册在这里 |
| `DISCORD_PROXY` | 可选。连 Discord 需要代理时填，如 `http://127.0.0.1:7890` |
| `TAVILY_API_KEY` | 可选。DeepSeek 自带的搜索用不了时，改用 [Tavily](https://tavily.com)（每月免费 1000 次） |

---

## 二、部署

### 方式 A：Docker Compose（推荐）

```bash
docker compose up -d --build
docker compose logs -f          # 看日志，确认“已登录”
```

数据库在 `./data/`，改了 `config.toml` 之后 `docker compose restart` 就行。

### 方式 B：直接用 Python（3.11 及以上）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m dsbot --check         # 离线自检：配置、斜杠命令定义（不连 Discord、不花钱）
python -m dsbot --test-search "台北 今天 天气"   # 可选：真的搜一次，确认联网搜索能用（花一点点钱）
python -m dsbot
```

---

## 三、上手

在服务器里（需要“管理服务器”权限）：

1. 先圈定它能待的地方（可选）：`/ds 区域 操作:禁止 范围:全服务器`，再到要用的频道执行 `/ds 区域 操作:允许`，或者选 `范围:本类别` 放开整个区
2. 在想让它主动聊天的频道里：`/ds 主动 开关:开`
3. 选个话量：`/ds 档位`（安静 / 普通 / 话痨）
4. 给它定人设：`/ds 人设`（弹出编辑框）
5. 想让它记得别的频道聊过的事：`/ds 记忆共享 操作:开 范围:全服务器`（见下面“七、跨频道记忆”）
6. 随时看状态：`/ds 状态`（额度、上次的判断和理由、今天花了多少、缓存命中率、记忆共享和搜索次数）；`/ds 区域列表` 看它在哪些地方会回应

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
| `/ds 人设` | 修改人设（弹出编辑框） |
| `/ds 记忆共享` | 开 / 关 / 跟随上级。开了共享的频道之间互通记忆；私密频道要在那个频道里单独开才会分享出去 |
| `/ds 状态` | 查看本频道的设置、额度、上次判断、今天花费、记忆共享、今天的搜索次数 |
| `/ds 清空记忆` | 清空本频道的对话记录和摘要 |

除了“区域列表”“状态”和“清空记忆”，这些命令都有一个“范围”选项：**本频道**（默认，人设除外）、**本类别**（Discord 里的“XX区”）、**全服务器**。

设置的优先级：**本频道 > 帖子所在的频道 > 所在类别 > 本服务器 > 主人设的全局值 > config.toml**。比如全服务器禁止、“闲聊区”类别允许、其中某个频道又单独禁止，结果就是只有闲聊区里除那个频道以外的地方会回应。

> 想彻底隔开某些频道，也可以直接在 Discord 的频道权限里去掉 bot 的“查看频道”，那样消息根本不会传给 bot。

### `/dsowner`（只有你；只注册在 `CONTROL_GUILD_ID` 那个服务器）

| 命令 | 作用 |
|---|---|
| `/dsowner 设置` | 改任何参数（含预算、思考强度、联网搜索开关和每日搜索上限），可以指定某个服务器 ID，不填就是全局 |
| `/dsowner 总览` | 今天每个服务器花了多少、调用多少次、缓存命中率 |

没设置 `CONTROL_GUILD_ID` 时不注册主人命令，直接改 `config.toml` 即可。

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
- 部署后可以先跑一次 `docker compose exec dsbot python -m dsbot --test-search "台北 今天 天气"` 确认能搜到。如果 DeepSeek 自带的搜索用不了（比如接口改了），在 `.env` 里填 `TAVILY_API_KEY`，会自动改用 Tavily。

## 九、它是怎么工作的

```
消息进来 ──► 存进该频道的缓冲区
              │
              ├─ 是在叫它？──► 立刻跑一轮（不耗额度，只受每人限速）
              │
              └─ 每 5 秒检查：开了主动 + 有新消息 + (冷场 或 等太久)
                              + 有额度 + 间隔够 + 没超预算 ──► 跑一轮

一轮 = 把这批新消息编号（#12、#13…）渲染成一条 user 消息（该读的文件内容跟在后面）
      → 连同全部历史发给 DeepSeek（JSON 输出）
      → 模型返回 {"actions": [...]}：回复 / 加表情 / 看文件 / 上网搜 / 翻别的频道 / 什么都不做
        （要看文件、搜索、翻记录时：办完把结果追加进去，再问它一次）
      → 这一轮的 user + assistant 原样追加进历史（沉默也记）
      → 显示“正在输入”，按字数等几秒再发
```

**缓存**：历史只追加、不改写，system prompt 固定（时间等会变的信息放在每轮消息里），所以每次请求都以上一次的请求开头，绝大部分输入按缓存命中价计费（便宜约 50 倍）。上下文太长时把较旧的一半（按 token 估算）压成摘要，最近 6 轮不动，只在那一次 miss。日志里每轮都会打印输入 token 数和命中数。

**费用参考**（高峰价，3 万 token 上下文，缓存正常时只有新消息那几百 token 未命中）：一次判断约 0.04–0.06 美分（开思考会多一些推理 token）；一个频道全天热聊、每 3 分钟判断一次，一天约 5–10 美分。非高峰时段（含周末）半价。价格变动时改 `config.toml` 的 `[pricing]`。

---

## 十、常见问题

**@ 它没反应**
- 这个频道不在允许的区域：`/ds 状态` 第一行会提示，`/ds 区域列表` 看整体设置。
- 后台的 Message Content Intent 没打开（日志里会报 4014 之类的错误）。
- 频道里没给 bot 发送消息的权限。
- 你 @ 的可能是它的身份组而不是它本人——这也能识别，但如果服务器把它的身份组隐藏了就看不到。

**主动发言一直不说话**
- `/ds 状态` 看看：主动发言开了吗？额度是不是 0？上次判断的“想法”是什么？
- 模型默认倾向沉默。想让它更活跃：`/ds 档位 话痨`，或在人设里写“你很爱聊天”。

**换了人设但说话风格没变**：历史里还有旧风格的发言，`/ds 清空记忆` 后重新开始。

**GIF 说看不到**：GIF 选单发的 GIF 其实是一个链接，Discord 过一两秒才补上预览图。它会读这个预览图（动图的第一帧）；如果 @ 它的那条消息自己就带链接，它会先等两秒再回。网页链接（比如 YouTube）只读标题，不看缩略图。

**它说文件读不了**：看它说的原因。常见的是老格式（.doc/.xls/.ppt）、设了打开密码的文件、超过 20 MB。如果它说“解析文件的进程启动不了”，是 bot 这边的问题（日志里有详细原因，多半是权限），修好后再叫它读就行。`python -m dsbot --check`（Docker 里是 `docker compose exec dsbot python -m dsbot --check`）会检查 PDF 组件和解析子进程是否正常。

**DeepSeek 余额不足**：bot 会暂停调用 10 分钟并在日志里提示，充值后自动恢复。暂停期间有人叫它，会给那条消息加个 ⚠️。

**某批消息被 DeepSeek 拒绝**（比如触发内容审核）：如果带了图片，先去掉图片重试一次；还不行就丢掉这批消息、在日志里记一笔，不会卡住频道。

**频道里发不出消息**（缺权限）：连续 3 轮发不出去，这个频道的主动发言会暂停 30 分钟，`/ds 状态` 里能看到提示。

**私信**：目前只在服务器里工作，不处理私信。

---

## 十一、开发

欢迎参与改进！提 PR 之前请先看 [CONTRIBUTING.md](CONTRIBUTING.md)，里面有本地开发步骤和几条设计约束。每次 push 和 PR 都会由 GitHub Actions 自动跑一遍测试和离线自检。各版本的变化见 [CHANGELOG.md](CHANGELOG.md)。

```bash
python -m unittest discover -s tests      # 核心逻辑测试，不需要装 discord.py
```

读文件的测试会用 python-docx、openpyxl、python-pptx、reportlab 现场生成真实文件（只是测试用，bot 运行不需要它们；`pip install -r requirements-dev.txt` 会一起装上），没装的话对应测试会跳过。

| 文件 | 作用 |
|---|---|
| `dsbot/engine.py` | 调度引擎：缓冲、时机判断、额度、预算、执行动作、压缩摘要、跨频道记忆 |
| `dsbot/render.py` / `prompts.py` | 聊天记录渲染格式、system prompt |
| `dsbot/llm.py` | DeepSeek HTTP 客户端（思考开关、JSON 模式、用量解析） |
| `dsbot/files.py` | 读文件：分类、解码、PDF / Office 解析、解析子进程 |
| `dsbot/search.py` | 联网搜索：DeepSeek 自带的 web_search（Anthropic 兼容接口），Tavily 备用 |
| `dsbot/discord_app.py` / `convert.py` | Discord 事件与消息转换、发送、表情、“正在输入” |
| `dsbot/commands.py` | 斜杠命令和人设编辑框 |
| `dsbot/config.py` / `settings.py` | 配置、参数定义与分层覆盖 |
| `dsbot/store.py` / `ledger.py` | SQLite 存储、每日花费 |

本项目采用 [MIT 许可证](LICENSE)。
