# DeepSeek 群友（Discord Bot）

**中文** | [English](README.en.md)

[![tests](https://github.com/Longying-2403/Deepseek-X-Discord/actions/workflows/tests.yml/badge.svg)](https://github.com/Longying-2403/Deepseek-X-Discord/actions/workflows/tests.yml)

一个用 DeepSeek V4.1 Flash（`deepseek-flash`）驱动、像普通群友一样待在 Discord 服务器里的 bot：

- **被叫到就回**：有人 @ 它、回复它的消息、或叫它的别名，立刻回应。
- **主动接话**（按频道开启）：等群里冷场一小会儿（或等太久），再让模型判断要不要说话；默认倾向沉默。
- **像真人**：回复短、口语化；先显示“正在输入”，按字数等几秒再发；可以把一段话拆成两三条，或只加个表情。
- **话量可控**：主动发言额度按群里的消息量恢复，群越热闹恢复越快；档位和各项参数都能在 Discord 里调。
- **省钱**：对话历史只追加不改写，尽量吃满 DeepSeek 的前缀缓存；没额度时连判断都不做；每日预算超了自动降级。
- **能看图**：群友发的截图、梗图会一起发给模型（可关）。

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
python -m dsbot
```

---

## 三、上手

在服务器里（需要“管理服务器”权限）：

1. 在想让它主动聊天的频道里：`/ds 主动 开关:开`
2. 选个话量：`/ds 档位`（安静 / 普通 / 话痨）
3. 给它定人设：`/ds 人设`（弹出编辑框）
4. 随时看状态：`/ds 状态`（额度、上次的判断和理由、今天花了多少、缓存命中率）

没开主动发言的频道里，它只在被叫到时说话。

> 建议在频道说明里告诉大家：这个频道的消息会发给 DeepSeek（服务托管在中国）处理。

---

## 四、命令

### `/ds`（服务器管理员）

| 命令 | 作用 |
|---|---|
| `/ds 主动` | 开关主动发言。范围默认是本频道，也可以设为全服务器默认 |
| `/ds 档位` | 一键套用：安静 / 普通 / 话痨 |
| `/ds 设置` | 调整单项参数，值填“默认”可恢复 |
| `/ds 人设` | 修改人设（全服务器或本频道） |
| `/ds 状态` | 查看本频道的设置、额度、上次判断、今天花费 |
| `/ds 清空记忆` | 清空本频道的对话记录和摘要 |

设置的优先级：**本频道 > 帖子所在的频道 > 本服务器 > 主人设的全局值 > config.toml**。

### `/dsowner`（只有你；只注册在 `CONTROL_GUILD_ID` 那个服务器）

| 命令 | 作用 |
|---|---|
| `/dsowner 设置` | 改任何参数（含预算、思考强度），可以指定某个服务器 ID，不填就是全局 |
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
| 别名 | 空 | 消息里出现这些名字也算叫它，逗号分隔，如 `DS,小D` |

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

“一轮”的意思：同一个人 20 秒内连发的几条算一轮，免得分行打字刷额度。

---

## 六、它是怎么工作的

```
消息进来 ──► 存进该频道的缓冲区
              │
              ├─ 是在叫它？──► 立刻跑一轮（不耗额度，只受每人限速）
              │
              └─ 每 5 秒检查：开了主动 + 有新消息 + (冷场 或 等太久)
                              + 有额度 + 间隔够 + 没超预算 ──► 跑一轮

一轮 = 把这批新消息编号（#12、#13…）渲染成一条 user 消息
      → 连同全部历史发给 DeepSeek（JSON 输出）
      → 模型返回 {"actions": [...]}：回复 / 加表情 / 什么都不做
      → 这一轮的 user + assistant 原样追加进历史（沉默也记）
      → 显示“正在输入”，按字数等几秒再发
```

**缓存**：历史只追加、不改写，system prompt 固定（时间等会变的信息放在每轮消息里），所以每次请求都以上一次的请求开头，绝大部分输入按缓存命中价计费（便宜约 50 倍）。上下文太长时一次性把旧的一半压成摘要，只在那一次 miss。日志里每轮都会打印输入 token 数和命中数。

**费用参考**（高峰价，3 万 token 上下文，缓存正常时只有新消息那几百 token 未命中）：一次判断约 0.04–0.06 美分（开思考会多一些推理 token）；一个频道全天热聊、每 3 分钟判断一次，一天约 5–10 美分。非高峰时段（含周末）半价。价格变动时改 `config.toml` 的 `[pricing]`。

---

## 七、常见问题

**@ 它没反应**
- 后台的 Message Content Intent 没打开（日志里会报 4014 之类的错误）。
- 频道里没给 bot 发送消息的权限。
- 你 @ 的可能是它的身份组而不是它本人——这也能识别，但如果服务器把它的身份组隐藏了就看不到。

**主动发言一直不说话**
- `/ds 状态` 看看：主动发言开了吗？额度是不是 0？上次判断的“想法”是什么？
- 模型默认倾向沉默。想让它更活跃：`/ds 档位 话痨`，或在人设里写“你很爱聊天”。

**换了人设但说话风格没变**：历史里还有旧风格的发言，`/ds 清空记忆` 后重新开始。

**DeepSeek 余额不足**：bot 会暂停调用 10 分钟并在日志里提示，充值后自动恢复。暂停期间有人叫它，会给那条消息加个 ⚠️。

**某批消息被 DeepSeek 拒绝**（比如触发内容审核）：如果带了图片，先去掉图片重试一次；还不行就丢掉这批消息、在日志里记一笔，不会卡住频道。

**频道里发不出消息**（缺权限）：连续 3 轮发不出去，这个频道的主动发言会暂停 30 分钟，`/ds 状态` 里能看到提示。

**私信**：目前只在服务器里工作，不处理私信。

---

## 八、开发

欢迎参与改进！提 PR 之前请先看 [CONTRIBUTING.md](CONTRIBUTING.md)，里面有本地开发步骤和几条设计约束。每次 push 和 PR 都会由 GitHub Actions 自动跑一遍测试和离线自检。

```bash
python -m unittest discover -s tests      # 核心逻辑测试，不需要装 discord.py
```

| 文件 | 作用 |
|---|---|
| `dsbot/engine.py` | 调度引擎：缓冲、时机判断、额度、预算、执行动作、压缩摘要 |
| `dsbot/render.py` / `prompts.py` | 聊天记录渲染格式、system prompt |
| `dsbot/llm.py` | DeepSeek HTTP 客户端（思考开关、JSON 模式、用量解析） |
| `dsbot/discord_app.py` / `convert.py` | Discord 事件与消息转换、发送、表情、“正在输入” |
| `dsbot/commands.py` | 斜杠命令和人设编辑框 |
| `dsbot/config.py` / `settings.py` | 配置、参数定义与分层覆盖 |
| `dsbot/store.py` / `ledger.py` | SQLite 存储、每日花费 |

本项目采用 [MIT 许可证](LICENSE)。
