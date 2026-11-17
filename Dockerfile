FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 点歌要用 ffmpeg：把下载的 B站音频转成 Discord 语音的格式
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 构建时就把标准库和 bot 的代码编译好。官方的 Python 镜像为了省地方把标准库的 .pyc 都删了，上面又关掉了运行时写 .pyc：
# 不先编译的话，每次启动、每读一个文件（各开一个解析子进程）都要把用到的标准库和 bot 的代码从头编译一遍，大头是标准库。
# NAS 上实测：解析子进程启动从 1 秒降到 0.2～0.3 秒，bot 启动时导入模块从 1.4 秒降到 0.6 秒。
# 标准库只是为了快，编译不成也不拦着构建（放在装依赖后面：前面几层不用重新来）；pip 装的库安装时已经编译过，跳过
RUN python -m compileall -q -x site-packages "$(python -c 'import sysconfig; print(sysconfig.get_paths()["stdlib"])')" || true

COPY dsbot ./dsbot
COPY config.toml ./config.toml
RUN python -m compileall -q dsbot
# 解析文件的子进程以 nobody 运行，要能读到代码（NAS 共享文件夹解压出来的权限有时很严）
RUN chmod -R a+rX /app

CMD ["python", "-m", "dsbot"]
