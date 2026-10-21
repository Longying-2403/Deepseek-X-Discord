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

COPY dsbot ./dsbot
COPY config.toml ./config.toml
# 解析文件的子进程以 nobody 运行，要能读到代码（NAS 共享文件夹解压出来的权限有时很严）
RUN chmod -R a+rX /app

CMD ["python", "-m", "dsbot"]
