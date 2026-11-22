# Runs with no network, read-only root (writable /tmp), 1 CPU, 512 MB, map at /arena.
FROM python:3.13-slim
RUN pip install --no-cache-dir numpy==2.2.6
WORKDIR /bot
COPY mechfight.py world.py bot.py ./
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
