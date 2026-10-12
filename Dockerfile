FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dsbot ./dsbot
COPY config.toml ./config.toml
# The file-parsing subprocess runs as nobody and must be able to read the code (files unzipped into a NAS shared folder sometimes get very strict permissions)
RUN chmod -R a+rX /app

CMD ["python", "-m", "dsbot"]
