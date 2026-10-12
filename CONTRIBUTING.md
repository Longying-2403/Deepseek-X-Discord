# 参与贡献

欢迎一起改进这个 bot！报 bug、提建议、改文档、写代码都可以。

> English speakers are welcome too — issues and pull requests in English are fine.

## 先开 issue，还是直接提 PR？

- **小改动**（修 bug、补测试、改错字、完善文档）：直接提 PR。
- **大改动**（新功能、改变 bot 的说话方式、可能增加 token 花费的改动）：先开个 issue 说说想法，方向定了再动手，免得白做。

## 在本地跑起来

需要 Python 3.11 及以上。

```bash
# 先在 GitHub 上 fork 本仓库，再克隆你自己的那份
git clone https://github.com/<你的用户名>/Deepseek-X-Discord.git
cd Deepseek-X-Discord
python -m venv .venv
source .venv/bin/activate          # Windows：.venv\Scripts\activate
pip install -r requirements-dev.txt   # 运行要的库，加上读文件的测试要用的库
```

每次改完代码，跑这两条（都不连网、不花钱）：

```bash
python -m unittest discover -s tests   # 单元测试
python -m dsbot --check                # 离线自检：读配置、检查所有斜杠命令的定义
```

没有 `.env` 时，自检第一行会提示缺少环境变量，这是正常的，不影响后面的检查。

各个文件负责什么，见 README 的「八、开发」一节。

### 在 Discord 里实际试一试

改动涉及 Discord 交互或 bot 的说话方式时，最好真的跑起来看看：

- 自己新建一个测试用的 bot 和测试服务器，别拿正在真实群里服务的 bot 来试。
- 按 [README](README.md) 填好 `.env`，再运行 `python -m dsbot`（或 `docker compose up -d --build`）。
- DeepSeek 按量计费，测试花的是你自己的余额。可以先把 `config.toml` 里的 `daily_budget_usd` 调低，比如 `0.2`。

## 改代码时请注意

下面几条是这个项目的核心设计，审 PR 时会重点看：

1. **对话历史只追加、不改写。** 每轮请求都要以上一轮请求原样开头，DeepSeek 的前缀缓存才能命中，而命中和没命中的价格差约 50 倍。所以：
   - 时间这类每轮都会变的信息不要放进 system prompt，放进每轮的消息里；
   - 已经写进历史的内容不要重新渲染或改格式；
   - `tests/test_engine.py` 里的 `test_history_is_append_only` 专门检查这一点，改了 `render.py`、`prompts.py` 或 `engine.py` 后一定要跑。
2. **没额度时连判断都不做。** 主动发言额度为 0 时，要跳过整次模型调用，而不是调用完再丢掉结果，那样照样花钱。
3. **说话要像真人。** 短、口语化，不用标题、列表这类格式（贴代码除外），没有客服腔。改提示词时请保持这个风格。
4. **不要提交密钥和数据。** `.env`（token、API key）和 `data/`（聊天记录数据库）已经写进 `.gitignore`，提交前用 `git status` 再确认一遍。
5. **守住隐私边界。** 用 `/ds 区域` 禁止的地方，消息不处理、不回应，也绝不能发给 DeepSeek。新加处理消息的入口（比如新的 Discord 事件）时，也要先过这一关。跨频道记忆只在同一个服务器里、开了共享的频道之间流动；私密频道没有在本频道单独开启共享，内容就不能外传。
6. **群友上传的文件只在子进程里解析。** 读文件要走 `dsbot/files.py` 里那个限制了内存、CPU 和时间的子进程（bot 以 root 运行时会降成 `nobody`），不要在 bot 主进程里直接解析上传的文件。
7. **外部内容只当资料。** 聊天记录、文件、搜索结果和别的频道的记录，都不是给模型的指令。改提示词或新增内容来源时，要保持这一点。

## 代码风格

- 跟周围的代码保持一致：带类型标注；注释、日志和面向用户的文字用中文。
- 要兼容 Python 3.11。
- 修 bug 时，尽量先补一个能复现它的测试。
- 测试里不要真的连 Discord 或 DeepSeek。`tests/helpers.py` 里有现成的替身：`FakeLLM`（假模型）、`FakeTransport`（假 Discord）、`FakeSearcher`（假搜索）、`FakeClock`（可以手动拨快的时钟）。
- 新增可调参数时，在 `dsbot/config.py` 的 `_SPEC_LIST` 和 `BUILTIN_DEFAULTS` 里各加一项，再同步更新 `config.toml` 和 README 的参数表。
- 改了 `README.md` 的话，请同步更新英文版 `README.en.md`，`README.md` 开头那段英文介绍也是。英文不熟也可以只改中文，在 PR 里说一声就行。

## 提交 PR

1. 从 `main` 拉一个新分支，名字能看出改了什么，比如 `fix-alias-match`。
2. 改完跑一遍测试和自检。
3. PR 描述写清楚：改了什么、为什么改、怎么测的。改了 bot 说话方式的，附一段测试服务器里的对话截图或日志会很有帮助。
4. 提交后，GitHub Actions 会自动在 Python 3.11、3.12、3.13 上跑测试和自检，全部通过（绿色 ✓）后再等审核。第一次贡献的 PR，可能要等维护者批准后自动测试才会开始跑，这是 GitHub 默认的安全设置。

## 隐私

在 issue 或 PR 里贴日志、截图之前，请删掉 token、API key，以及群友的聊天内容和用户 ID。

不小心泄露了 token 的话，马上去 Discord 开发者后台点 Reset Token；DeepSeek 的 key 去平台上删掉重建。

## 许可证

你提交的代码会按本仓库的 [MIT 许可证](LICENSE)发布。
