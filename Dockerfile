FROM python:3.11.13-alpine3.22

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup -S -g 10001 msm && adduser -S -D -H -u 10001 -G msm msm

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app
RUN mkdir -p /app/config \
    && chown -R msm:msm /app \
    && chmod +x /app/start.sh

USER msm

ENTRYPOINT ["/app/start.sh"]
CMD ["python", "-m", "media_server_manager_web"]
