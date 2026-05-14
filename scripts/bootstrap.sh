#!/usr/bin/env bash
# random-proxy unraid 一键部署脚本
# 用法（unraid SSH 进去之后）：
#
#   curl -fsSL https://raw.githubusercontent.com/linuxgarry/random-proxy/main/scripts/bootstrap.sh | \
#     PG_PASSWORD='你的pg密码' bash
#
# 或者本地跑（已经 git clone 过）：
#   bash scripts/bootstrap.sh

set -euo pipefail

# ====== 配置 ======
REPO_URL="${REPO_URL:-https://github.com/linuxgarry/random-proxy.git}"
INSTALL_DIR="${INSTALL_DIR:-/mnt/user/appdata/random-proxy}"
PG_HOST_DEFAULT="${PG_HOST:-192.168.5.20}"
PG_USER_DEFAULT="${PG_USER:-garry}"
PG_DB_DEFAULT="${PG_DB:-proxy}"
MIHOMO_IP_DEFAULT="${MIHOMO_IP:-192.168.5.100}"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
yellow(){ printf '\033[33m%s\033[0m\n' "$*"; }
blue()  { printf '\033[34m%s\033[0m\n' "$*"; }

step() { echo; blue "==> $*"; }
ok()   { green "    ✓ $*"; }
warn() { yellow "    ⚠ $*"; }
die()  { red   "    ✗ $*"; exit 1; }

# ====== 0. 进项目目录（远程 curl 模式 vs 本地模式）======
if [[ -f "$(dirname "$0")/../docker-compose.yml" ]] 2>/dev/null; then
    cd "$(dirname "$0")/.."
    MODE="local"
else
    MODE="remote"
fi

step "random-proxy 部署 [$MODE 模式]"

# ====== 1. 预检：依赖 ======
step "预检：依赖"
command -v docker >/dev/null || die "未找到 docker，先在 unraid Apps 里装 Docker"
ok "docker $(docker --version | awk '{print $3}' | tr -d ',')"

# 兼容 docker compose v2（推荐）和老的 docker-compose v1
if docker compose version >/dev/null 2>&1; then
    DC="docker compose"
    ok "compose v2: $(docker compose version --short)"
elif command -v docker-compose >/dev/null 2>&1; then
    DC="docker-compose"
    ok "compose v1: $(docker-compose version --short 2>/dev/null || docker-compose --version)"
    warn "建议升级到 compose v2（unraid 上装 'compose-manager' 插件即可）"
else
    die "未找到 docker compose（v2 或 v1 都没有）。
    解决方法：unraid 上 Community Apps 搜 'compose-manager' 装一下"
fi

command -v git >/dev/null || die "未找到 git"
ok "git"

# ====== 2. 拉代码 ======
if [[ "$MODE" == "remote" ]]; then
    step "拉代码到 $INSTALL_DIR"
    if [[ -d "$INSTALL_DIR/.git" ]]; then
        cd "$INSTALL_DIR"
        git pull --rebase --autostash
        ok "已 git pull"
    else
        mkdir -p "$(dirname "$INSTALL_DIR")"
        git clone "$REPO_URL" "$INSTALL_DIR"
        cd "$INSTALL_DIR"
        ok "clone 完成"
    fi
fi

ROOT="$PWD"

# ====== 3. 准备 .env ======
step "准备 .env"
if [[ ! -f .env ]]; then
    if [[ -z "${PG_PASSWORD:-}" ]]; then
        die "首次部署需要 PG_PASSWORD 环境变量。
    用法：
      curl ... | PG_PASSWORD='xxx' bash
    或：
      cd $ROOT && cp .env.example .env && vi .env"
    fi
    cp .env.example .env
    # 用 sed 替换默认值（macOS/Linux 通用，用 perl 兜底）
    if command -v perl >/dev/null; then
        perl -i -pe "s|^PG_HOST=.*|PG_HOST=${PG_HOST_DEFAULT}|" .env
        perl -i -pe "s|^PG_USER=.*|PG_USER=${PG_USER_DEFAULT}|" .env
        perl -i -pe "s|^PG_PASSWORD=.*|PG_PASSWORD=${PG_PASSWORD}|" .env
        perl -i -pe "s|^PG_DB=.*|PG_DB=${PG_DB_DEFAULT}|" .env
    else
        sed -i "s|^PG_PASSWORD=.*|PG_PASSWORD=${PG_PASSWORD}|" .env
    fi
    # 自动生成 mihomo secret
    SECRET=$(openssl rand -hex 24 2>/dev/null || head -c 32 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 48)
    if command -v perl >/dev/null; then
        perl -i -pe "s|^MIHOMO_SECRET=.*|MIHOMO_SECRET=${SECRET}|" .env
    fi
    chmod 600 .env
    ok "已生成 .env（权限 600）"
else
    ok ".env 已存在，跳过"
fi

# shellcheck disable=SC1091
set -a; source .env; set +a

: "${PG_HOST:?}" "${PG_USER:?}" "${PG_PASSWORD:?}" "${PG_DB:?}"

