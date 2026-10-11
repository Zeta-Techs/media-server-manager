FROM python:3.11.13-alpine3.22

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup -S -g 10001 msm && adduser -S -D -H -u 10001 -G msm msm

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml README.md LICENSE alembic.ini /app/
COPY src /app/src
COPY migrations /app/migrations
COPY scripts /app/scripts
COPY start.sh /app/start.sh
RUN pip install --no-cache-dir --no-deps .
RUN mkdir -p /app/data \
    && chown -R msm:msm /app \
    && chmod +x /app/start.sh

USER msm

ENTRYPOINT ["/app/start.sh"]
CMD ["python", "-m", "media_server_manager_web"]
