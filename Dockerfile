FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg ca-certificates python3 python3-flask \
    && curl -fsSL https://repo.jellyfin.org/jellyfin_team.gpg.key -o /usr/share/keyrings/jellyfin.gpg.key \
    && gpg --dearmor -o /usr/share/keyrings/jellyfin.gpg /usr/share/keyrings/jellyfin.gpg.key \
    && echo "deb [signed-by=/usr/share/keyrings/jellyfin.gpg] https://repo.jellyfin.org/ubuntu noble main" > /etc/apt/sources.list.d/jellyfin.list \
    && apt-get update && apt-get install -y --no-install-recommends jellyfin-ffmpeg7 \
    && apt-get purge -y curl gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py /app/app.py
COPY static /app/static

EXPOSE 5050
CMD ["python3", "/app/app.py"]