# ====== 4. 预检：macvlan 父接口 ======
step "预检：macvlan 父接口"
PARENT="${MACVLAN_PARENT:-}"
if [[ -z "$PARENT" || "$PARENT" == "br0" ]]; then
    # 探测 unraid 上 UP 的物理桥接口
    DETECTED=$(ip -o link show 2>/dev/null | awk -F': ' '
        /state UP/ && ($2 ~ /^br[0-9]+$/ || $2 ~ /^bond[0-9]+$/ || $2 ~ /^eth[0-9]+$/) {
            print $2; exit
        }' || true)
    if [[ -n "$DETECTED" ]]; then
        PARENT="$DETECTED"
        ok "探测到父接口：$PARENT"
        # 写回 .env
        if grep -q '^MACVLAN_PARENT=' .env; then
            perl -i -pe "s|^MACVLAN_PARENT=.*|MACVLAN_PARENT=${PARENT}|" .env
        else
            echo "MACVLAN_PARENT=${PARENT}" >> .env
        fi
    else
        warn "没探测到，用默认 br0；如果不对，编辑 .env 改 MACVLAN_PARENT 后重跑"
    fi
else
    ok "用 .env 里的 $PARENT"
fi

# ====== 5. 预检：PG 连通性 ======
step "预检：PG 连通性"
if docker run --rm \
    -e PGPASSWORD="$PG_PASSWORD" \
    postgres:16-alpine \
    psql -h "$PG_HOST" -p "${PG_PORT:-5432}" -U "$PG_USER" -d "$PG_DB" \
        -c "SELECT count(*) FROM clashxlist;" 2>&1 | grep -E '^\s*[0-9]+' >/tmp/.pg_check; then
    COUNT=$(awk '{print $1}' /tmp/.pg_check)
    ok "PG OK，clashxlist 有 $COUNT 条"
else
    die "PG 连不上或 clashxlist 表不存在。检查 PG_HOST/PG_USER/PG_PASSWORD/PG_DB"
fi

# ====== 6. 应用 schema ======
step "应用 schema 升级（幂等）"
docker run --rm -i \
    -e PGPASSWORD="$PG_PASSWORD" \
    -v "$ROOT/sql:/sql:ro" \
    postgres:16-alpine \
    psql -h "$PG_HOST" -p "${PG_PORT:-5432}" -U "$PG_USER" -d "$PG_DB" -f /sql/001_schema.sql
ok "schema 应用完成"

# ====== 7. 启动容器 ======
step "拉镜像 + 构建"
$DC pull mihomo 2>&1 | tail -3
$DC build api tester 2>&1 | tail -5
ok "构建完成"

step "启动容器"
$DC up -d
ok "容器已启动"

# ====== 8. 等 mihomo 就绪 ======
step "等 mihomo 起来"
for i in $(seq 1 60); do
    if $DC exec -T mihomo wget -qO- http://127.0.0.1:9090/version 2>/dev/null | grep -q version; then
        ok "mihomo 就绪（耗时 ${i}s）"
        break
    fi
    sleep 1
    if [[ $i -eq 60 ]]; then
        red "    mihomo 60s 没起来，看日志："
        $DC logs --tail=30 mihomo
        die "mihomo 启动失败"
    fi
done

# ====== 9. smoke test ======
step "Smoke test"

# 9a. mihomo 探活
if $DC exec -T mihomo wget -qO- http://127.0.0.1:9090/version >/dev/null 2>&1; then
    ok "mihomo /version OK"
else
    warn "mihomo /version 失败"
fi

# 9b. API 探活（API 跟 mihomo 共享网络命名空间，所以从 mihomo 容器里 curl 8000）
sleep 3
API_HEALTH=$($DC exec -T mihomo wget -qO- http://127.0.0.1:8000/healthz 2>/dev/null || echo "")
if echo "$API_HEALTH" | grep -q ok; then
    ok "API /healthz OK"
else
    warn "API 还没起来，看日志：$DC logs api"
fi

# ====== 10. 完成 ======
echo
green "=================================================="
green "  random-proxy 部署完成 🎉"
green "=================================================="
echo
echo "  容器 IP:  ${MIHOMO_IP_DEFAULT}（macvlan）"
echo "  API:      http://${MIHOMO_IP_DEFAULT}:8000/healthz"
echo "  代理段:   http://${MIHOMO_IP_DEFAULT}:10000-19999"
echo
echo "  常用命令："
echo "    cd $ROOT"
echo "    $DC logs -f tester     # 看测活进度"
echo "    $DC logs -f api        # 看 API 日志"
echo "    curl http://${MIHOMO_IP_DEFAULT}:8000/proxies/alive"
echo "    curl http://${MIHOMO_IP_DEFAULT}:8000/proxy/12345    # 申请代理"
echo
yellow "  ⚠ 首次测活 5w+ 节点要 15-30 分钟，耐心等"
yellow "  ⚠ unraid 上 macvlan 模式下，宿主机访问不到容器 IP（这是 Linux 内核限制）"
yellow "    要从 unraid 本机调用 API，得在 unraid 上加个 macvlan-shim 或者从别的机器访问"
echo
