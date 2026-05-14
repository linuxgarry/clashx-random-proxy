#!/usr/bin/env bash
# random-proxy unraid 部署脚本
# 在 unraid 终端（或任何能 SSH 到 unraid 的机器）上跑：
#
#   cd /mnt/user/appdata
#   git clone https://github.com/linuxgarry/random-proxy.git
#   cd random-proxy
#   cp .env.example .env
#   vim .env                    # 填好 PG_PASSWORD / MIHOMO_SECRET 等
#   bash scripts/deploy.sh

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"

echo "==> 检查依赖"
command -v docker >/dev/null || { echo "未找到 docker"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "未找到 docker compose"; exit 1; }

if [[ ! -f .env ]]; then
    echo "错误：.env 不存在。请先 cp .env.example .env 并填完字段。"
    exit 1
fi

# shellcheck disable=SC1091
source .env

: "${MIHOMO_IP:?MIHOMO_IP 未设置}"
: "${PG_HOST:?PG_HOST 未设置}"
: "${PG_USER:?PG_USER 未设置}"
: "${PG_PASSWORD:?PG_PASSWORD 未设置}"

echo "==> 检测 macvlan 父接口"
PARENT="${MACVLAN_PARENT:-}"
if [[ -z "$PARENT" ]]; then
    PARENT=$(ip -o link show | awk -F': ' '/state UP/ && /br0|bond0|eth0/ {print $2; exit}' || true)
    PARENT="${PARENT:-br0}"
fi
echo "    使用父接口: $PARENT （如果不对，重新跑：MACVLAN_PARENT=xxx bash scripts/deploy.sh）"

echo "==> 检查 macvlan 网络 lan_macvlan"
if ! docker network inspect lan_macvlan >/dev/null 2>&1; then
    # 自动从 MIHOMO_IP 推测网段
    SUBNET=$(echo "$MIHOMO_IP" | awk -F. '{print $1"."$2"."$3".0/24"}')
    GATEWAY=$(echo "$MIHOMO_IP" | awk -F. '{print $1"."$2"."$3".1"}')
    echo "    创建 macvlan: subnet=$SUBNET gateway=$GATEWAY parent=$PARENT"
    docker network create -d macvlan \
        --subnet="$SUBNET" \
        --gateway="$GATEWAY" \
        -o parent="$PARENT" lan_macvlan
else
    echo "    已存在，跳过"
fi

echo "==> 应用数据库 schema"
if command -v psql >/dev/null 2>&1; then
    PGPASSWORD="$PG_PASSWORD" psql -h "$PG_HOST" -p "${PG_PORT:-5432}" \
        -U "$PG_USER" -d "${PG_DB:-proxy}" -f sql/001_schema.sql
else
    echo "    未找到 psql，使用一次性 docker 容器跑"
    docker run --rm -i \
        -e PGPASSWORD="$PG_PASSWORD" \
        -v "$ROOT/sql:/sql:ro" \
        postgres:16-alpine \
        psql -h "$PG_HOST" -p "${PG_PORT:-5432}" \
            -U "$PG_USER" -d "${PG_DB:-proxy}" -f /sql/001_schema.sql
fi

echo "==> 构建并启动容器"
docker compose pull mihomo || true
docker compose build api tester
docker compose up -d

echo "==> 等待 mihomo 起来"
for i in $(seq 1 30); do
    if docker compose exec -T mihomo wget -qO- http://127.0.0.1:9090/version 2>/dev/null | grep -q version; then
        echo "    mihomo 已就绪"
        break
    fi
    sleep 2
done

echo "==> 看一眼日志"
echo "  --- mihomo ---"
docker compose logs --tail=20 mihomo
echo "  --- api ---"
docker compose logs --tail=20 api
echo "  --- tester ---"
docker compose logs --tail=20 tester

echo
echo "============================================"
echo "  部署完成"
echo "  API:    http://$MIHOMO_IP:8000/health"
echo "  代理段: http://$MIHOMO_IP:10000-19999"
echo "  日志:   docker compose logs -f tester"
echo "============================================"
