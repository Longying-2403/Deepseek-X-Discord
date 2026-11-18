# 更新记录 · Changelog

每个版本的主要变化，中文在前、English below。

## [2.8.2] - 2026-11-18

> 英文版这次没有更新，还是 2.0.0。
> The English edition wasn't updated this time and stays at 2.0.0.

### 中文

**改进**

- **一条消息带好几张图时更快**：以前一张下载、缩小完才轮到下一张，现在同时下载（同时缩小的还是最多两张，免得太占内存）。看哪几张、先后顺序和以前一样，也不会多下载。NAS 上实测（假设每张下载要 0.3 秒）：3 张图从 1.4 秒降到 0.65 秒，4 张从 1.9 秒降到 0.66 秒。
- **又有图、又有文件或链接时更快**：看图和读文件、打开链接同时进行，不用等图都下完了才去读。
- **要上网搜的那一轮更快**：上网搜、找图不再等读完文件、打开完链接才开始，要搜两样东西时也同时搜（搜到的还是照原来的顺序给它看）。实测一条消息里搜两样东西：以前第二次搜索比第一次晚 0.4 秒回来，现在同时回来；又搜又打开链接时，大约省下搜索的那段时间。
- 每天的搜索、找图、打开链接的次数和预算照旧。

**升级须知**

- 只有 `dsbot` 文件夹变了：换掉它，再 `sudo docker compose up -d --build`。确认新版生效：日志里有 `dsbot 2.8.2 启动中`。数据库不变。从 2.8.0 或更早的版本升上来，`Dockerfile` 也要换（见 2.8.1）。
- 英文版这次没有更新，还是 v2.0.0。

**开发**

- `engine.py`：`_together`（同时办几件互不相干的事，结果照传进去的顺序；有一件出错，别的停下、收拾完，抛出去的还是它本来的错）、`_settle`（提前开始办的事用不着了：停下、收拾完）。
- `Engine._images` 一批一批同时下载：每批只拿“一张一张来也一定会下载”的那几张（还剩几个名额就拿几张），有没下成的再拿后面的补，所以选中的图和原来一模一样。`_round` 里 `_images` 和 `_round_content` 同时。
- `Engine._tool_step`：排在点歌前面的 `search`、`image` 一开始就各建一个任务（各自先记一笔占住每天的名额，顺序照旧），结果在原来的位置取；排在点歌后面的不提前（按歌名找歌和搜索共用每天的搜索次数）。`_read_links(pay=…)`：打开链接用不用要花钱的读法在这之前定好，和原来一样；搜不搜、找不找图则是在这一轮打开链接的钱记上之前定的（预算正好卡在这中间时，最多多搜这一轮的这几次）。
- 测试增加到 1385 个（新增 `tests/test_parallel.py`）。

### English

**Improved**

- **Faster when a message has several images**: images used to be downloaded and resized one after another; they are now downloaded at the same time (still at most two being resized at once, to keep memory in check). Which images it looks at and their order are the same as before, and nothing extra is downloaded. Measured on a NAS (assuming 0.3 seconds per download): 3 images went from 1.4 seconds to 0.65, 4 images from 1.9 seconds to 0.66.
- **Faster when there are images plus files or links**: looking at images and reading files / opening links now happen at the same time.
- **Faster in rounds that search the web**: web searches and picture searches no longer wait until files are read and links are opened, and two searches run at the same time (the results are still shown to the model in the original order). Measured with two searches in one message: the second result used to arrive 0.4 seconds after the first, now both arrive together; searching while opening a link saves about the time the search takes.
- The daily limits for searches, picture searches and opened links, and the budget rules, are unchanged.

**Upgrading**

- Only the `dsbot` folder changed: replace it, then run `sudo docker compose up -d --build`. To check the new version took effect: the log shows `dsbot 2.8.2 启动中`. The database is unchanged. Coming from 2.8.0 or earlier, replace the `Dockerfile` too (see 2.8.1).
- The English edition wasn't updated this time and stays at v2.0.0.

**Development**

- `engine.py`: `_together` (runs independent jobs at the same time and returns results in the order given; if one fails the others are stopped and awaited, and the original exception is raised, not a wrapped one) and `_settle` (stops jobs that were started early and are no longer needed).
- `Engine._images` downloads in waves: each wave only takes the images that the one-by-one version would certainly have downloaded (as many as there are slots left), and failures are made up from the next ones, so the chosen images are exactly the same. In `_round`, `_images` and `_round_content` run together.
- `Engine._tool_step`: `search` and `image` actions that come before any music action each get a task right away (each reserves its daily slot first, in the same order as before) and their results are collected at their original position; ones after a music action are not started early (finding a song by name shares the daily search limit). `_read_links(pay=…)`: whether opening links may use the paid reader is decided before those tasks start, as before; whether to search / find pictures is decided before this round's link costs are booked (if the budget runs out exactly in between, at most this round's searches go over).
- There are now 1385 tests (new `tests/test_parallel.py`).

## [2.8.1] - 2026-11-18

> 英文版这次没有更新，还是 2.0.0。
> The English edition wasn't updated this time and stays at 2.0.0.

### 中文

**改进**

- **读文件更快**：群友发来的每个文件都要另开一个子进程来解析，以前这个子进程光启动就要 0.8～1 秒（NAS 上实测）。现在：
  - 构建镜像时就把 Python 标准库和 bot 的代码编译好。官方的 Python 镜像为了省地方把标准库编译好的文件删了，镜像里又关掉了运行时写这些文件，以前每次启动、每读一个文件都要从头编译一遍；
  - 子进程只先导入这种文件用得到的库（文本只要猜编码的那个，PDF 要 pypdf 和 Pillow，Word、Excel、PPT 一个都不用），以前不管什么文件三个都导入。

  NAS 上实测：读一个文本、Word 文件从 0.8 秒左右降到 0.2 秒左右，扫描版 PDF 从 1.1 秒降到 0.4 秒。
- **启动更快**：同一个原因，bot 启动时导入自己的代码从 1.4 秒降到 0.6 秒（NAS 上实测）。
- **打开很大的网页更快**：清掉网页里直接嵌的图（一大串 base64）那一步换了个写法，清掉的内容和原来一样，10 MB 的网页快几倍。

**升级须知**

- 这次除了 `dsbot` 文件夹，`Dockerfile` 也变了：两个都换掉，再 `sudo docker compose up -d --build`（镜像会大 14 MB 左右）。确认新版生效：日志里有 `dsbot 2.8.1 启动中`。`Dockerfile` 没换也照样能跑，只是读文件、启动没那么快。数据库不变。
- 英文版这次没有更新，还是 v2.0.0。

**开发**

- `Dockerfile`：装完依赖后用 `python -m compileall` 编译标准库（不含 `site-packages`：pip 安装时已经编译过；编译不成不拦着构建），拷进代码后再编译 `dsbot`。
- `files.py`：`_PRELOAD_FOR`（`classify` 的类型 → 先导入哪几个库）、`_preload_libraries(kind)`、`_child_args(kind, …)`；`_probe` 不带类型，照旧全部导入。读某种文件要用到新的库时，记得加进 `_PRELOAD_FOR`（`test_preload_table_covers_what_the_readers_import` 会查）。
- `links.py` 的 `_DATA_URI`：开头的 `\b` 换成 `data:(?<!\wdata:)`（意思一样，开头是固定的几个字，正则能直接跳着找）。
- `__main__.py` 的 `check_files`：读完测试用的 Word 文件后再 `_probe` 一次（读 Word 文件不再导入 pypdf、charset_normalizer、Pillow，另外查它们在子进程里导入得了没有）。
- 测试增加到 1368 个（`test_files.py` 的 `ChildStartup`、`test_links_images.py` 的 `PagesAndImages`）。

### English

**Improved**

- **Faster file reading**: every file a member sends is parsed in a separate child process, and just starting that process used to take 0.8–1 second (measured on a NAS). Now:
  - the image build compiles the Python standard library and the bot's code ahead of time. The official Python image deletes the standard library's compiled files to save space, and the image also turns off writing them at run time, so every start and every file read used to compile everything from scratch;
  - the child process only preloads the libraries this kind of file needs (the encoding detector for text, pypdf and Pillow for PDF, none for Word, Excel and PowerPoint); before, all three were loaded for every file.

  Measured on a NAS: reading a text or Word file went from about 0.8 seconds to about 0.2, a scanned PDF from 1.1 seconds to 0.4.
- **Faster startup**: for the same reason, importing the bot's own code at startup went from 1.4 seconds to 0.6 (measured on a NAS).
- **Faster on very large web pages**: the step that strips images embedded in a page (long base64 strings) was rewritten; it removes exactly what it removed before and is several times faster on a 10 MB page.

**Upgrading**

- Besides the `dsbot` folder, the `Dockerfile` changed this time: replace both, then run `sudo docker compose up -d --build` (the image grows by about 14 MB). To check the new version took effect: the log shows `dsbot 2.8.1 启动中`. With the old `Dockerfile` everything still works, just without the faster file reading and startup. The database is unchanged.
- The English edition wasn't updated this time and stays at v2.0.0.

**Development**

