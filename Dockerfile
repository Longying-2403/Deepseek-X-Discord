FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dsbot ./dsbot
COPY config.toml ./config.toml
# 解析文件的子进程以 nobody 运行，要能读到代码（NAS 共享文件夹解压出来的权限有时很严）
RUN chmod -R a+rX /app

CMD ["python", "-m", "dsbot"]
