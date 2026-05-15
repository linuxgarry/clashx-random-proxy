FROM metacubex/mihomo:latest AS mihomo

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=mihomo /mihomo /usr/local/bin/mihomo

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

EXPOSE 8000 9090
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["./entrypoint.sh"]
