#!/bin/bash
# 单容器启动：mihomo 后台 + uvicorn 前台。任意一个进程死掉都拉整个容器一起死，
# docker restart unless-stopped 会负责重启。

set -e

mihomo -d /mihomo &
MIHOMO_PID=$!

uvicorn main:app --host 0.0.0.0 --port 8000 &
UVICORN_PID=$!

shutdown() {
    kill -TERM "$MIHOMO_PID" "$UVICORN_PID" 2>/dev/null || true
    wait
    exit 0
}
trap shutdown SIGTERM SIGINT

# wait -n：任一子进程退出就解除阻塞
wait -n
EXIT=$?
kill -TERM "$MIHOMO_PID" "$UVICORN_PID" 2>/dev/null || true
wait
exit $EXIT