- `Dockerfile`: after installing the dependencies it runs `python -m compileall` on the standard library (without `site-packages`, which pip already compiled at install time; a failure here doesn't stop the build), and compiles `dsbot` after copying the code in.
- `files.py`: `_PRELOAD_FOR` (the kind from `classify` → which libraries to preload), `_preload_libraries(kind)`, `_child_args(kind, …)`; `_probe` passes no kind and still preloads everything. When reading some kind of file starts needing a new library, add it to `_PRELOAD_FOR` (`test_preload_table_covers_what_the_readers_import` checks this).
- `_DATA_URI` in `links.py`: the leading `\b` became `data:(?<!\wdata:)` (same meaning; with a fixed literal at the start the regex engine can skip ahead to it).
- `check_files` in `__main__.py` runs `_probe` once more after reading its test Word file (reading a Word file no longer imports pypdf, charset_normalizer and Pillow, so their import in the child is checked separately).
- There are now 1368 tests (`ChildStartup` in `test_files.py`, `PagesAndImages` in `test_links_images.py`).

## [2.8.0] - 2026-11-13

> 英文版这次没有更新，还是 2.0.0。
> The English edition wasn't updated this time and stays at 2.0.0.

### 中文

**新增**

- **连通频道**（Beta）：bot 主人用新命令 `/dsowner chatsync`，把几个服务器（也可以是同一个服务器）的文字频道连通，几个频道共用一份聊天记录：
  - **当成一段对话**：在哪个频道叫它，它都看得到连通的频道里聊过的，记录里每段写明是哪个服务器的哪个频道；消息编号、更早的摘要也是同一份。被叫到时，别的连通频道里还没进记录的消息先补进来（不另外问模型、不花钱，也不在那边回话），那边叫它的消息留给那边；补进来的让这一轮被 DeepSeek 拒了（比如内容审核），换成一句“没记下”，不会卡住几个频道（群友把最近的消息改成了被拒的内容，那几条也当成删了）。
  - **只在这里说话**：它只在被叫（或者主动发言）的那个频道说话、加表情；回应别的频道的消息时就在这里说（挂不成回复）。
  - **各管各的**：人设、设置、预算、表情和贴图、人设卡、长期记忆还是每个服务器（或服务器组）自己的；@ 人只认这个服务器的人，提到别的服务器的频道不换成这里同名频道的链接；整理记忆时每段按它在哪个服务器，记进那个服务器的那份。
  - **保险**：只有 bot 主人能连，只能连公开的文字频道；连通、断开时在相关的频道各发一条说明。连通之前各频道的旧聊天不给别的频道看（每个频道自己还记得，只给它自己看个大概；连通那一刻还没进记录的闲聊也记在它自己那份里）；私信里的事（私信记忆、刚才私信里聊的）不带进连通的频道。频道连通后被改成私密的：连通暂停，它不看那里的消息，说明和列表里也只写连通时的频道名（不显示它现在叫什么）；这时在那里改过的消息，共用的记录里那一行当成删了（还没处理的那条也不再处理；被 `/ds 区域` 关掉了的连通频道、断开以后变成私密的频道也一样），它自己的记录照常改（连通期间说的，断开拿回来以后照改过的写）。
  - **跨服务器连通时**（几个频道不全在同一个服务器，连成服务器组的算一个），共用的记录里只放这几个频道里说的：翻别的频道的记录、按名字查人设卡不能用，别处的近况不带进来，@ 人只认在这个频道说过话、被 @ 过的人；人设卡只在所有服务器共用一张时才附上（各服务器分开时这里也不改人设卡，请群友自己用 `/ds-rp`），它改长期记忆时记下的回话里也不留那一条的原文；语音频道在放什么写明是哪个服务器的。同一个服务器里的几个频道连通时这些照常。
  - **变成跨服务器时**：原来都在同一个服务器（或服务器组）里的组连进了别的服务器的频道，或者组里的服务器退出了服务器组，共用的记录里只有原来那边能看的内容（别的频道带来的近况、能翻的频道名单、翻到的记录、按名字查到的人设卡；人设卡各服务器分开时连同附着的卡，还有它自己写过的卡）先拿掉。连通前的预览会写明。原来只有一个服务器时写的“语音频道在放什么”，也补上是哪个服务器的。组里的频道还在一个服务器组里、可是组里原来有的服务器退出了，也一样整份查一遍（断开了的频道留下的那几段也算）。
  - **服务器组里的几个服务器的频道连通时**：语音频道在放什么也写明是哪个服务器的；服务器组以外的服务器的近况要这几个服务器都开了“跨服务器”才带进来。连进来的频道所在的服务器原来不在这个连通组里、又没开“跨服务器”的话，共用的记录里写着是哪个服务器的近况先拿掉（服务器组里的也算）（预览会写明）。
  - **加进已经有的组**：新来的频道也看得到组里已经共用的记录（预览和说明里会写明）。
  - **连通**（`/dsowner chatsync 连通 频道:… 连到:…`，频道从列表里选，能按服务器名、频道名筛选；也可以用 # 选，或者直接打频道名）、**断开**（`/dsowner chatsync 断开 频道:…`：带回自己这次连通期间那部分，别的频道说的、共用的摘要不带走；只剩一个频道时组就散了）、**查看**（`/dsowner chatsync 查看`）。连通、断开之前会先说清楚会怎样，点确认才办，也会等正在回的话说完。
  - **清空记忆**：在连通的频道里 `/ds 清空记忆`，只清掉这个频道说过的（连同共用的摘要）和它连通之前的记录，别的频道说的不动；以前连通过的频道清空时，留在那个组里的它那部分也删掉（它说的已经压进那个组的摘要的话，连同摘要）。删消息、改消息照常同步（断开以后也是）。
  - **整理记忆**：几个频道的聊天分开整理，一个服务器那部分出错、超了预算，不耽误别的服务器的；整理过的断开、重启以后都不再整理。自己那份记录在连通期间被旧版本写过的频道断开时，组还在的话，连通期间说的留在组里，照样在组里整理（组散了就和共用的记录一起删掉）。
  - `/ds 状态` 会写这个频道和哪些频道连通了（跨服务器连通时还会写哪些不能用）。共用的记录长得快，费用会比各聊各的高一些。

**改进**

- **频道链接**：bot 在好几个服务器里时，它话里写“「某某」#大厅”（没写“服务器”两个字），某某又是 bot 在的别的服务器，就不换成这里同名频道的链接了（以前只认“「某某」服务器的 #大厅”“#大厅（「某某」服务器）”）。「」里是别的字（“「必看」#公告”）照常换。

**升级须知**

- 只有 `dsbot` 文件夹变了：换掉它，再 `sudo docker compose up -d --build`。确认新版生效：日志里有 `dsbot 2.8.0 启动中`。
- 数据库自动升级：`dsbot.sqlite3` 加了六张表（`chat_syncs`、`chat_sync_seq`、`chat_sync_summary`、`chat_sync_kept`、`chat_unvetted`、`chat_sync_scrub`），`turns` 加两列（`origin`、`reviewed`），`msgmap` 加一列 `origin`，`conversations` 加一列 `took_back`。原来的数据不动；不连通频道时一切照旧，2.7.x 也还能读写升级后的文件。用过连通频道再换回 2.8.0 以前的版本，先把连通的频道都断开（旧版本不认连通组，连通期间的聊天它看不到）；断开时拿回了连通期间那部分的频道，换回去以后很早的消息被删、被编辑可能就改不到记录里了（旧版本按编号只记得最近 3000 个，拿回来的编号中间跳过了一段），介意的话先在那些频道 `/ds 清空记忆`。
- `/dsowner 服务器组` 的查看、连起来、退出改成了三个子命令（`/dsowner 服务器组 连起来 服务器:… 连到:…`，要填的栏都是必填：手机、平板上也填得上）。
- `/dsowner` 多了 `chatsync`：重启后 Discord 里的命令列表会自己更新（可能要等一两分钟，或者重开 Discord）。
- 英文版这次没有更新，还是 v2.0.0。

**开发**

- 引擎里一份聊天记录的状态（记录、消息编号、读过的文件和链接、删改、附过的人设卡、整理记忆的进度、锁、`generation`）从 `ChannelState` 搬进新的 `ConvState`（`Engine.convs`，按记录的 ID）；`ChannelState` 上照旧能用 `st.xxx`（`_Shared` 转到 `st.conv`）。没连通的频道一个频道一份，和以前一样；连通的几个频道共用一份（ID 是连通组的负数）。`generation` 改成全局不重复的编号（换了记录也不会和旧的撞上）。
- `Store`：`chat_syncs`（记着连进来时的 `joined_turn`、`joined_sid`、`joined_next`：断开时只拿回这次连通期间的，自己那份被旧版本写过就不拿）/ `chat_sync_seq` / `chat_sync_summary`（哪个组的摘要里有哪个频道的事：`compact` 记，`origin_syncs` 也看它）/ `chat_sync_kept`（断开时没能拿回去、留在组里整理的；`sync_joined_turns` 也算它）/ `chat_unvetted`（每份记录里还没问过模型的那几轮、直接改过的那几条消息：`load_unvetted`、`save_unvetted`；清空记录、散组时一起删）/ `chat_sync_scrub`（服务器退出服务器组时正在跑的那一轮写完以后要怎么再查：`mark_scrub`、`pending_scrubs`、`clear_scrub`）；`turns.origin`、`turns.reviewed`（连通组分开整理完的那几条，`mark_reviewed`、`reviewed_turns`；断开时拿回去的照这个算，整理进度只跳过整理完的那一段）、`msgmap.origin`、`conversations.took_back`；连通组的记录和拿回过的记录，旧的编号映射按条数留（编号中间会跳一段）；`msgmap(message_id)` 索引；`message_keys`、`origin_syncs`、`next_short_id`、`stale_syncs`、`turn_ids`、`record_turns`、`msg_origins`；`link_channels`、`unlink_channel`（断开的频道把自己那部分复制回去，`_take_back`）、`drop_origin`（以前在组里的频道：它说的压进过摘要才连同摘要删）、`raise_join_marks`、`sync_joined_turns`、`sync_of`、`sync_members`、`syncs`、`sync_place`、`conversation_key`、`load_msg_origins`；`append_round(origin=…)`、`recent_authors(origin=…)`；`conversation_channels` 也列连通的频道。
- `Engine`：`sync_guard`（等正在跑的一轮、先写进删改；等完叫醒排在后面的点名；拿到锁以后照现在的组再算一遍要等哪几份，等的时候连通、断开、清空过就放开重新等，最多 `SYNC_GUARD_TRIES` 次；`extra` 可以是函数）、`sync_changed`（连进来的频道：缓冲区里连通之前的记进它自己那份，`_settle_before_link`；还没附上的“改成了什么”直接改进它自己那份；文件、链接搬到共用的那份。断开的频道：带走自己的“改成了什么”（那一行在它自己那份里是最近的：之后再改就直接改，那句作废，`_apply_fixes`；改回了记录里的样子也一样作废：连通组的记录里，还有那一行是最近的时候；频道自己那份里很早的一行照 2.7.1），组里那份直接改；散掉的组排着队的整理照样做完。返回 `scrub_cross` 拿掉了几条；开始跨好几个服务器时 `_label_music` 给旧的“在放什么”补上服务器）、`scrub_cross`（变成跨服务器时拿掉只有原来那边能看的内容，`render.drop_local`，它自己的回话里写过的卡、那段时间翻记录查人设卡的动作也拿掉：`_without_actions`、`actions.mentions_kinds`；跨服务器的连通自己会写的不动，再查一遍不改记录；连通、退出服务器组（正在跑的那一轮写完、放开锁之前再查一遍：`ConvState.scrub_due`、`_scrub_now`；怎么查也记在库里，那一轮写完、还没查就停了的话启动时照样查：`_scrub_syncs`；查的时候连同库里记着的、以前没查成的一起查：`Store.pending_scrub`）、启动时都会查；一次改完：`Store.update_turns`）、`_other_convs` / `_refresh_held`（删改也改到别的记录带着的文件、链接）、`sync_foreign_at`、`channels_cross`、`_absorb_others`、`_here`、`_sync_label`、`_sync_foreign`、`_past_digest`、`_channel_history`、`reset_linked`（等正在回的话说完再清空）、`_reset_linked`、`_drop_from_group`（别的频道排着队的整理、还没附上的“改成了什么”交给新的那份，`_salvage_jobs`、`_start_jobs`；散掉的组留下的那几批记在 `_orphans`，清空记忆时也拿掉这个频道的那部分；断开时它自己那份压缩前挑出来、还没整理完的交给新的那份接着整理；换成新的状态时沿用原来那把锁，等着旧的那份的，等的就是新的这份）、`_clean_reply`（跨服务器、卡各服务器分开时，记下的回话里不留写卡的动作）、同一个人刚发的文件、链接在连通组的记录里只读这一批里的、记进了这份记录的（`_auto_links`、`_auto_entries`：连通之前的闲聊、没跑成的那一轮的不读进来）、没问过模型就记进记录的那几轮（`ConvState.unvetted`：补进来的、连通时记进它自己那份的；清空、断开时跟着换成新的那份；也记在库里，改了就写：`_keep_unvetted`，重启以后照样认得）带着它们的请求被拒了就换成一句（`_withdraw_unvetted`、`prompts.PASSIVE_REJECTED`，附着的“改成了什么”只留一句“改过”：`EDIT_NOTE_LOST`），过了就算数（`_vetted`；它自己那份里的只有概要带全了才算：`ChannelState.past_vetted`），连通组的记录里直接改成新内容的那几条也一样（`ConvState.unvetted_edits`，被拒了当成删了），查完资料的那一步没问成（被拒了、DeepSeek 忙、超时……）时记下的点歌结果也一样、别的连通频道很早的消息被改了还没附上的那句跟着那边补进来的那段附上（记成那边的）、跨服务器的连通里改长期记忆的动作也不留在记下的回话里（连通组的记录压缩成摘要时，改长期记忆、查人设卡的动作也不进摘要）、带文件、链接的消息在连通组共用的那份里每个频道各留各的（`_cap_entries`）、整理记忆时每个频道的失败次数各数各的（`ConvState.review_bad_by`；等多久按整理失败了几次算，不按失败了几段算；压缩前挑出来的那批整理了它、又连回来时从头数）、连通组里同一个服务器的几个频道共用“在放什么”和能翻的频道名单（`_mine` 按服务器挑）、连通组的记录压缩成摘要时一律叫它“〔本 bot〕”（`prompts.SYNC_BOT_NAME`：它在几个服务器里的昵称可能不一样；群友的名字里写不出〔〕）、散掉的组留下的那几批只记它们自己有的消息被删了、`_drop_cleared`（清空记忆时，每份记录排着队的、正在整理的那几批里这个频道的那几段标成不整理：`_ReviewChunk.dropped`）、`_park_asking`（频道自己那份安静时整理正在问的那一段里压掉的那几条另外记下，`ConvState.review_parked`；只在断开时交给新的那份）、`_edit_hidden`（连通组的记录里，频道变成私密的、被关掉了时改过的消息当成删了，别的 bot 改的也不看；还没处理完的那条：`_hide_unprocessed`，缓冲区里拿掉、还没编号的这一轮也不写：`ChannelState.withdrawn`；“当成删了”记在 `ConvState.hides`，之后公开时又改了照改；只在组里那份的，改过的样子记在它自己那份的 `ConvState.hidden_edits`，断开拿回来以后照着改）、`_scrub_crossed`（服务器组里连进来的服务器没开“跨服务器”：拿掉组外带来的近况，`render.drop_recents`：写着服务器的近况都拿掉；断开了的频道的服务器退出了服务器组：`scrub_outsiders`（连通时、启动时；找不到的照它自己那份记录记下的服务器算：`_place_former`、`Store.record_guild`），组里原来有的服务器退出了服务器组时整份查一遍（`scrub_cross(whole=True)`、`ConvState.scrub_whole`）；`brings_cross_off`：只看这次连进来的、组里原来没有的服务器）、`_review_frozen`（连通前那份没整理成的不记成整理过，组里整理时再试）；`_run` 拿到锁以后发现频道换了记录，就去拿现在那份的锁；删改（`_queue_fix`）改进有这条消息的每一份记录（`Store.message_keys`），已经断开的频道的消息在组里那份直接改；连通的频道变成私密的就暂停（`_sync_public`）。整理记忆按记录走：`_plan_review` 按 `origin` 分段，每段照那个频道的设置、记进那个服务器；连通组的记录每个频道归在一起整理（`_run_shared_review`、`_chunk_done`、`_reviewed_already`、`ConvState.review_done` / `review_asking` / `review_cut` / `review_current`、`_ReviewChunk.reviewed` / `part_of`；频道自己那份整理完一段也记在那一段上，断开时交出去的那几批只看这个），一个频道失败只跳过它那部分（压缩前挑出来的那批失败了不算进失败次数），安静时整理正在问的那一段要压掉的那几条另外分段排进去（`_asking_part`；那一段问完了就跳过），某个服务器超了预算只是它那部分先不整理，已经断开的频道那部分在它自己那份里整理（问到一半断开的，那一段的结果不照着改：`_review_chunk` 返回 "left"）；等锁的时候记录换掉了的整理、改删不再做；`_ReviewJob.frozen`、`_ReviewChunk.authors`。`_other_server_named` / `_here_named` / `_sees_other_servers` 多了频道（这个频道和别的服务器的频道连通了，也算看得到别的服务器的名字）。
- `render_round(where=…)`（时间后面写明是哪个频道的，写成“#大厅（「某某」服务器）”）、`kind="passive"`、`drop_local`、`label_music`；`prompts.SYNC_BLOCK`、`SYNC_FOREIGN`、`SYNC_FOREIGN_CARDS`、`RECALL_MISS`、`RECALL_AMBIGUOUS`、`SYNC_PAST_BLOCK`、`SUMMARY_BLOCK_SYNC`、`SUMMARY_SYNC_NOTE`、`HINT_PASSIVE`、`MUSIC_NOW_AT`、`MUSIC_IDLE_AT`、`RECALL_SYNC_OFF`、`RECALL_NO_SHARE`、`WHO_SYNC_OFF`；`build_system_prompt(sync=…, sync_here=…, sync_past=…, sync_foreign=…, sync_cards=…)`；`mentions.link(known_server=…)`（只写了“「X」#频道”、X 是 bot 知道的别的服务器时，不换成这里的链接）。
- `/dsowner chatsync`：`commands.sync_command`、`ConfirmSync`、`do_sync_link`、`do_sync_unlink`（说明、结果照断开以后的组写；自己那份被旧版本写过的频道照实说拿不回来，`SYNC_NOTICE_LEFT_STALE`）；`/ds 清空记忆` 在连通（或以前连通过）的频道里先 `defer`，再 `await engine.reset_linked`；`start_groups` 启动时也查一遍跨服务器的连通组（`_scrub_syncs`）。
- 测试共 1364 个（新增 `tests/test_chatsync*.py`）。

### English

**Added**

- **Linked channels** (Beta): the new owner command `/dsowner chatsync` links text channels in several servers (or in the same server) so they share one chat record:
  - **One conversation**: wherever the bot is called, it sees what was said in all linked channels; each part of the record says which server's which channel it came from, and the message numbers and older summary are shared too. When it's called, messages in the other linked channels that aren't in the record yet are added first (no extra model call, no cost, and it doesn't reply there); a message calling the bot over there is left for that channel's own turn. If those added messages get the request rejected by DeepSeek (content filter, for example), they're replaced by a short note, so the linked channels don't get stuck (a recent message edited into rejected content counts as deleted there).
  - **It only speaks here**: the bot talks and reacts only in the channel where it was called (or speaks up on its own); it answers another channel's message here, without a reply link.
  - **Each keeps its own**: persona, settings, budget, emojis and stickers, persona cards and long-term memory stay per server (or server group). @-mentions only match people in this server, and another server's channel isn't turned into a link to a same-named channel here. Memory review writes each part into the cards and memory of the server it came from.
  - **Safeguards**: only the bot owner can link, and only public text channels; the bot posts a notice in the affected channels when linking and unlinking. Each channel's chat from before linking isn't shown to the others (the channel itself still remembers it, as a short digest; chatter not yet recorded at the moment of linking goes into the channel's own record too). Nothing from DMs (DM memory, recent DM talk) is brought into linked channels. A linked channel that is later made private is paused: the bot ignores it until the owner unlinks it or it's public again, and notices and lists only show the name it had when it was linked (not its current name); a message edited there meanwhile counts as deleted in the shared record, and a message still waiting there is dropped (the same for a linked channel switched off with `/ds 区域`, and for a channel that turned private after unlinking); the channel's own record still gets the edit (for something said while linked, once the channel unlinks and takes its part back).
  - **Links across servers** (channels not all in one server; a server group counts as one): the shared record only gets what's said in those channels. Recalling other channels and looking up persona cards by name are off there, recent talk from elsewhere isn't brought in, and @-mentions only match people who spoke or were @-mentioned in this channel. Persona cards are attached only when cards are shared by all servers (with per-server cards the bot doesn't change cards there either; members use `/ds-rp`), and when it edits long-term memory the stored reply doesn't keep the entry's text, and the "now playing" line names its server. Links within one server keep these as usual.
  - **Becoming cross-server**: when a group that was all in one server (or server group) gets a channel from another server, or one of its servers leaves its server group, whatever only that side could see is taken out of the shared record first (recent talk brought from other channels, the list of recallable channels, recall results, persona cards looked up by name, and, when cards are per server, attached persona cards and the cards the bot wrote). The link preview says so. "Now playing" lines written while only one server was linked get their server added. When a server leaves the server group while the linked channels stay within one group, the whole shared record is checked the same way (including what channels that already unlinked left there).
  - **Linking channels of servers in one server group**: the "now playing" line names its server there too, and recent talk from servers outside the server group is brought in only if all of those servers have 跨服务器 on. When the joining channel's server isn't in the link yet and has 跨服务器 off, recent talk that names a server (in the server group or not) already in the shared record is taken out first (the preview says so).
  - **Joining an existing group**: the newcomer also sees the group's existing shared record (the preview and the notices say so).
  - **Link** (`/dsowner chatsync 连通 频道:… 连到:…`; pick channels from the list, filter by server or channel name, or use # / type the channel name), **unlink** (`/dsowner chatsync 断开 频道:…`: the channel takes back its part from this link, not the others' and not the shared summary; a group of one dissolves) and **list** (`/dsowner chatsync 查看`). Linking and unlinking explain what will happen and need a confirmation click; they wait until the bot finishes what it's saying.
  - **Clearing**: `/ds 清空记忆` in a linked channel clears only what that channel said (with the shared summary) and its pre-link record; the other channels keep theirs. In a channel that was linked before, its part left in that group's record is removed too (with that group's summary, if its talk was already compacted into it). Deleted and edited messages sync as usual, also after unlinking.
  - **Memory review**: each channel's chat is reviewed separately, so an error or an exhausted budget in one server doesn't hold up the others, and what was reviewed isn't reviewed again after unlinking or a restart. When a channel whose own record an older version wrote into is unlinked and the group remains, its chat from the link stays in the group and is reviewed there (if the group dissolves, it's deleted with the shared record).
  - `/ds 状态` shows which channels this one is linked with (and, for links across servers, what's off). The shared record grows faster, so it costs somewhat more than separate chats.

**Improved**

- **Channel links**: when the bot is in several servers and writes "「X」#大厅" (without the word 服务器) where X is another server it's in, that's no longer turned into a link to this server's #大厅 (before, only "「X」服务器的 #大厅" and "#大厅（「X」服务器）" were recognized). Other words in 「」 ("「必看」#公告") still link as usual.

**Upgrade notes**

- Only the `dsbot` folder changed: replace it, then `sudo docker compose up -d --build`. To check the new version is running, look for `dsbot 2.8.0 启动中` in the log.
- The database upgrades itself: `dsbot.sqlite3` gets six tables (`chat_syncs`, `chat_sync_seq`, `chat_sync_summary`, `chat_sync_kept`, `chat_unvetted`, `chat_sync_scrub`), two columns in `turns` (`origin`, `reviewed`), an `origin` column in `msgmap` and a `took_back` column in `conversations`. Existing data is untouched; without linked channels nothing changes, and 2.7.x can still read and write the upgraded file. To go back to a version before 2.8.0 after using linked channels, unlink them all first (older versions don't know linked groups and won't see the chat from while they were linked); in channels that took their part back when unlinking, deleting or editing very old messages may no longer reach the record after going back (older versions keep only the latest 3000 message numbers, and the taken-back numbers skip ahead), so clear those channels first with `/ds 清空记忆` if that matters.
- `/dsowner 服务器组`'s list / link / leave are now three subcommands (`/dsowner 服务器组 连起来 服务器:… 连到:…`, every field they need is required, so phones and tablets send them).
- `/dsowner` gains `chatsync`: Discord updates the command list after the restart (it may take a minute or two, or reopening Discord).
- The English edition wasn't updated this time and stays at v2.0.0.

**Development**

- The state of one chat record in the engine (the record, message numbers, files and links read, pending deletes and edits, attached persona cards, memory-review progress, the lock and `generation`) moved from `ChannelState` into the new `ConvState` (`Engine.convs`, keyed by record ID); `st.xxx` still works on `ChannelState` (`_Shared` forwards to `st.conv`). An unlinked channel has its own, as before; linked channels share one (keyed by the group's negative ID). `generation` is now a globally unique number, so switching records never collides with an old one.
- `Store`: `chat_syncs` (with `joined_turn`, `joined_sid`, `joined_next`: unlinking takes back only this link's part, and nothing if an older version wrote into the frozen record) / `chat_sync_seq` / `chat_sync_summary` (which channels' talk a group's summary holds: written by `compact`, read by `origin_syncs`) / `chat_sync_kept` (chat that couldn't be taken back and is reviewed in the group; included by `sync_joined_turns`) / `chat_unvetted` (each record's turns and in-place edits not yet carried by an accepted request: `load_unvetted`, `save_unvetted`; removed when the record is cleared or the group dissolves) / `chat_sync_scrub` (how the round in flight at a server-group leave is to be checked once it has written: `mark_scrub`, `pending_scrubs`, `clear_scrub`); `turns.origin`, `turns.reviewed` (turns of a shared record whose chunk was reviewed, `mark_reviewed`, `reviewed_turns`; take-back copies the marks and moves the watermark only over a fully reviewed prefix), `msgmap.origin`, `conversations.took_back`; shared records and records that took back trim their number map by row count (their numbering can jump); an index on `msgmap(message_id)`; `message_keys`, `origin_syncs`, `next_short_id`, `stale_syncs`, `turn_ids`, `record_turns`, `msg_origins`; `link_channels`, `unlink_channel` (the leaving channel copies its part back, `_take_back`), `drop_origin` (for a channel that left: the summary goes only if its talk was compacted into it), `raise_join_marks`, `sync_joined_turns`, `sync_of`, `sync_members`, `syncs`, `sync_place`, `conversation_key`, `load_msg_origins`; `append_round(origin=…)`, `recent_authors(origin=…)`; `conversation_channels` also lists linked channels.
- `Engine`: `sync_guard` (waits for a running turn, writes pending deletes and edits first, then wakes mentions that queued behind it; once it holds the locks it recomputes which records it needs from the current groups and, if a link, unlink or clear finished meanwhile, releases them and waits again, at most `SYNC_GUARD_TRIES` times; `extra` may be a callable), `sync_changed` (a joining channel's pending pre-link messages go into its own record, `_settle_before_link`; its pending edit notes are written into its own record in place; its files and links move to the shared state. A leaving channel takes its edit notes along (its line is recent in its own record, so a later edit rewrites it in place and drops the note: `_apply_fixes`; an edit back to the recorded text drops it too, in a group record or when the line is recent; an old line of a channel's own record keeps the 2.7.1 behaviour) and the group record is rewritten in place; a dissolved group's queued reviews still finish. Returns how many turns `scrub_cross` changed; when a link starts spanning servers, `_label_music` adds the server to old "now playing" lines), `scrub_cross` (takes out what only one side could see when a group becomes cross-server, `render.drop_local`, including cards the bot wrote and, from that period, its recall/who actions in its replies, `_without_actions`, `actions.mentions_kinds`; never removes what a cross-server link writes itself, so scrubbing again changes nothing; checked on linking, on leaving a server group (again by the round in flight before it releases the lock: `ConvState.scrub_due`, `_scrub_now`; that check is also stored, so if the bot stops after the round wrote and before it checked, `_scrub_syncs` runs it at startup; a check also runs what an earlier failed one left stored, `Store.pending_scrub`) and at startup; all changes in one transaction: `Store.update_turns`), `_other_convs` / `_refresh_held` (deletes and edits also reach the file/link entries of other records), `sync_foreign_at`, `channels_cross`, `_absorb_others`, `_here`, `_sync_label`, `_sync_foreign`, `_past_digest`, `_channel_history`, `reset_linked` (waits before clearing), `_reset_linked`, `_drop_from_group` (the other channels' queued reviews and edit notes move to the fresh state, `_salvage_jobs`, `_start_jobs`; a dissolved group's pending reviews are kept in `_orphans`, so clearing a channel removes its part there too; a leaving channel's own pending compaction reviews move to its fresh record; a replacement state keeps the old lock object, so whoever waited on the old record waits on the new one), `_clean_reply` (in a cross-server link with per-server cards, card actions never reach the stored reply), same-author auto-reads in a group record only take messages of this round or recorded in it (`_auto_links`, `_auto_entries`: no pre-link chatter, nothing left over from a failed round), turns written without a model call (`ConvState.unvetted`: absorbed chatter, chatter settled at linking; carried across clears and unlinking; also written to the database on every change by `_keep_unvetted`, so a restart doesn't forget them) are replaced by a note when the request carrying them is rejected (`_withdraw_unvetted`, `prompts.PASSIVE_REJECTED`; an edit note inside keeps only a content-free line, `EDIT_NOTE_LOST`) and count as accepted once one succeeds (`_vetted`; settled chatter only if the request's pre-link digest carried all of it, `ChannelState.past_vetted`); lines a group record rewrote in place for an edit are tracked the same way (`ConvState.unvetted_edits`, turned into deleted lines on a rejection), and so is a music result written when the tool step fails (rejected, busy, timed out...), another linked channel's pending edit notes go out with its absorbed chatter (recorded as its own), memory actions are kept out of stored replies in cross-server links (and memory/who actions out of a group record's summary input), the file/link caches of a group record are per channel (`_cap_entries`), review failures are counted per channel (`ConvState.review_bad_by`; the backoff counts failed runs, not failed parts; a compaction review of that part or a rejoin starts the count over), linked channels of one server share the "now playing" line and the channel list (`_mine` picks by server), a group record's summary calls the bot "〔本 bot〕" (`prompts.SYNC_BOT_NAME`: its nickname can differ per server, and no member name can contain 〔〕), orphans only record deletes of messages they hold, `_drop_cleared` (clearing a channel marks its chunks in every pending review as not to be reviewed: `_ReviewChunk.dropped`), `_park_asking` (the compacted part of the chunk a channel's own quiet review is asking is kept aside in `ConvState.review_parked` and handed to the fresh record only on leave), `_edit_hidden` (in group records, an edit made while the channel is private or switched off counts as a delete; for a message not processed yet: `_hide_unprocessed`, which also takes it out of the buffer and out of a round that hasn't numbered it (`ChannelState.withdrawn`); other bots' updates there are ignored; such pseudo-deletes are marked in `ConvState.hides`, so a later public edit still goes through; for a message only in the group record, the edit is kept in its own record's `ConvState.hidden_edits` and applied after the take-back), `_scrub_crossed` (a grouped join by a server with 跨服务器 off removes recent talk from outside the group: `render.drop_recents`, which now drops every block naming a server; `scrub_outsiders` scrubs what an unlinked channel left in a record once its server leaves the server group (on links and at startup; a channel it can't locate is placed by its own record's guild: `_place_former`, `Store.record_guild`), and a server leaving the server group of a still-grouped link scrubs the whole record (`scrub_cross(whole=True)`, `ConvState.scrub_whole`); `brings_cross_off`: only servers the joining channels bring in that weren't there), `_review_frozen` (a pre-link record that couldn't be reviewed isn't marked reviewed; retried during the group's reviews). `_run` re-takes the lock of the current record if the channel switched records while it waited; deletes and edits (`_queue_fix`) go into every record holding the message (`Store.message_keys`), and a group record rewrites in place the messages of channels that left; a linked channel that turned non-public is paused (`_sync_public`). Memory review works per record: `_plan_review` splits by `origin`, and each part uses that channel's settings and its server's scope; a shared record groups each channel's chat together (`_run_shared_review`, `_chunk_done`, `_reviewed_already`, `ConvState.review_done` / `review_asking` / `review_cut` / `review_current`, `_ReviewChunk.reviewed` / `part_of`; a channel's own record also marks each chunk it finishes, and the reviews handed on at unlinking go by that alone); a failing channel only skips its own part (failures in compaction reviews don't count), the compacted part of the chunk a quiet review is asking goes into the compaction's review too (`_asking_part`; skipped there once that chunk is done), a server over budget only delays its own part, and channels that left are reviewed in their own record (an answer for a channel that left meanwhile isn't applied: `_review_chunk` returns "left"); reviews and edits whose record was replaced while they waited stop; `_ReviewJob.frozen`, `_ReviewChunk.authors`. `_other_server_named` / `_here_named` / `_sees_other_servers` take the channel (a channel linked with another server's channel also counts as seeing other servers' names).
- `render_round(where=…)` (labels which channel a part came from after the time, as "#大厅（「X」服务器）"), `kind="passive"`, `drop_local`, `label_music`; `prompts.SYNC_BLOCK`, `SYNC_FOREIGN`, `SYNC_FOREIGN_CARDS`, `RECALL_MISS`, `RECALL_AMBIGUOUS`, `SYNC_PAST_BLOCK`, `SUMMARY_BLOCK_SYNC`, `SUMMARY_SYNC_NOTE`, `HINT_PASSIVE`, `MUSIC_NOW_AT`, `MUSIC_IDLE_AT`, `RECALL_SYNC_OFF`, `RECALL_NO_SHARE`, `WHO_SYNC_OFF`; `build_system_prompt(sync=…, sync_here=…, sync_past=…, sync_foreign=…, sync_cards=…)`; `mentions.link(known_server=…)` (a bare "「X」#channel" where X is another server the bot knows isn't linked here).
- `/dsowner chatsync`: `commands.sync_command`, `ConfirmSync`, `do_sync_link`, `do_sync_unlink` (notices and the result follow the membership after unlinking; a channel whose own record an older version wrote into is told its chat can't be taken back, `SYNC_NOTICE_LEFT_STALE`); `/ds 清空记忆` in a linked (or previously linked) channel defers, then awaits `engine.reset_linked`; `start_groups` also checks cross-server groups at startup (`_scrub_syncs`).
- 1364 tests in total (new `tests/test_chatsync*.py`).

## [2.7.1] - 2026-11-09

> 英文版这次没有更新，还是 2.0.0。
> The English edition wasn't updated this time and stays at 2.0.0.

### 中文

**修复**

- **频道链接**：2.7.0 起，它话里写明“「某某」服务器的 #频道”时，不换成频道链接（免得链到这里同名的频道）。但这个服务器的名字带【】之类的符号、或者长得被截短时，它写的明明是这里，也不换了；“「生存」服务器的 #公告”这种游戏里的服务器也不换。现在：
  - 写的是这个服务器（全名，或者截短、中和过的名字）：照常换。这里和组里的服务器（开了跨服务器时，还有也开了跨服务器的服务器）同名、分不出是哪个时不换；它自己截短（带着“…”）或者截到 40 字的名字，连了组、开了跨服务器或者共用人设卡时，bot 知道的别的服务器也对得上就不换（聊天里给它看的、截到 60 字的照常换）。
  - 写的是 bot 在的别的服务器、组里的、最近有人在那边跟它聊过的服务器：不换。
  - 认不出是哪个服务器的：这个服务器没连组、没开跨服务器、不共用人设卡时（它看不到别的服务器的名字）照常换；连了组、开了跨服务器或者共用人设卡（卡上会写着别的服务器）就不换（名字写得和它看到的不一样时，也不会链到这里）。
- **`/ds-dm 记忆`**：私信记忆记得很多、一次发不完时，2.7.0 比以前少列了大约 9 条。现在和 `/ds 长期记忆` 一样，每条截短，编号全部列出来（几千条时只列编号也发不完，最后会说还有几段没发出来）；查看时选上编号，能看那一条的全文。
- **`/ds 长期记忆`**：条目多到每条截短也放不下时（调高了上限，或者组里服务器很多、名字很长），2.7.0 后面的编号会漏掉（2.7.0 的更新日志说“编号照样全部列出来”，这种情况下其实做不到）。现在放不下的也会列出编号（分成几行，一个编号不会被切到两条消息里；几千条时只列编号也发不完，最后会说还有几段没发出来，那些编号在「编号」一栏输入还找得到）；截短时，标着是哪个服务器记的那部分也截短。查看时选上编号，能看那一条的全文。
- **服务器组**（Beta）：
  - 组里两个服务器同名（或者名字很长、截短以后一样）时，翻“#大厅（「某某」服务器）”会翻成这里同名的频道。现在先照名单里的写法认，翻的是名单里那个；抄的时候丢了截短的“…”也一样。那边有两个同名的频道时，和这里一样认第一个。
  - 组散了以后又连成一组，所有长期记忆都会算成“连到”那边记的：加入方自己记的，它整理时改不了也删不了。现在退出组时带走的那份照样记着是谁记的，再连成一组时，记的那个服务器也在组里的还算它的，不在的算带进来的那个服务器的（那个服务器后来再加进来也不改回去）；没在组里时这边改过的那条，算这边记的。连到一半断了、再连一次时也一样。
  - 连组、退组出错时的提示说“下次会接着做完”，可新建的组只连上了一个服务器的，下次是先散掉。现在提示里写明了：要连的话再连一次。
  - 长期记忆正好记满（50 条）、里面又有组里别的服务器记的时：
    - 聊天里叫它记新的，它可能为了腾地方删掉别的服务器记的条目；
    - 整理记忆时它被要求“快满了就合并、删掉旧的”，可别的服务器记的它删不了，就一直记不进新的，也没有提示。

    现在聊天时不会为了腾地方删条目（会说记满了），整理时只合并、删掉这个服务器自己记的；`/ds 长期记忆` 会提醒记满了，连组的预览里也会说并完正好到上限。
- **所有服务器共用一张人设卡**：启动时接着做（检查）上次的连组、退组出错的话（比如数据库正被别的程序占着），2.7.0 照样换共用方式：上次有没做完的，没并进组的卡会直接并成共用的，组里删掉的卡可能被顶回来。现在先不换，还按原来的用，下次启动再换。

**升级须知**

- 只有 `dsbot` 文件夹变了：换掉它，再 `sudo docker compose up -d --build`。确认新版生效：日志里有 `dsbot 2.7.1 启动中`。数据库不变。
- 英文版这次没有更新，还是 v2.0.0。

**开发**

- `mentions.link` 的参数 `here` 换成 `other_server`（写明的“「X」服务器”能不能当成这里）。`Engine._other_server_named`：X 是这里（`_here_named`：全名、中和过、截到 60 字的；它自己截短、带着“…”的，或者截到 40 字的也认，只是连了组、开了跨服务器、共用人设卡时，bot 知道的别的服务器也对得上、或者正好是它的全名就不算）就换，和给它看过的别的服务器同名时不换；X 是 bot 知道的别的服务器（transport 的 `guild_ids()`、组里的、`interactions` 里的，按 `_guilds_named` 认）不换；都认不出时，它看得到别的服务器的名字（`_sees_other_servers`：连了组、开了跨服务器、共用人设卡）就不换。
- `Engine._find_source`：写明了服务器的，先照 `_source_name` 的写法原样对（同一个服务器里同名的频道认第一个）；对不上再找服务器时，这里只按 `_here_named` 认。
- `MemoryStore.copy_scope` 保留 `origin`；`MemoryStore.merge_scope(src, dst, members)`：记的服务器在 `members`（连完以后组里的服务器）里的照旧，不知道是谁记的、不在里面的算 `src` 的。`groups._merge` / `_sweep` 传进连完以后的成员；`_sweep` 扫只有一个服务器的组（连到一半断了，接着就散掉）时，记着是谁记的都留着（不知道是谁记的算它的）。`MemoryStore.edit` 改服务器自己名下（没连组）的条目时，`origin` 改成这个服务器。
- `prompts.MEMORY_FULL`、`REVIEW_MEMORY_FULL`；`build_system_prompt(memory_foreign=…)`（只在连成组、正好记满时用上，没连组时提示词不变）。
- `commands.fit_listing`（`/ds 长期记忆`、`/ds-dm 记忆` 共用；只列编号的分成几行（`numbers_lines`），每行最多 `NUMBERS_LINE` 字；前面列几条先二分着找，再往后试最多 120 条）；`memory_entry_text`、`dm_memory_entry_text`（查看时选了编号）；`commands.start_groups`（启动时先 `recover_groups`，成功了才 `apply_card_mode`）。
- 补上改坏了也没有测试会失败的地方：
  - 共用卡时，卡上的名字不跟着昵称变；
  - `/ds-rp` 会记下他在哪个服务器出现过；
  - 一轮里说话的人多、卡附不完时，没附上卡的也记成出现过；
  - 加入方忘掉了的卡，旧版本排在组里自己的旧版本后面；
  - 退出复制到一半时，那份副本不会被扫回组里；
  - 别的服务器连组时，不打断这边改记忆、整理记忆；
  - 退出没做完时，不带走他在别处重写的卡；
  - 第三个服务器加进组时，这边照样改得了记忆；
  - 启动时真的会先接着做完连组、退组再换共用方式（建一个真的 `DSBot` 试）。
- 测试共 1042 个。

### English

**Fixed**

- **Channel links**: since 2.7.0, a channel the bot mentions as "「X」服务器的 #channel" (X's #channel) isn't turned into a channel link, so it can't point at this server's channel of the same name. But when this server's own name contains symbols like 【】, or is long enough to be cut short, the bot was writing about this very server and the link was still dropped. A game server ("「生存」服务器的 #公告", the survival server's #announcements) lost its link too. Now:
  - this server (its full name, or a shortened or neutralized form of it) is linked. It isn't linked when a server in the group (or, with cross-server on, another server that also has it on) has the same name, since the bot can't tell which one is meant; nor is a name the bot shortened itself (ending in "…") or cut to 40 characters, when it also matches another server the bot knows and this server is in a group, has cross-server on, or uses shared cards (the 60-character form shown in chat is still linked);
  - another server the bot is in, a server in the same group, or a server where someone talked to the bot recently is not linked;
  - a server name the bot can't recognize is linked if this server isn't in a group, has cross-server off and doesn't use shared cards (the bot sees no other server's name); in a group, with cross-server on or with shared cards (cards mention other servers) it stays plain text, so a misspelled name of another server can't link here either.
- **`/ds-dm 记忆`** (DM memory): with a very large DM memory that doesn't fit in one reply, 2.7.0 listed about 9 fewer entries than before. Like `/ds 长期记忆`, it now shortens each entry and lists every number (with several thousand entries even the numbers don't fit; the reply then says how many parts were left out). Picking a number when viewing shows that entry in full.
- **`/ds 长期记忆`** (long-term memory): when the entries didn't fit even after shortening each one (a raised limit, or many linked servers with long names), 2.7.0 dropped the last numbers. Its changelog said every number would still be listed, which wasn't true there. The numbers that don't fit are now listed too, on several lines so no number is split across two messages (with several thousand entries even the numbers don't fit; the reply then says how many parts were left out, and those numbers can still be found by typing in the 编号 field), and the "written by" server names are shortened along with the entries. Picking a number when viewing shows that entry in full.
- **Server groups** (beta):
  - When two servers in a group have the same name (or long names that look the same once cut short), asking for "#大厅（「X」服务器）" (X's #lobby) read this server's channel of the same name. The exact label from the list is now matched first, so it reads the channel from the list, also when the trailing "…" of a shortened name was dropped. When that server has two channels with the same name, the first one is used, as for this server's own channels.
  - After a group broke up and servers were linked again, all long-term memory counted as written by the server linked *to*. The joining server's own entries became read-only for its memory reviews. A leaving server's copy now keeps who wrote each entry. When servers are linked again, an entry still belongs to its writer if the writer is in the new group; otherwise it counts as written by the server whose copy brought it in, and stays that way if the writer joins later. An entry a server edited while it was on its own counts as that server's. This also holds when a link was interrupted and run again.
  - When linking or leaving failed, the message said the rest would be finished next time. A new group that only one server had joined is undone instead, so the message now says to link again.
  - When long-term memory was exactly full (50 entries) and some entries were written by other servers in the group:
    - asked in chat to remember something new, the bot could delete another server's entry to make room;
    - its memory review was told "nearly full, merge and delete old ones", but it can't delete other servers' entries, so it never recorded anything new, with no warning.

    Now chat doesn't delete entries to make room (it says memory is full), and reviews only merge or delete this server's own entries. `/ds 长期记忆` warns that memory is full, and the link preview says when the merge reaches the limit exactly.
- **One profile card per person across all servers**: if finishing (or checking for) unfinished group operations failed at startup (for example because another program had the database locked), 2.7.0 switched the card mode anyway. If something was unfinished, a card not yet merged into its group then went straight into the shared card, and a card deleted in the group could come back. The bot now keeps the current mode and switches on the next start.

**Upgrading**

- Only the `dsbot` folder changed: replace it, then run `sudo docker compose up -d --build`. To check the new version took effect: the log shows `dsbot 2.7.1 启动中`. The databases don't change.
- The English edition wasn't updated this time and stays at v2.0.0.

**Development**

- `mentions.link` takes `other_server` instead of `here`: it decides whether a named "「X」服务器" can be taken for this server. `Engine._other_server_named` links when X is this server (`_here_named`: full, neutralized, or cut to 60 characters; a name the model shortened itself, ending in "…", or cut to 40 characters, counts too, except that in a group, with cross-server on or with shared cards it doesn't when another server the bot knows also matches, nor when it is exactly another known server's full name), unless X is also the name of another server shown to the model. It doesn't link when X is another server the bot knows (the transport's `guild_ids()`, the group, `interactions`, matched through `_guilds_named`). When X is neither, it doesn't link when the model can see other servers' names (`_sees_other_servers`: in a group, with cross-server on, or with shared cards).
- `Engine._find_source`: a channel named with its server is first matched exactly against `_source_name` labels (the first of same-named channels in one server). Otherwise, this server is matched only through `_here_named`.
- `MemoryStore.copy_scope` keeps `origin`. In `MemoryStore.merge_scope(src, dst, members)`, an entry keeps its origin if it is in `members` (the group's servers after the link); unknown origins and servers outside the group become `src`. `groups._merge` and `_sweep` pass the members after the link; `_sweep` keeps who wrote each entry for a one-server group (a half-done link that is undone right after; unknown writers become that server). `MemoryStore.edit` sets `origin` to the server when it edits the server's own (ungrouped) memory.
- `prompts.MEMORY_FULL`, `REVIEW_MEMORY_FULL` and `build_system_prompt(memory_foreign=…)`. These are only used in a group at exactly the limit, so prompts outside groups are unchanged.
- `commands.fit_listing`, shared by `/ds 长期记忆` and `/ds-dm 记忆` (numbers that don't fit go on lines of at most `NUMBERS_LINE` characters, built by `numbers_lines`; how many entries keep their text is found by a binary search, then up to 120 more are tried). `memory_entry_text` and `dm_memory_entry_text` show one entry when a number is picked. `commands.start_groups` runs `recover_groups` at startup and calls `apply_card_mode` only if it succeeded.
- Tests now catch these behaviours being broken; before, none failed:
  - with shared cards, a card's name doesn't follow nicknames;
  - `/ds-rp` records which server the person was seen in;
  - when more people talk in one round than cards can be attached, everyone still counts as seen;
  - a joining server's forgotten card keeps its old versions behind the group's own;
  - a half-made copy of a leaving server isn't swept back into the group;
  - another server linking doesn't interrupt this server's memory edits and reviews;
  - an unfinished leave doesn't take a card rewritten elsewhere;
  - this server can still edit its memory while a third server joins the group;
  - a real `DSBot` finishes unfinished group operations before switching the card mode at startup.
- There are now 1042 tests.

## [2.7.0] - 2026-11-08

> 英文版这次没有更新，还是 2.0.0。
> The English edition wasn't updated this time and stays at 2.0.0.

### 中文

**新增**

- **服务器组**（Beta）：bot 主人用新命令 `/dsowner 服务器组`，把同一群人的几个服务器（比如主服加游戏服）连成一组，当成一个服务器来记：
  - **共用一份人设卡和长期记忆**：在哪个服务器记下的，组里每个服务器都用得上；`/ds-rp`、`/ds 长期记忆` 看的、改的都是组里那份。
  - **能互相翻记录**：开了记忆共享的频道，也能翻组里别的服务器里开了共享的频道（名单里写着是哪个服务器）；两边的频道都开了共享时，在 A 服务器跟它聊过的，到 B 服务器叫它时也接得上。私密频道的规矩不变。
  - **私信也一样**：私信挂在组里哪个服务器上，组里的服务器都算“私信挂着的那个服务器”。
  - **各管各的**：设置、预算、人设、表情包库、点歌、“跨服务器”还是每个服务器自己的。
  - **连起来**（`操作:连起来`，服务器从列表里选）：加入的服务器的长期记忆并进组里，接在后面、重新编号（一样的不重复记）。同一个人两边都有卡的，用组里那张，加入方那张存成旧版本，本人用 `/ds-rp 操作:改回上一版` 能换过去、再换回来（换回来以后加入方那张就不留了）；组里删掉了的卡（删得比加入方那张新的），不会被加入方那张顶上。
  - **退出**（`操作:退出`）：带走一份组里的长期记忆，和在这个服务器出现过的人的人设卡，之后各记各的；组里只剩一个服务器时组就散了。`操作:查看` 列出有哪些组。连起来、退出之前会先说清楚会怎样（并进几条记忆、几个人两边都有卡、编号会不会变），点确认才办。
  - **谁记的谁整理**：每条长期记忆记着是哪个服务器记的。它自己整理记忆时只改、删这个服务器记的，组里别的服务器记的只给看（有人在聊天里明确叫它改的照常改）。连起来以后超过 50 条时，它不会为了腾地方删条目，要管理员删到 49 条以内才能再记新的。
  - **名字**：按名字查卡时，同一个人在组里几个服务器的昵称都认得，先认在这里出现过的人；组里别的服务器同名的人，查到时会写明他还没在这个服务器出现过。@ 人只认在这个服务器出现过的人，按他在这里的昵称。写明了是哪个服务器的频道（“#大厅（「B」服务器）”）只在那个服务器里找，不会翻成、链接到这里同名的频道。
  - **断电也能接着做完**：连组、退组时三个数据库文件每一步都等写进磁盘；做到一半断了，下次执行 `/dsowner 服务器组` 或者重启时接着做完。散掉的组还剩数据时（多半是只还原了其中一两个文件的备份），不删，在日志和 `操作:查看` 里提醒。
  - `/ds 状态`、`/ds 长期记忆`、`/ds-dm 状态`、第一次私信的说明和 `/dsowner 总览` 都会写出和谁连成了一组。
- **所有服务器共用一张人设卡**（默认关）：`config.toml` 的 `[bot]` 新增 `shared_cards`。改成 `true` 并重建容器后，每个人在所有服务器和私信里只有一张卡，跟着他走：在 A 服务器叫它“记住我是猫娘”，到 B 服务器它也知道。长期记忆不受影响。
  - **打开时**自动把各服务器的卡并成每人一张：用最近改过的那张，别的存成旧版本（`/ds-rp 操作:改回上一版` 能换过去、再换回来；换回来以后，换过去的那张就不留了），旧版本一共留 5 个。在哪个服务器清空了卡，只是那边没有卡了，别的服务器里还有的照样并进来；所有服务器共用时清空的、bot 主人删掉的，哪里的都不会并回来。日志里写并了几个人的卡。
  - **关掉时**把每个人的卡（连同旧版本）复制回他出现过的每个服务器（连成一组的放进组里），之后各改各的。
  - **查不到不相干的服务器的人**：按名字查卡只查在这个服务器（连成一组时也包括组里的服务器）出现过的人；@ 人、提示词里“有卡的人”只认在这里出现过的人，用他在这里的昵称。
  - 卡上可能有他在别的服务器、在私信里说的事，它会注意别把别处的私事拿出来说。第一次私信的说明、`/ds-dm 服务器`、`/ds-dm 状态`、`/ds-rp` 和 `/ds 状态` 都会说明卡是所有服务器共用的。
  - 这个设置记在 `cards.sqlite3` 里，切换在一次事务里做完；出错就还按原来的方式用（日志里会写），下次重启再换。
- **`CONTROL_GUILD_ID` 可以填几个服务器**：用逗号（中英文都行）、顿号、分号或空格隔开。每个里面都有 `/dsowner`，在哪个里面用都一样；某个服务器注册失败（比如 bot 不在里面）不影响别的。`0` 当没填，全角数字也认。

**改进**

- 私信里聊天时，不再把人设卡上的名字改成他在私信里的名字（Discord 的全局名字）。以前他私信一次，服务器里“有卡的人”就变成那个名字，按服务器里的昵称也查不到他的卡，要等他回服务器说话才改回来。
- `/ds 长期记忆`：删除时「编号」一栏会列出每条的内容（输入几个字能筛选），删完回复里写出删的是哪一条；记满了会说要删几条；条目多到一次发不完时，每条截短、编号照样全部列出来。
- 它话里写明是别的服务器的频道（比如“「A」服务器的 #大厅”），不再换成这个服务器同名频道的链接。

**升级须知**

- 只有 `dsbot` 文件夹变了：换掉它，再 `sudo docker compose up -d --build`。确认新版生效：日志里有 `dsbot 2.7.0 启动中`。
- 数据库会自动多几张表、几列。`config.toml` 里新加的 `shared_cards` 不加也行，默认 `false`（和以前一样，每个服务器一张卡）。
- 服务器组只有 bot 主人能连，不连就和以前一样。连起来之前最好停止项目、备份 `data/`，也跟各个服务器的人说一声；用过服务器组以后，`data/` 里的三个数据库文件要一起备份、一起还原。
- 打开 `shared_cards` 之前也最好停止项目、备份 `data/`，再跟大家说一声：卡上的事在所有服务器都会用上。改回 `false` 不会变回原来的样子：每个服务器拿到的都是共用的那张，原来各服务器的卡顶多还留在旧版本里（旧版本一共只留最近 5 个，共用期间卡多改几次就挤掉了，它整理记忆时改的也算）。
- 要换回旧版本：打开过 `shared_cards` 的，先改回 `false` 重启一次，日志里有“人设卡：改回每个服务器一张”了再换；连过服务器组的，先让服务器一个个退出、直到组散掉（旧版本不认这两样，共用的卡、组里的人设卡和长期记忆它都看不到）；`.env` 的 `CONTROL_GUILD_ID` 填了几个的，改回只填一个（旧版本只认一个，填几个会起不来）。
- 英文版这次没有更新，还是 v2.0.0。

**开发**

- 新模块 `groups.py`：`plan_link` / `link`、`plan_unlink` / `unlink`、`finish_leaving` / `recover`（每一步都能重做，做到一半断了能接着做完）、`durable`（组操作期间三个库都 `synchronous=FULL`）、`leftovers`（散掉的组还剩的数据，只报不删）。
- `store.py`：`server_groups`、`server_group_seq`、`server_group_leaving` 三张表，`Store.memory_scope` / `group_of` / `group_members` / `join_group` / `start_leaving` / `unmap` / `group_epoch` 等。组的 ID 是负数，组里共用的人设卡、长期记忆按它存在原来的 `cards.sqlite3`、`memory.sqlite3` 里。正常关闭时也关掉主库。
- `memory.py`：新列 `memories.origin`（哪个服务器记的）、`card_versions.merged`（合并进来的版本：改回它时，现在这张存成旧版本，见 `CardStore.swaps_on_restore`）；新表 `card_seen`（卡在组里哪些服务器用过、他在那里叫什么）、`card_meta`（`shared`）；组操作用的 `merge_scope` / `copy_scope` / `drop_scope` / `merge_preview` 等。共用卡：`SHARED = 0`，`CardStore.set_shared`（`_share` / `_split`，返回 `ShareChange`）、`for_groups()`（共用时交给组操作的是 `SharedCardMarks`，只搬删卡的记号）、`find_here(near=…)`、`owners_near`。改成共用、改回每个服务器一张时（`_combine`），只有 0 下面的删卡记号（所有服务器都删了）挡得住一张卡；连组时（`merge_scope`），组里自己的删卡记号照样算。
- `engine.py`：`_scope` / `_card_scope` / `_card_near`；`_memory_key`（记在哪，加上这个服务器连组、退组过几次：问模型期间变了，就不照着编号改，整理记忆马上重问）；`ChannelState.memory_shown`（没给它看过的编号不改）；`_foreign_memories`（组里别的服务器记的只给看）；`_source_name`（能翻记录的频道名单里，组里别的服务器的频道后面写上是哪个服务器）、`_find_source`（写明了服务器的频道只在那个服务器里找，服务器名用 `_guilds_named` 认）；`groups_changed`；`status()` 多了 `group`、`cards_shared`。
- `commands.py`：`/dsowner 服务器组`（`group_command`；`ConfirmGroup` 确认时照当时的情况再算一次，对不上就不办）、`recover_groups`、`apply_card_mode`（启动时先 `recover_groups`，再按 `config.toml` 换卡的模式，都在注册命令之前）、`memory_number_choices`、`dm_group_note` / `dm_warning`。
- `config.py`：`Secrets.control_guild_ids`（以前是 `control_guild_id`）、`parse_guild_ids`；`BotConfig.shared_cards`（认 true / false，也认“开”“关”、on / off、yes / no、1 / 0 这些写法；认不出的报错、不去猜，免得猜错了把卡并起来、分开）。
- `mentions.link` 多了 `here`；`render.render_who` 能在名字后面带一句说明，`render_review_input` 标出组里别的服务器记的条目。
- `prompts.py`：`GROUP_BLOCK`、`MEMORY_BLOCK_GROUP`、`MEMORY_OVER`、`CARDS_SHARED_BLOCK`、`REVIEW_GROUP` / `REVIEW_GROUP_SHARED` / `REVIEW_SHARED` / `REVIEW_MEMORY_FOREIGN`。“另外还有人有卡”只说有、不说几个：别处多一张卡，这里的提示词不变，缓存照样命中。没连组、没开 `shared_cards` 的服务器，提示词和 2.6.0 逐字一样（只是私信里聊天时不再改卡上的名字，见“改进”）。
- 测试共 1014 个（新增 `test_server_groups.py`、`test_shared_cards.py`）。

### English

**Added**

- **Server groups** (Beta): with the new `/dsowner 服务器组` command, the bot owner can link several servers used by the same people (say, a main server and a gaming server) into a group that's remembered as one server:
  - **Shared profile cards and long-term memory**: whatever is saved in one server is available in every server of the group; `/ds-rp` and `/ds 长期记忆` show and edit the group's copy.
  - **Shared history**: channels with memory sharing on can also look through the sharing channels in the group's other servers (the list says which server each one is in), and when both channels have sharing on, what someone talked about with it in server A carries over when they call it in server B. The rules for private channels don't change.
  - **DMs too**: whichever server in the group someone's DMs use, every server in the group counts as the server their DMs use.
  - **Still separate**: settings, budgets, persona, meme library, music and cross-server stay per server.
  - **Linking** (`操作:连起来`, pick the servers from the list): the joining server's long-term memory is merged into the group's, appended and renumbered (identical entries aren't duplicated). When someone has a card on both sides, the group's card is kept and the joining server's becomes an old version they can switch to and back with `/ds-rp 操作:改回上一版` (once they switch back, the joining server's card is gone); a card deleted in the group isn't brought back by the joining server's copy, unless that copy was changed after the deletion.
  - **Leaving** (`操作:退出`): the server takes a copy of the group's long-term memory and the cards of people who have appeared in it, and from then on keeps its own; a group with one server left dissolves. `操作:查看` lists the groups. Before linking or leaving, it explains what will happen (how many memory entries get merged, how many people have cards on both sides, whether numbers change) and only acts after you confirm.
  - **Each server tidies its own**: every long-term memory entry records which server saved it. Its own memory reviews only edit or delete entries saved by this server; entries saved by the group's other servers are read-only to it (explicit requests in chat still work). If linking pushes the total over 50, it won't delete entries just to make room; an admin has to bring it down to 49 before new ones can be saved.
  - **Names**: card lookups by name recognize a person's nicknames from every server in the group and prefer people who have appeared here; a same-named person from another server in the group is marked as not having appeared here yet. @mentions only match people who have appeared in this server, under their nickname here. A channel named together with its server ("#大厅（「B」服务器）") is only looked up in that server, never in or linked to a same-named channel here.
  - **Survives power cuts**: while linking or leaving, every step waits until all three database files are on disk; an interrupted operation is finished the next time `/dsowner 服务器组` runs or the bot restarts. If a dissolved group still has data (usually from restoring only one or two of the files), it isn't deleted; the log and `操作:查看` point it out.
  - `/ds 状态`, `/ds 长期记忆`, `/ds-dm 状态`, the first-DM notice and `/dsowner 总览` all say which servers are linked.
- **One profile card across all servers** (off by default): new setting `shared_cards` under `[bot]` in `config.toml`. Set it to `true` and recreate the container, and each person has a single card for every server and DMs that follows them around: tell it "remember I'm a catgirl" in server A and it knows in server B too. Long-term memory isn't affected.
  - **Turning it on** merges each person's cards into one: the most recently edited card wins, and the others become old versions (switch to one and back with `/ds-rp 操作:改回上一版`; once you switch back, the one you switched to is gone), keeping 5 old versions in all. Clearing a card in one server only removes that server's card; a card the person still has in another server is merged in as usual. Cards cleared while shared and cards the bot owner deleted aren't brought back from anywhere. The log says how many people's cards were merged.
  - **Turning it off** copies each person's card (with its old versions) back to every server they've appeared in (into the group for linked servers); after that each copy is edited separately.
  - **No lookups across unrelated servers**: name lookups only find people who have appeared in this server (or, for linked servers, in the group's servers); @mentions and the list of people with cards in the prompt only count people who have appeared here, under their nickname here.
  - Cards may hold things people said in other servers or in DMs, and it's told not to bring up private things from elsewhere. The first-DM notice, `/ds-dm 服务器`, `/ds-dm 状态`, `/ds-rp` and `/ds 状态` all say the card is shared across servers.
  - The setting is stored in `cards.sqlite3`, and switching happens in a single transaction; if it fails, the bot keeps the old mode (and says so in the log) and tries again on the next restart.
- **`CONTROL_GUILD_ID` takes several servers**: separate them with commas (Chinese or English), 、, semicolons or spaces. Each one gets `/dsowner`, and it works the same in all of them; if registering in one fails (say the bot isn't in it), the others still work. `0` counts as not set, and full-width digits work.

**Changed**

- Chatting in DMs no longer renames a person's profile card to the name they use in DMs (their global Discord name). Before, after one DM, the server's list of people with cards showed that name and looking them up by their server nickname found nothing, until they talked in the server again.
- `/ds 长期记忆`: when deleting, the 编号 field lists each entry's text (type a few words to filter), and the reply says which entry was deleted; when memory is full, it says how many entries to delete; when there are too many entries to fit, each one is shortened but every number is still listed.
- A channel it names together with another server (say "「A」服务器的 #大厅") is no longer turned into a link to this server's channel of the same name.

**Upgrading**

- Only the `dsbot` folder changed: replace it, then run `sudo docker compose up -d --build`. To check the new version took effect: the log shows `dsbot 2.7.0 启动中`.
- The databases add a few tables and columns on their own. The new `shared_cards` setting in `config.toml` is optional and defaults to `false` (one card per server, as before).
- Only the bot owner can link servers, and nothing changes until you do. Before linking, stop the project and back up `data/`, and consider telling each server's members. Once you've used server groups, back up and restore the three database files in `data/` together.
- Before turning on `shared_cards`, also stop the project and back up `data/`, and consider telling people: what's on a card gets used in every server. Setting it back to `false` doesn't restore how things were: every server gets the shared card, and each server's original card survives at most as an old version (only the last 5 old versions are kept, so a few edits while shared, including those from its memory reviews, push them out).
- To go back to an older version: if you turned on `shared_cards`, set it back to `false`, restart once, and wait for “人设卡：改回每个服务器一张” in the log before switching; if you linked servers, have them leave one by one until the group dissolves (older versions know about neither, and can't see shared cards or a group's cards and long-term memory); if `CONTROL_GUILD_ID` in `.env` lists several servers, cut it back to one (older versions only accept one and won't start otherwise).
- The English edition wasn't updated this time and stays at v2.0.0.

**Development**

- New module `groups.py`: `plan_link` / `link`, `plan_unlink` / `unlink`, `finish_leaving` / `recover` (every step can be redone, so an interrupted operation gets finished), `durable` (all three databases use `synchronous=FULL` during group operations), `leftovers` (data left by dissolved groups is reported, never deleted).
- `store.py`: tables `server_groups`, `server_group_seq` and `server_group_leaving`, with `Store.memory_scope` / `group_of` / `group_members` / `join_group` / `start_leaving` / `unmap` / `group_epoch` and more. Group IDs are negative, and a group's shared cards and long-term memory are stored under its ID in the existing `cards.sqlite3` and `memory.sqlite3`. The main database is now also closed on a clean shutdown.
- `memory.py`: new columns `memories.origin` (which server saved it) and `card_versions.merged` (merged-in versions: restoring one saves the current card as an old version, see `CardStore.swaps_on_restore`); new tables `card_seen` (which servers in a group a card was used in, and the person's name there) and `card_meta` (`shared`); `merge_scope` / `copy_scope` / `drop_scope` / `merge_preview` and more for group operations. Shared cards: `SHARED = 0`, `CardStore.set_shared` (`_share` / `_split`, returns a `ShareChange`), `for_groups()` (while cards are shared, group operations get a `SharedCardMarks` that only moves deletion marks), `find_here(near=…)`, `owners_near`. When switching to or from shared cards (`_combine`), only deletion marks under 0 (deleted everywhere) block a card; when linking (`merge_scope`), the group's own deletion marks still count.
- `engine.py`: `_scope` / `_card_scope` / `_card_near`; `_memory_key` (where the memory lives, plus how many times this server linked or left: if that changed while the model was thinking, numbered edits are skipped and memory reviews are asked again right away); `ChannelState.memory_shown` (numbers it wasn't shown aren't touched); `_foreign_memories` (entries saved by the group's other servers are read-only); `_source_name` (in the list of channels it can look through, channels from the group's other servers get their server's name after them), `_find_source` (a channel named together with its server is only looked up in that server; `_guilds_named` matches the server name); `groups_changed`; `status()` gains `group` and `cards_shared`.
- `commands.py`: `/dsowner 服务器组` (`group_command`; `ConfirmGroup` checks again on confirm and does nothing if things changed), `recover_groups`, `apply_card_mode` (at startup, `recover_groups` runs first, then the card mode is switched to match `config.toml`, both before commands are registered), `memory_number_choices`, `dm_group_note` / `dm_warning`.
- `config.py`: `Secrets.control_guild_ids` (was `control_guild_id`), `parse_guild_ids`; `BotConfig.shared_cards` (true / false, also spellings like 开 / 关, on / off, yes / no, 1 / 0; anything it doesn't recognize is an error rather than a guess, so a typo can't merge or split cards).
- `mentions.link` gains `here`; `render.render_who` can add a note after a name, and `render_review_input` marks entries saved by the group's other servers.
- `prompts.py`: `GROUP_BLOCK`, `MEMORY_BLOCK_GROUP`, `MEMORY_OVER`, `CARDS_SHARED_BLOCK`, `REVIEW_GROUP` / `REVIEW_GROUP_SHARED` / `REVIEW_SHARED` / `REVIEW_MEMORY_FOREIGN`. The note that others also have cards only says that some exist, not how many, so one more card elsewhere doesn't change this server's prompt or break its cache. Servers that aren't linked, with `shared_cards` off, get exactly the same prompts as in 2.6.0 (except that chatting in DMs no longer renames cards, see Changed).
- There are now 1014 tests (new `test_server_groups.py` and `test_shared_cards.py`).

## [2.6.0] - 2026-11-05

> 英文版这次没有更新，还是 2.0.0。
> The English edition wasn't updated this time and stays at 2.0.0.

### 中文

**新增**

- **私信记忆**：私信里聊到的、你和它之间的事（最近在忙什么、约好的事、它答应你的事、私信里一起玩的剧情），会记进你自己的私信记忆。你的称呼、设定、喜好照旧记在人设卡里，服务器的事记在长期记忆里。
  - 私信里叫它“记住……”当场记（加个 📝，回话里说记成了敏感还是不敏感）；私信安静下来以后它也会自己整理。
  - **人设卡优先**：关于你自己、长期有效、又不敏感的（称呼、设定、喜好雷点、习惯、身份、爱好）先写进人设卡；写不进人设卡的（你们之间的事、近况、约定、敏感的事）才记进私信记忆。
  - 每人一份，跟着人走、不分服务器，换了私信挂的服务器也还在；最多 150 条，每条 200 字，快满了它自己合并、删掉旧的。私信里只列最近改过的那些（最多约 6000 字），服务器里只带最近改过的 20 条不敏感的；整理记忆时全都看得到。
  - **每条标着敏感不敏感**：健康、住址、学校单位、真实姓名、联系方式、钱、感情上的烦恼，还有说了要保密的，都算敏感，只在私信里用。不敏感的，你在私信挂着的那个服务器（以及开了“跨服务器”的服务器）里叫它时也会用上，可能在大家面前提起。
  - 敏感不敏感是它自己判断的，可能判错：私信里直接跟它说（“第 3 条不算敏感”），或者用新命令 `/ds-dm 记忆` 查看（只有自己看得到）、设为敏感 / 不敏感（编号一栏输入几个字就能选）、删除某条、全部清空。清空之后，整理记忆时不会拿清空之前的聊天把它们重新记回来。
  - **敏感的事守在私信里**：就算叫它记住，敏感的事也只记进私信记忆（标成敏感），不会写进人设卡和长期记忆；说到敏感的事的，整理记忆时也拦下来。敏感标记只有本人能往下调（私信里说的那一轮它还看了文件、链接、搜索结果的话不算，免得被骗），它整理记忆、改说法时只能往上调；先删后加、合并进来的也认得出是不是敏感的事。
  - **撤回**：某条改成敏感、本人删掉或改掉某条、它把不敏感的并进了敏感的那条、清空、私信改挂到别的服务器，或者服务器关掉了私信记忆 / 跨服务器，服务器频道记录里附过的直接拿掉（和群友删消息一样）。
  - 在频道里叫它时带上的私信近况，只带本人说的话（它在私信里的回话不带），说到敏感的事的那句也不带；私信里刚说了敏感的事，之前的私信近况都不带了。
  - 管理员、bot 主人都看不到私信记忆，日志和 `/ds 状态` 里也不写它的想法；`/dsowner 删除人设卡` 删人时连私信记忆一起删。别人的人设卡原文不会被抄进私信记忆。
  - 说了“保密”的那段：以前完全不记，现在可能记进私信记忆并标成敏感，仍然不会进人设卡和长期记忆。
  - 新参数“私信记忆”（`dm_memory`，管理员，只看全服务器这一级，默认开）。
- **跨服务器**：新参数“跨服务器”（`cross_server`，管理员，只看全服务器这一级，**默认关**）。开了的服务器之间，同一个人跟它聊过的会跟着他走：
  - 他在 B 服务器叫它时，带上他最近 6 小时在 A 服务器跟它聊的两段（只有他自己的话和它的回复），写明是哪个服务器的哪个频道。两边都要开。
  - 只带公开频道里的；私密频道、私密帖子、私信的原话、说了要保密的那段都不带。
  - 私信记忆里不敏感的那些，在开了跨服务器的服务器里也会用上；私信挂着的那个服务器也开了的话，私信里也接得上他在别的服务器跟它聊的。
- `/ds 状态` 多了“私信记忆”“跨服务器”两行；`/ds-dm 状态` 会说私信记忆有几条（几条敏感）。

**升级须知**

- 只有 `dsbot` 文件夹变了：换掉它，再 `sudo docker compose up -d --build`。确认新版生效：日志里有 `dsbot 2.6.0 启动中`。
- 数据库会自动多几张表（私信记忆存在 `data/memory.sqlite3`）。`config.toml` 里新加的参数不加也行，都有默认值。
- ⚠️ **私信记忆默认是开的**：更新后，私信里聊的事会开始记进私信记忆，不敏感的会在服务器里被用上。以前就私信过它的人，下次私信时它会先说明一次。不想要的服务器用 `/ds 设置 参数:私信记忆 值:关`；也建议跟群友说一声 `/ds-dm 记忆` 怎么用。
- 跨服务器默认关，要用的服务器都得开：`/ds 设置 参数:跨服务器 值:开`。开之前最好跟群友说一声。
- 英文版这次没有更新，还是 v2.0.0。

**开发**

- `memory.py`：`dm_memories`、`dm_memory_seq`、`dm_memory_cleared` 三张表和 `MemoryStore.dm_*`；`MemoryOp.sensitive`；`parse_review` 多返回一组私信记忆的改动。
- `[engine]` 新增 `dm_memory_max_entries`（150）、`dm_memory_entry_chars`（200）、`dm_memory_prompt_chars`（6000）、`dm_memory_attach_entries`（20）。
- 新动作 `{"type": "dm_memory", "add" / "edit" / "delete", "sensitive": …}`（只在私信里生效）；`Action.sensitive`。
- `engine.py`：`_dm_memory_context` / `_mark_dm_memory`（频道里附上不敏感的私信记忆，`ChannelState.dm_memory_shown` 记着附过哪份）、`_apply_dm_ops` / `_dm_one`（一批改动对照这一批开始前的敏感条目，先删后加也认得出）、整理记忆时的 `dm_ok`；`quiet_dm` / `_forget_dm_talk`（私信里刚记了敏感的事，私信近况不再带到频道里）；`_elsewhere` 拿别的服务器的近况；`_screen_texts` 拦住把别人的人设卡抄进私信记忆。
- `own_text` / `review_text` 去掉频道里附上的私信记忆，压缩、整理频道时不会抄走。
- 频道里附过的私信记忆记在 `dm_memory_attached` 表里，`Engine.withdraw_dm_memory` 按它把记录里的那一段拿掉；`memory.overlaps` / `reveals` 拦住说到敏感的事的人设卡、长期记忆。`dm_notices` 表记着谁看过私信记忆的说明（以前私信过的人说明一次）。
- 测试共 911 个（新增 `test_dm_memory.py`）。

### English

**Added**

- **DM memory**: things from your DMs that are between you and the bot (what you've been busy with, plans you made, things it promised you, a story you're playing together in DMs) are saved to your own DM memory. What to call you, your persona and likes stay on your profile card, and server matters stay in the server's long-term memory.
  - Say "remember …" in DMs to save something right away (it adds 📝 and says whether it saved it as sensitive); it also reviews the DM on its own once things go quiet.
  - **Profile card first**: things about you that last and aren't sensitive (what to call you, your persona, likes and pet peeves, habits, identity, hobbies) go on your profile card first; only what doesn't fit there (things between you two, recent news, plans, and sensitive things) goes to DM memory.
  - One per person, it follows you across servers and survives switching the server your DMs use; up to 150 entries of 200 characters, and it merges or deletes old ones as it fills up. DMs list only the most recently changed entries (about 6,000 characters at most), and servers get at most the 20 most recently changed non-sensitive ones; its memory reviews see all of them.
  - **Every entry is marked sensitive or not**: health, address, school or workplace, real name, contact details, money, relationship troubles, and anything you asked to keep secret are sensitive and only used in DMs. The rest is also used when you call it in the server your DMs use (and in servers with cross-server on), so it may come up in front of everyone.
  - It decides what's sensitive itself and can get it wrong: tell it in DMs ("entry 3 isn't sensitive"), or use the new `/ds-dm 记忆` command to view it (only you can see it), mark an entry sensitive or not (type a few words in the number field to pick it), delete an entry, or clear everything. After clearing, its memory reviews won't re-add anything from chat before the clear.
  - **Sensitive things stay in DMs**: even when you ask it to remember them, sensitive things only go to DM memory (marked sensitive), never to your profile card or the server's long-term memory, and its memory reviews block anything that mentions them. Only you can lower the sensitive mark (not in a DM round where it also read a file, link or search results, so text in those can't trick it); its reviews and rewordings can only raise it, and merges or delete-then-add batches still recognize sensitive things.
  - **Withdrawal**: when an entry is marked sensitive, you delete or edit one, it merges a non-sensitive entry into a sensitive one, you clear it all, your DMs move to another server, or a server turns off DM memory or cross-server, copies already attached in server channels are removed from the chat record (the same way deleted messages are).
  - The recent DM lines brought along when you call it in a channel only include your own words (not its replies in DMs), and lines that mention your sensitive things are left out; once you've just said something sensitive in DMs, no earlier DM lines are brought along.
  - Admins and the bot owner can't see it, and its reasoning isn't written to logs or `/ds 状态`; `/dsowner 删除人设卡` also deletes the person's DM memory. Other people's profile cards are never copied into your DM memory word for word.
  - Parts where you said to keep it secret: these used to be not saved at all; now they may be saved to DM memory, marked sensitive, and still never go to the profile card or long-term memory.
  - New setting 私信记忆 (`dm_memory`, admins, whole-server level only, on by default).
- **Cross-server**: new setting 跨服务器 (`cross_server`, admins, whole-server level only, **off by default**). Between servers that turned it on, what a person talked about with it follows them:
  - When they call it in server B, it brings along their last two exchanges with it in server A from the past 6 hours (only their own messages and its replies), saying which server and channel they were in. Both servers have to turn it on.
  - Only public channels count; private channels and threads, DM lines, and parts they asked to keep secret are never carried over.
  - The non-sensitive part of their DM memory is used in servers with cross-server on too, and if the server their DMs use also has it on, DMs can pick up what they just talked about in other servers.
- `/ds 状态` shows two new lines, 私信记忆 and 跨服务器; `/ds-dm 状态` shows how many DM memory entries you have (and how many are sensitive).

**Upgrading**

- Only the `dsbot` folder changed: replace it, then run `sudo docker compose up -d --build`. To check the new version took effect: the log shows `dsbot 2.6.0 启动中`.
- The database adds a few tables on its own (DM memory lives in `data/memory.sqlite3`). The new settings in `config.toml` are optional and have defaults.
- ⚠️ **DM memory is on by default**: after updating, DM conversations start being saved to DM memory, and the non-sensitive parts get used in servers. People who already DM'd it get a one-time explanation on their next DM. Turn it off where you don't want it with `/ds 设置 参数:私信记忆 值:关`, and consider telling your members about `/ds-dm 记忆`.
- Cross-server is off by default, and every server that wants it has to turn it on: `/ds 设置 参数:跨服务器 值:开`. Consider telling your members first.
- The English edition wasn't updated this time and stays at v2.0.0.

**Development**

- `memory.py`: new tables `dm_memories`, `dm_memory_seq` and `dm_memory_cleared` with `MemoryStore.dm_*`; `MemoryOp.sensitive`; `parse_review` returns a third list with the DM memory changes.
- New `[engine]` settings: `dm_memory_max_entries` (150), `dm_memory_entry_chars` (200), `dm_memory_prompt_chars` (6000), `dm_memory_attach_entries` (20).
- New action `{"type": "dm_memory", "add" / "edit" / "delete", "sensitive": …}` (only applied in DMs); `Action.sensitive`.
- `engine.py`: `_dm_memory_context` / `_mark_dm_memory` (attach the non-sensitive DM memory in channels; `ChannelState.dm_memory_shown` tracks what was attached), `_apply_dm_ops` / `_dm_one` (a batch is checked against the sensitive entries from before it started, so delete-then-add is still caught), `dm_ok` in memory reviews; `quiet_dm` / `_forget_dm_talk` (once something sensitive is saved in DMs, recent DM lines stop going to channels); `_elsewhere` for recent exchanges from other servers; `_screen_texts` keeps other people's cards out of DM memory.
- `own_text` / `review_text` drop the attached DM memory, so channel summaries and reviews never copy it.
- DM memory attached in channels is recorded in the `dm_memory_attached` table, and `Engine.withdraw_dm_memory` uses it to cut that block out of the record; `memory.overlaps` / `reveals` block card and long-term memory writes that mention sensitive things. The `dm_notices` table tracks who has seen the DM memory explanation (people who DM'd before get it once).
- There are now 911 tests (new `test_dm_memory.py`).

## [2.5.0] - 2026-11-05

> 英文版这次没有更新，还是 2.0.0。
> The English edition wasn't updated this time and stays at 2.0.0.

### 中文

**改进**

- **联网搜索、读网页、按歌名找歌都先用 DeepSeek 的 Search / Fetch 接口**（2026-10-17 开放，用的就是 `DEEPSEEK_API_KEY`），Tavily 改成备用：
  - **联网搜索**：改用 Search 接口，一次通常不到 1 秒（以前是让 DeepSeek 的模型边搜边整理，要好几秒到十几秒）。搜到的几条（标题、网站、日期、摘要）交给它自己读，挑对得上的说。DeepSeek 没搜到或者出错了，填了 `TAVILY_API_KEY` 的话改用 Tavily 再搜一次。
  - **读网页**：群友发的网页先交给 Fetch 接口去读，读不了的（PDF、有的网站不让它看、整页都是图）再交给 Tavily。**不填 `TAVILY_API_KEY` 也能打开一般网页了**（以前只能读推文）。网页还是由 DeepSeek / Tavily 的服务器去读，NAS 自己不打开。
  - **按歌名找歌**：先用 Search 接口只搜 bilibili.com，没找到再用 Tavily，最后才用 B站自己的搜索（它容易被风控拦下来）。讲这首歌的视频（分析、解析、讲解、演奏之类）也往后排，免得放成别人在讲歌。
  - 上网找图还是只能用 Tavily（DeepSeek 的搜索不给图片）。
- **费用**：Search、Fetch 都按次收费，每 1000 次 0.75 美元（一次 0.075 美分），只有搜到结果、读到内容才算钱，算进每日预算和私信预算。联网搜索比以前便宜（以前按 token 算，一次约 0.1–0.5 美分）。超了每日预算时和以前一样不搜，也不用 DeepSeek 读网页、按歌名找歌（推文、Tavily、B站自己的搜索不花钱，照常）。
- 让它搜最近的事时，它会在关键词里写具体日期（“11月4日”比“昨晚”搜得准）；搜到的结果可能有不相关的、过时的，它会看清日期和来源再用。
- `config.toml` 的 `base_url` 填了不带这两个接口的中转地址时，日志会提醒你（以前会被当成网页打不开）。

**升级须知**

- 只有 `dsbot` 文件夹变了：换掉它，再 `sudo docker compose up -d --build`。确认新版生效：日志里有 `dsbot 2.5.0 启动中`。
- `config.toml` 的 `[pricing]` 新加的 `search_per_1000`、`fetch_per_1000` 不加也行，有默认值；`[deepseek]` 下面的 `search_base_url`、`search_timeout_seconds`、`search_max_uses` 用不到了，留着也没关系。
- `TAVILY_API_KEY` 现在只用来上网找图，以及在 DeepSeek 搜不到、读不了时备用，不填也行。
- 英文版这次没有更新，还是 v2.0.0。

**开发**

- 新文件 `dsbot/dstools.py`：`DeepSeekTools`（Search / Fetch 接口）。`search.py` 改成 Search 接口 → Tavily，Anthropic 兼容接口那一套（`parse_messages_response`、`estimate_search` 等）去掉了。
- `SearchResult.paid`、`SearchError.paid`、`LinkResult.paid`：DeepSeek 算了钱的次数；`Pricing.search_cost()`、`fetch_cost()`。`LinkReader.read(..., pay=False)`：超了预算不用要花钱的读法。`Source.search(query, paid=..., pay=...)`：按歌名找歌算了钱时叫一下，记在这次找歌的那一行上；超了预算不用 DeepSeek。
- 测试共 862 个（`test_share_search.py` 的 `SearchClient`、`test_links_images.py`、`test_music.py` 新增了 DeepSeek 接口和记账的测试）。

### English

**Improved**

- **Web search, reading web pages and finding songs by name now go to DeepSeek's Search / Fetch APIs first** (opened on 2026-10-17, using the same `DEEPSEEK_API_KEY`), with Tavily as the backup:
  - **Web search**: now uses the Search API and usually takes under a second (it used to have a DeepSeek model search and summarize, which took several seconds or more). The results it finds (title, site, date, summary) are handed to it to read, and it picks what's relevant. If DeepSeek finds nothing or fails, it searches again with Tavily when `TAVILY_API_KEY` is set.
  - **Reading web pages**: pages people post are read through the Fetch API first; what it can't read (PDFs, sites that block it, pages that are all pictures) goes to Tavily. **Ordinary web pages now open without `TAVILY_API_KEY`** (before, only posts on X could). Pages are still read by DeepSeek's / Tavily's servers; the NAS never opens them itself.
  - **Finding songs by name**: searches only bilibili.com through the Search API first, then Tavily, and only then Bilibili's own search (which is often blocked by its anti-bot checks). Videos that talk about the song (analysis, breakdowns, instrumental performances) are ranked lower too, so it doesn't end up playing someone discussing the song.
  - Image search still needs Tavily (DeepSeek's search doesn't return images).
- **Cost**: Search and Fetch are billed per request, $0.75 per 1,000 (0.075 US cents each), and only when they return results or page content. The cost counts toward the daily budget and the DM budget. Web search is cheaper than before (it used to be billed by tokens, roughly 0.1–0.5 US cents per search). Over the daily budget it still doesn't search, and it also stops using DeepSeek to read web pages or find songs by name (posts on X, Tavily and Bilibili's own search are free and keep working).
- When searching for something recent, it writes the actual date in the query ("Nov 4" finds more than "last night"), and it checks the dates and sources of what it finds, since some results may be off-topic or out of date.
- If `base_url` in `config.toml` points to a relay that doesn't offer these two APIs, the log now tells you (before, it looked like the web page couldn't be opened).

**Upgrading**

- Only the `dsbot` folder changed: replace it, then run `sudo docker compose up -d --build`. To check the new version took effect: the log shows `dsbot 2.5.0 启动中`.
- The new `search_per_1000` and `fetch_per_1000` under `[pricing]` in `config.toml` are optional and have defaults. `search_base_url`, `search_timeout_seconds` and `search_max_uses` under `[deepseek]` are no longer used; it's fine to leave them.
- `TAVILY_API_KEY` is now only for image search and as a backup when DeepSeek can't search or read something; it's optional.
- The English edition wasn't updated this time and stays at v2.0.0.

**Development**

- New file `dsbot/dstools.py`: `DeepSeekTools` (the Search / Fetch APIs). `search.py` now goes Search API → Tavily; the Anthropic-compatible code (`parse_messages_response`, `estimate_search` and so on) was removed.
- `SearchResult.paid`, `SearchError.paid`, `LinkResult.paid`: how many DeepSeek calls were billed; `Pricing.search_cost()`, `fetch_cost()`. `LinkReader.read(..., pay=False)`: skip the paid way of reading when over budget. `Source.search(query, paid=..., pay=...)`: `paid` is called when a song search was billed and charged to that search's row; `pay=False` skips DeepSeek when over budget.
- There are now 862 tests (new tests for the DeepSeek APIs and their billing in `SearchClient` in `test_share_search.py`, `test_links_images.py` and `test_music.py`).

## [2.4.0] - 2026-10-28

> 英文版这次没有更新，还是 2.0.0。
> The English edition wasn't updated this time and stays at 2.0.0.

### 中文

**新增**

- **替你操作电脑（桌面操作，可选）**：你（`OWNER_ID`）私信它“帮我开一下 Steam”“把记事本关了”“打开 https://…”，它就替你开关电脑上登记过的程序、用默认浏览器打开网址，办完告诉你结果。
  - 能做的只有这三种：开、关你在电脑上 `agent.toml` 里登记过的程序（关是正常关闭，跟点右上角的叉一样，没保存的东西程序会先问你），打开 http / https 网址（网址要是你自己那条消息里发的）。不跑命令、不碰文件。
  - 只认你本人：只在你自己的私信里、你这一轮亲自叫它时才办。别人私信它、在服务器频道里都用不了；聊天记录、文件、网页里写的“去打开……”一律不做。
  - 电脑上要运行一个桌面小程序（`desktop_agent` 文件夹，要 Python 3.11 以上）。小程序是主动连 NAS 的，电脑不用开端口；在家连局域网地址，出门可以连 ZeroTier 这类虚拟局域网地址，按顺序试。
  - 连接用 `DESKTOP_TOKEN` 签名，每条指令也带签名和递增的编号，别人伪造不了，旧指令也重放不了。
  - 电脑没开、小程序没运行时，它会告诉你“电脑现在不在线”。不想用了：`/dsowner 设置 参数:桌面操作 值:关`。

**升级须知**

- `dsbot` 文件夹变了，另外新加了 `desktop_agent` 文件夹。只换 `dsbot`、不填 `DESKTOP_TOKEN` 时，桌面操作不会开，其他一切照旧。
- 想用桌面操作：在 `.env` 里填 `DESKTOP_TOKEN`，把 `docker-compose.yml` 里 `ports:` 那两行前面的 `#` 去掉，再 `sudo docker compose up -d --build`；然后在电脑上装好桌面小程序。步骤见 README 的“十五、操作你的电脑”。
- 确认新版生效：日志里有 `dsbot 2.4.0 启动中`；填了 `DESKTOP_TOKEN` 的话还有 `桌面操作：等电脑上的小程序连过来`，小程序连上后有 `电脑「…」连上了`。
- `config.toml` 里新加的 `desktop_port`、`desktop` 不加也行，都有默认值。
- 英文版这次没有更新，还是 v2.0.0。

**开发**

- 新文件 `dsbot/desktop.py`：`DesktopHub`，一个 aiohttp WebSocket 服务（容器里的 8765 端口），等小程序连过来；HMAC-SHA256 签名，连接时双方核对，指令编号只增不减。
- 新文件夹 `desktop_agent`：电脑上运行的小程序 `agent.py`（只依赖 aiohttp），配置照着 `agent.example.toml` 写。
- `actions.py` 新动作 `{"type":"desktop","op":"open|close|url","target":…}`，一轮最多 3 个；`engine.py` 的 `_desktop_here` / `_desktop_section` 负责只在 dsowner 的私信里、只认 dsowner 本人这一轮的消息才执行。
- 新参数 `desktop`（dsowner 才能改），`config.toml` 的 `[bot]` 新加 `desktop_port`。
- 测试增加到 857 个（`test_desktop.py`：解析、引擎、真的连一次小程序、小程序的动作）。

### English

**Added**

- **Operates your computer (desktop control, optional)**: DM it as `OWNER_ID` with "open Steam for me", "close Notepad" or "open https://…", and it opens or closes programs you've listed on your PC, or opens a web page in your default browser, then tells you how it went.
  - It can only do these three things: open or close programs listed in `agent.toml` on your PC (closing is a normal close, like clicking the X, so the program asks about unsaved work first), and open http / https links that appear in your own message. It never runs commands or touches files.
  - It only listens to you: only in your own DMs, only for the message where you called it this turn. Other people's DMs and server channels can't use it, and "go open …" written in chat history, files or web pages is ignored.
  - Your PC needs to run a small desktop agent (the `desktop_agent` folder, Python 3.11 or later). The agent connects out to the NAS, so the PC doesn't open any port. At home it uses the LAN address; on the road it can use a virtual LAN address such as ZeroTier, trying them in order.
  - The connection is signed with `DESKTOP_TOKEN`, and every command carries a signature and an increasing number, so nobody can forge commands or replay old ones.
  - When the PC is off or the agent isn't running, it tells you the computer is offline. To turn it off: `/dsowner 设置 参数:桌面操作 值:关`.

**Upgrading**

- The `dsbot` folder changed, and there's a new `desktop_agent` folder. If you only replace `dsbot` and don't set `DESKTOP_TOKEN`, desktop control stays off and everything else works as before.
- To use desktop control: set `DESKTOP_TOKEN` in `.env`, remove the `#` in front of the two `ports:` lines in `docker-compose.yml`, then run `sudo docker compose up -d --build`; then set up the desktop agent on your PC. See section 十五 of the README.
- To check the new version took effect: the log shows `dsbot 2.4.0 启动中`; with `DESKTOP_TOKEN` set it also shows `桌面操作：等电脑上的小程序连过来`, and `电脑「…」连上了` once the agent connects.
- The new `desktop_port` and `desktop` settings in `config.toml` are optional and have defaults.
- The English edition wasn't updated this time and stays at v2.0.0.

**Development**

- New file `dsbot/desktop.py`: `DesktopHub`, an aiohttp WebSocket server (port 8765 in the container) that waits for the agent; HMAC-SHA256 signing, both sides verify on connect, command numbers only go up.
- New folder `desktop_agent`: the agent `agent.py` that runs on the PC (needs only aiohttp), configured from `agent.example.toml`.
- New action in `actions.py`: `{"type":"desktop","op":"open|close|url","target":…}`, at most 3 per turn; `_desktop_here` / `_desktop_section` in `engine.py` make sure it only runs in dsowner's DMs, for dsowner's own message this turn.
- New setting `desktop` (dsowner only); new `desktop_port` under `[bot]` in `config.toml`.
- There are now 857 tests (`test_desktop.py`: parsing, engine, a real agent connection, the agent's actions).

## [2.3.2] - 2026-10-27

> 英文版这次没有更新，还是 2.0.0。
> The English edition wasn't updated this time and stays at 2.0.0.

### 中文

**修复**

- **私信里不显示“正在输入”**：discord.py 收到私信时不把私信频道放进缓存，按 ID 找不到，“正在输入”就悄悄跳过了。现在私信里它想回复、看文件、上网查的时候都会显示“正在输入”。
- 私信里它不知道对方叫什么（提示词里写的是“对方”），现在用对方的名字。
- 私信里发消息、加表情不用每次再去问 Discord 要一次频道。

**升级须知**

- 只有 `dsbot` 文件夹变了：换掉它，再 `sudo docker compose up -d --build`。确认新版生效：日志里有 `dsbot 2.3.2 启动中`。
- 英文版这次没有更新，还是 v2.0.0。

**开发**

- `discord_app.py` 的 `DiscordTransport._cached`：私信频道不在缓存里时换成 `get_partial_messageable`；`DSBot.dm_names` 记着私信对方最近的名字。
- 测试增加到 840 个（`test_dm.py` 的 `TransportDM`）。

### English

**Fixed**

- **No "typing…" in DMs**: discord.py doesn't keep DM channels in its cache, so looking one up by ID found nothing and the typing indicator was silently skipped. It now shows "typing…" in DMs while it's writing a reply, reading files or searching.
- In DMs it didn't know the other person's name (the prompt said "the other person"); it now uses their name.
- Sending messages and adding reactions in DMs no longer asks Discord for the channel each time.

**Upgrading**

- Only the `dsbot` folder changed: replace it, then run `sudo docker compose up -d --build`. To check the new version took effect: the log shows `dsbot 2.3.2 启动中`.
- The English edition wasn't updated this time and stays at v2.0.0.

**Development**

- `DiscordTransport._cached` in `discord_app.py` falls back to `get_partial_messageable` when a DM channel isn't cached; `DSBot.dm_names` keeps the DM partner's latest name.
- There are now 840 tests (`TransportDM` in `test_dm.py`).

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
