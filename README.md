# 🎲 random-proxy

> 把数据库里几万个 clash 节点变成一个**按端口随机分配出口 IP** 的代理池。
> 跑在 unraid 上，全 docker 化。

---

## ✨ 这是个啥

你有一张 PostgreSQL 表 `clashxlist`，里面塞了一堆 clash 协议节点（vmess / vless / trojan / ss / hysteria2 ...）。这个项目做三件事：

- 🩺 **每天自动测活**：清晨 3 点把所有节点跑一遍 `generate_204` 测速，活的标 `status=true`，死的标 `false`。
- 🎯 **端口即出口**：访问 `GET /proxy/12345`，自动从活的节点里随机抽一个，绑到本机 12345 端口。你拿这个端口当 HTTP 代理用就行。
- 🔄 **重复访问 = 换 IP**：再请求一次 `/proxy/12345` 就换一个新节点。需要换 IP 时刷一下接口即可。

---

## 🧱 架构

```
                 ┌─────────────────────────────────────┐
                 │   unraid (192.168.5.100, macvlan)   │
                 │                                     │
   你的脚本 ───▶  │   :10000-:19999  (动态 listener)    │
                 │   :8000          (FastAPI)          │
                 │   :9090          (mihomo API,内部)  │
                 │                                     │
                 │   mihomo  +  api  +  tester         │
                 └──────────────┬──────────────────────┘
                                │
                       ┌────────▼─────────┐
                       │  PostgreSQL      │
                       │  192.168.5.20    │
                       │  proxy.clashxlist│
                       └──────────────────┘
```

三个容器共享一个 IP（192.168.5.100），分工：

| 容器 | 职责 |
|---|---|
| 🟢 `mihomo` | 真正的代理核心。监听 10000-19999 区间的动态 listener |
| 🟡 `api` | FastAPI 服务，处理 `/proxy/{port}`，按需改 mihomo 配置 |
| 🔵 `tester` | 后台 daemon，每天 03:00 把所有节点测一遍 |

---

## 🚀 快速上手

### 1. 准备数据库

确保 `proxy.clashxlist` 已经存在（从 `proxylist.csv` 导进来的那张表），然后跑 schema 升级：

```bash
psql -h 192.168.5.20 -U garry -d proxy -f sql/001_schema.sql
```

会加这几列：`status / country / exit_ip / latency_ms / last_check`，并创建 `proxy_listeners` 表。

### 2. 创建 unraid 上的 macvlan 网络

让 mihomo 容器能拿到固定 IP `192.168.5.100`，跟 LAN 内其他设备直接互通：

```bash
docker network create -d macvlan \
  --subnet=192.168.5.0/24 \
  --gateway=192.168.5.1 \
  --ip-range=192.168.5.96/29 \
  -o parent=br0 \
  lan_macvlan
```

> ⚠️ `parent=br0` 是 unraid 默认桥的名字，不一样的话改成你网卡的实际名字（`ip link` 看下）。
> ⚠️ macvlan 网段要避开 DHCP 池，不然容易 IP 冲突。

### 3. 配置环境变量

```bash
cp .env.example .env
vim .env
```

至少改这几项：

```env
PG_PASSWORD=你的真密码
MIHOMO_SECRET=自己随便编一个长串
```

### 4. 启动

```bash
docker compose up -d
docker compose logs -f
```

第一次启动 tester 会立即跑一轮全量测活（5 万节点大概 15-30 分钟），跑完之后才有 alive 池可用。

---

## 🎮 使用

**拿一个新代理（端口随便挑，10000-19999 之间）：**

```bash
$ curl http://192.168.5.100:8000/proxy/12345
{
  "port": 12345,
  "name": "🇯🇵 Tokyo-01",
  "country": "JP",
  "exit_ip": "203.0.113.42",
  "proxy_url": "http://192.168.5.100:12345",
  "expires_in": 300,
  "note": "再次请求该端口会换一个节点"
}
```

**拿 proxy_url 当代理用：**

```bash
curl -x http://192.168.5.100:12345 https://ifconfig.me
# 203.0.113.42
```

**想换 IP？再请求一次同一个端口：**

```bash
curl http://192.168.5.100:8000/proxy/12345
# country / exit_ip 都变了
```

**用完释放：**

```bash
curl -X DELETE http://192.168.5.100:8000/proxy/12345
```

不释放也行，5 分钟没新请求会自动回收。

**看池子健康度：**

```bash
$ curl http://192.168.5.100:8000/health
{
  "ok": true,
  "alive_proxies": 8423,
  "active_listeners": 17,
  "port_range": [10000, 19999],
  "ttl_seconds": 300
}
```

---

## 🛠️ 配置项

`.env` 里的关键变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `PG_HOST/PORT/USER/PASSWORD/DB` | - | 数据库连接 |
| `MIHOMO_IP` | 192.168.5.100 | mihomo 容器在 LAN 上的 IP |
| `MIHOMO_SECRET` | - | mihomo API 密码，**强烈建议改** |
| `PORT_MIN / PORT_MAX` | 10000 / 19999 | 动态 listener 端口段 |
| `LISTENER_TTL` | 300 | 端口闲置多久回收（秒） |
| `TEST_BATCH_SIZE` | 500 | 每批喂给 mihomo 的节点数 |
| `TEST_CONCURRENCY` | 100 | 测延迟的并发 |
| `TEST_CRON_HOUR` | 3 | 每天几点开始测活 |

---

## 🧪 调试

### 看 mihomo 实时日志

```bash
docker compose logs -f mihomo
```

### 手动触发一次测活

```bash
docker compose exec tester python -c "import asyncio; from tester import run_one_round; asyncio.run(run_one_round())"
```

### 看当前激活的 listener

```sql
SELECT port, proxy_name, country, exit_ip, expires_at
FROM proxy_listeners
WHERE expires_at > now()
ORDER BY expires_at;
```

### 死了多少节点

```sql
SELECT
  count(*) FILTER (WHERE status=true)  AS alive,
  count(*) FILTER (WHERE status=false) AS dead,
  count(*) FILTER (WHERE status IS NULL) AS untested,
  count(*) AS total
FROM clashxlist;
```

---

## ⚠️ 几个坑预先告诉你

- **每次 `/proxy/{port}` 调用都会触发 mihomo reload**，节点多的时候 reload 要 1-2 秒。高频换 IP 不适合这个架构。
- **macvlan 容器从 unraid 主机自身访问不通**，这是 macvlan 的固有限制。要从 unraid 自己测试，加一个 macvlan-shim 接口。
- **节点的 `name` 是 `db<id>-<原名>`**，加 id 前缀避免重名。`/proxy/{port}` 返回的 `name` 字段是 db 里的原名。
- **测活只判通断，不查地理位置**。country / exit_ip 是 API 在 listener 起好之后通过它请求 ipinfo.io 现拿的。

---

## 📁 项目结构

```
random-proxy/
├── api/              # FastAPI 服务
│   ├── Dockerfile
│   ├── requirements.txt
│   └── main.py
├── tester/           # 测活 daemon
│   ├── Dockerfile
│   ├── requirements.txt
│   └── tester.py
├── mihomo/           # mihomo 配置目录（容器挂载点）
│   └── config.yaml
├── sql/
│   └── 001_schema.sql
├── docker-compose.yml
├── .env.example
└── .gitignore
```

---

## 📜 协议

私有项目，仅自用。
