FROM python:3.11-slim

# The app runs as this unprivileged user, not root. docker-compose.yml can
# override the IDs at run time (PUID/PGID) to match the data folder's owner.
RUN groupadd -g 1000 lastframe && useradd -u 1000 -g 1000 -M -d /app -s /usr/sbin/nologin lastframe

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8080
ENV DATA_DIR=/data
EXPOSE 8080

RUN mkdir -p /data && chown lastframe:lastframe /data
VOLUME ["/data"]

USER lastframe

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--timeout", "60", "--access-logfile", "-", "app:app"]
