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

# ====== 4. 预检：外部网络 ======
step "预检：外部 macvlan 网络"
EXTERNAL_NETWORK="${EXTERNAL_NETWORK:-br0}"
if docker network inspect "$EXTERNAL_NETWORK" >/dev/null 2>&1; then
    NET_DRIVER=$(docker network inspect "$EXTERNAL_NETWORK" --format '{{.Driver}}')
    NET_SUBNET=$(docker network inspect "$EXTERNAL_NETWORK" --format '{{range .IPAM.Config}}{{.Subnet}} {{end}}')
    ok "网络 \"$EXTERNAL_NETWORK\" 存在（driver=$NET_DRIVER, subnet=$NET_SUBNET）"
    if grep -q '^EXTERNAL_NETWORK=' .env; then
        perl -i -pe "s|^EXTERNAL_NETWORK=.*|EXTERNAL_NETWORK=${EXTERNAL_NETWORK}|" .env
    else
        echo "EXTERNAL_NETWORK=${EXTERNAL_NETWORK}" >> .env
    fi
else
    die "找不到外部网络 \"$EXTERNAL_NETWORK\"。
    用 docker network ls 看你已有的 macvlan 网络名，然后：
      EXTERNAL_NETWORK=<那个名字> bash scripts/bootstrap.sh"
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

# ====== 7. 准备 mihomo 配置 ======
step "准备 mihomo 配置"
if [[ ! -f mihomo/config.yaml ]]; then
    if [[ ! -f mihomo/config.template.yaml ]]; then
        die "缺少 mihomo/config.template.yaml，仓库可能不完整"
    fi
    cp mihomo/config.template.yaml mihomo/config.yaml
    ok "已从 config.template.yaml 生成 config.yaml"
else
    ok "mihomo/config.yaml 已存在，跳过"
fi

# ====== 8. 启动容器 ======
step "构建镜像（含 mihomo 二进制）"
$DC build app 2>&1 | tail -5
ok "构建完成"

step "启动容器"
$DC up -d
ok "容器已启动"

# ====== 8. 等容器就绪 ======
step "等 app 起来（mihomo + uvicorn 同容器）"
for i in $(seq 1 60); do
    if $DC exec -T app curl -fsS http://127.0.0.1:8000/healthz >/dev/null 2>&1; then
        ok "app 就绪（耗时 ${i}s）"
        break
    fi
    sleep 1
    if [[ $i -eq 60 ]]; then
        red "    60s 没起来，看日志："
        $DC logs --tail=30 app
        die "启动失败"
    fi
done

# ====== 9. smoke test ======
step "Smoke test"

# mihomo /version 在配了 secret 后要鉴权，这里用 /proxies 探活（任何 GET 在无 secret 时 200，配了 secret 时 401 也证明在线）
MIHOMO_HTTP=$($DC exec -T app curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:9090/proxies 2>/dev/null || echo "000")
if [[ "$MIHOMO_HTTP" == "200" || "$MIHOMO_HTTP" == "401" ]]; then
    ok "mihomo 在线（HTTP $MIHOMO_HTTP）"
else
    warn "mihomo 探活失败（HTTP $MIHOMO_HTTP）"
fi

API_HEALTH=$($DC exec -T app curl -fsS http://127.0.0.1:8000/healthz 2>/dev/null || echo "")
if echo "$API_HEALTH" | grep -q ok; then
    ok "API /healthz OK"
else
    warn "app 还没起来，看日志：$DC logs app"
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
echo "    $DC logs -f app        # mihomo + API + 测活日志（同容器）"
echo "    curl http://${MIHOMO_IP_DEFAULT}:8000/proxies/alive"
echo "    curl http://${MIHOMO_IP_DEFAULT}:8000/proxy/12345    # 申请代理"
echo
yellow "  ⚠ 首次测活 5w+ 节点要 15-30 分钟，耐心等"
yellow "  ⚠ unraid 上 macvlan 模式下，宿主机访问不到容器 IP（这是 Linux 内核限制）"
yellow "    要从 unraid 本机调用 API，得在 unraid 上加个 macvlan-shim 或者从别的机器访问"
echo
