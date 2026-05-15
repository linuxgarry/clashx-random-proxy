#!/usr/bin/env python3
"""
clashx-random-proxy 单容器入口：FastAPI + APScheduler 测活。

合并自原来的 api/ 和 tester/，共享一份 PG 连接、一份 mihomo HTTP 客户端。
- HTTP 路由：/proxy/{port} 申请、/proxy/{port}/info 查询、释放、健康、统计
- 后台任务：每 30s 清理过期 listener；每天 cron 跑一次全量测活；启动时也跑一次
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import psycopg
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException, Path as PathParam
from psycopg.rows import dict_row

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("app")

PG_HOST = os.environ["PG_HOST"]
PG_PORT = int(os.environ.get("PG_PORT", "5432"))
PG_USER = os.environ["PG_USER"]
PG_PASSWORD = os.environ["PG_PASSWORD"]
PG_DB = os.environ.get("PG_DB", "proxy")

MIHOMO_API = os.environ.get("MIHOMO_API", "http://127.0.0.1:9090")
MIHOMO_HOST = os.environ.get("MIHOMO_HOST", "127.0.0.1")
MIHOMO_SECRET = os.environ.get("MIHOMO_SECRET", "")
MIHOMO_CONFIG_FILE = os.environ.get("MIHOMO_CONFIG", "/mihomo/config.yaml")

PORT_MIN = int(os.environ.get("PORT_MIN", "10000"))
PORT_MAX = int(os.environ.get("PORT_MAX", "19999"))
TTL_SECONDS = int(os.environ.get("TTL_SECONDS", "300"))
GEO_TIMEOUT = int(os.environ.get("GEO_TIMEOUT", "10"))
GEO_URL = os.environ.get(
    "GEO_URL",
    "http://ip-api.com/json/?fields=status,country,countryCode,query",
)

TEST_URL = os.environ.get("TEST_URL", "http://www.gstatic.com/generate_204")
TEST_TIMEOUT_MS = int(os.environ.get("TEST_TIMEOUT_MS", "5000"))
TEST_CONCURRENCY = int(os.environ.get("TEST_CONCURRENCY", "100"))
DAILY_CRON = os.environ.get("DAILY_CRON", "0 3 * * *")
TZ = os.environ.get("TZ", "Asia/Shanghai")

DSN = (
    f"host={PG_HOST} port={PG_PORT} user={PG_USER} "
    f"password={PG_PASSWORD} dbname={PG_DB} connect_timeout=10"
)

NAME_SANITIZE = re.compile(r"[^A-Za-z0-9._-]+")

# 串行 mihomo 配置写入 + reload，避免 api 路由和 tester 同时改文件
reload_lock = asyncio.Lock()

# 整个进程共用一个 mihomo HTTP 客户端
mihomo_client: httpx.AsyncClient | None = None


def auth_headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if MIHOMO_SECRET:
        h["Authorization"] = f"Bearer {MIHOMO_SECRET}"
    return h


def validate_port(port: int) -> None:
    if port < PORT_MIN or port > PORT_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"端口必须在 {PORT_MIN}-{PORT_MAX} 范围内",
        )


def db_conn():
    return psycopg.connect(DSN, row_factory=dict_row)


def safe_name(raw_name: str, idx: int) -> str:
    base = NAME_SANITIZE.sub("_", raw_name or "").strip("_") or "node"
    return f"{base[:32]}__{idx}"


# ------------------- mihomo 配置渲染（API 用：只写 alive + listener）-------------------

def fetch_alive_proxies(cur) -> list[dict[str, Any]]:
    cur.execute("""
        SELECT id, name, raw FROM clashxlist
        WHERE status = TRUE AND raw IS NOT NULL AND raw <> ''
    """)
    out: list[dict[str, Any]] = []
    for row in cur.fetchall():
        try:
            d = json.loads(row["raw"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(d, dict):
            continue
        d = dict(d)
        d["name"] = f"db{row['id']}-{d.get('name', 'node')}"
        d["__db_id"] = row["id"]
        d.pop("dialer-proxy", None)
        out.append(d)
    return out


def fetch_active_listeners(cur) -> list[dict[str, Any]]:
    cur.execute("""
        SELECT pl.port, pl.proxy_name, c.id AS proxy_id
        FROM proxy_listeners pl
        JOIN clashxlist c ON c.id = pl.proxy_id
        WHERE pl.expires_at > now()
    """)
    return cur.fetchall()


def render_alive_config(proxies: list[dict[str, Any]],
                        listeners_db: list[dict[str, Any]]) -> dict[str, Any]:
    proxy_clean = [
        {k: v for k, v in p.items() if not k.startswith("__")} for p in proxies
    ]
    name_by_id: dict[int, str] = {p["__db_id"]: p["name"] for p in proxies}

    listeners = []
    for entry in listeners_db:
        name = name_by_id.get(entry["proxy_id"])
        if not name:
            continue
        listeners.append({
            "name": f"port-{entry['port']}",
            "type": "mixed",
            "port": entry["port"],
            "listen": "0.0.0.0",
            "proxy": name,
        })

    return {
        "mixed-port": 7890,
        "allow-lan": True,
        "mode": "rule",
        "log-level": "warning",
        "ipv6": False,
        "external-controller": "0.0.0.0:9090",
        "external-controller-cors": {
            "allow-origins": ["*"],
            "allow-private-network": True,
        },
        "secret": MIHOMO_SECRET,
        "proxies": proxy_clean,
        "proxy-groups": [
            {
                "name": "ALIVE",
                "type": "select",
                "proxies": ["DIRECT"] + [p["name"] for p in proxy_clean] if proxy_clean else ["DIRECT"],
            }
        ],
        "listeners": listeners,
        "rules": ["MATCH,ALIVE"],
    }


def write_config(cfg: dict[str, Any]) -> None:
    Path(MIHOMO_CONFIG_FILE).parent.mkdir(parents=True, exist_ok=True)
    tmp = MIHOMO_CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp, MIHOMO_CONFIG_FILE)


async def mihomo_reload() -> None:
    assert mihomo_client is not None
    # 不传 path：app 视角的 /mihomo/config.yaml 在 mihomo 容器里不存在，
    # 让 mihomo 用启动时 -d 指定的默认配置文件路径（同一份挂载点下的同一个文件）
    r = await mihomo_client.put(
        f"{MIHOMO_API}/configs",
        params={"force": "true"},
        headers=auth_headers(),
        json={},
        timeout=60,
    )
    if r.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"mihomo reload 失败 {r.status_code}: {r.text[:200]}",
        )


async def rebuild_and_reload() -> None:
    """读 PG → 写 alive 配置 → reload。串行化。"""
    async with reload_lock:
        with db_conn() as conn, conn.cursor() as cur:
            proxies = fetch_alive_proxies(cur)
            listeners = fetch_active_listeners(cur)
        cfg = render_alive_config(proxies, listeners)
        write_config(cfg)
        await mihomo_reload()


# ------------------- 国家信息 -------------------

async def query_geo_via_port(port: int) -> dict[str, Any]:
    proxy_url = f"http://{MIHOMO_HOST}:{port}"
    try:
        async with httpx.AsyncClient(proxy=proxy_url, timeout=GEO_TIMEOUT) as client:
            r = await client.get(GEO_URL)
        if r.status_code == 200:
            data = r.json()
            if data.get("status") == "success":
                return {
                    "country": data.get("country") or data.get("countryCode"),
                    "country_code": data.get("countryCode"),
                    "exit_ip": data.get("query"),
                }
    except (httpx.HTTPError, asyncio.TimeoutError):
        pass
    return {"country": None, "country_code": None, "exit_ip": None}


# ------------------- API 端点逻辑 -------------------

async def assign_port(port: int) -> dict[str, Any]:
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT id, name FROM clashxlist
            WHERE status = TRUE
              AND id NOT IN (SELECT proxy_id FROM proxy_listeners WHERE expires_at > now())
            ORDER BY random()
            LIMIT 1
        """)
        row = cur.fetchone()
        if row is None:
            cur.execute("""
                SELECT id, name FROM clashxlist
                WHERE status = TRUE
                ORDER BY random()
                LIMIT 1
            """)
            row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=503, detail="数据库里没有 alive 节点，等测活先跑一次")

        proxy_id = row["id"]
        proxy_name = row["name"]
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=TTL_SECONDS)

        cur.execute("""
            INSERT INTO proxy_listeners (port, proxy_id, proxy_name, expires_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (port) DO UPDATE
              SET proxy_id   = EXCLUDED.proxy_id,
                  proxy_name = EXCLUDED.proxy_name,
                  country    = NULL,
                  exit_ip    = NULL,
                  expires_at = EXCLUDED.expires_at,
                  created_at = now()
        """, (port, proxy_id, proxy_name, expires_at))
        conn.commit()

    await rebuild_and_reload()
    await asyncio.sleep(0.5)

    geo = await query_geo_via_port(port)

    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            UPDATE proxy_listeners
            SET country = %s, exit_ip = %s
            WHERE port = %s
        """, (geo.get("country"), geo.get("exit_ip"), port))
        cur.execute("""
            UPDATE clashxlist
            SET country = COALESCE(%s, country),
                exit_ip = COALESCE(%s, exit_ip)
            WHERE id = %s
        """, (geo.get("country"), geo.get("exit_ip"), proxy_id))
        conn.commit()

    return {
        "port": port,
        "name": proxy_name,
        "country": geo.get("country"),
        "country_code": geo.get("country_code"),
        "exit_ip": geo.get("exit_ip"),
        "ttl_seconds": TTL_SECONDS,
        "expires_at": expires_at.isoformat(),
    }


async def cleanup_expired() -> None:
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM proxy_listeners WHERE expires_at <= now() RETURNING port")
        deleted = cur.fetchall()
        conn.commit()
    if deleted:
        log.info("清理过期 listener: %s", [d["port"] for d in deleted])
        try:
            await rebuild_and_reload()
        except Exception:
            log.exception("清理后 reload 失败")


async def cleanup_loop() -> None:
    while True:
        try:
            await cleanup_expired()
        except Exception:
            log.exception("cleanup_loop 异常")
        await asyncio.sleep(30)


# ------------------- 测活逻辑（原 tester） -------------------

def load_all_proxies_from_db() -> list[dict[str, Any]]:
    out = []
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT id, raw FROM clashxlist WHERE raw IS NOT NULL AND raw <> ''")
        for db_id, raw in cur:
            try:
                node = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(node, dict):
                continue
            if not node.get("server") or not node.get("port") or not node.get("type"):
                continue
            node["name"] = safe_name(str(node.get("name", "")), db_id)
            node["__db_id"] = db_id
            node.pop("dialer-proxy", None)
            out.append(node)
    return out


def render_test_config(proxies: list[dict[str, Any]]) -> dict[str, Any]:
    """测活时用的配置：写入所有节点（包括未测过的）。"""
    proxy_clean = [{k: v for k, v in p.items() if not k.startswith("__")} for p in proxies]
    return {
        "mixed-port": 7890,
        "allow-lan": True,
        "mode": "rule",
        "log-level": "warning",
        "ipv6": False,
        "external-controller": "0.0.0.0:9090",
        "external-controller-cors": {
            "allow-origins": ["*"],
            "allow-private-network": True,
        },
        "secret": MIHOMO_SECRET,
        "proxies": proxy_clean,
        "proxy-groups": [
            {
                "name": "ALIVE",
                "type": "select",
                "proxies": ["DIRECT"] + [p["name"] for p in proxy_clean][:1] if proxy_clean else ["DIRECT"],
            }
        ],
        "rules": ["MATCH,ALIVE"],
    }


async def test_one(client: httpx.AsyncClient, name: str) -> int | None:
    try:
        r = await client.get(
            f"{MIHOMO_API}/proxies/{name}/delay",
            params={"timeout": TEST_TIMEOUT_MS, "url": TEST_URL},
            headers=auth_headers(),
        )
    except (httpx.HTTPError, asyncio.TimeoutError):
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except json.JSONDecodeError:
        return None
    delay = data.get("delay")
    if isinstance(delay, int) and delay > 0:
        return delay
    return None


async def run_test() -> None:
    started = datetime.utcnow()
    log.info("=== 测活开始 ===")

    proxies = load_all_proxies_from_db()
    log.info("加载 %d 个节点", len(proxies))
    if not proxies:
        log.warning("没有节点可测")
        return

    async with reload_lock:
        cfg = render_test_config(proxies)
        write_config(cfg)
        await mihomo_reload()

    await asyncio.sleep(3)

    sem = asyncio.Semaphore(TEST_CONCURRENCY)
    results: dict[int, int | None] = {}

    test_client_timeout = TEST_TIMEOUT_MS / 1000 + 5
    async with httpx.AsyncClient(timeout=test_client_timeout) as client:
        async def worker(node):
            async with sem:
                delay = await test_one(client, node["name"])
                results[node["__db_id"]] = delay

        tasks = [asyncio.create_task(worker(n)) for n in proxies]
        done = 0
        for fut in asyncio.as_completed(tasks):
            await fut
            done += 1
            if done % 500 == 0:
                alive = sum(1 for v in results.values() if v is not None)
                log.info("  进度 %d/%d, 当前活的 %d", done, len(proxies), alive)

    alive = sum(1 for v in results.values() if v is not None)
    log.info("测试结束: %d 活 / %d 总", alive, len(results))

    now = datetime.utcnow()
    rows = [(v, v is not None, now, k) for k, v in results.items()]
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.executemany(
            "UPDATE clashxlist SET latency_ms=%s, status=%s, last_check=%s WHERE id=%s",
            rows,
        )
        conn.commit()

    elapsed = (datetime.utcnow() - started).total_seconds()
    log.info("=== 测活完成，耗时 %.1fs ===", elapsed)

    # 测完之后把 mihomo 配置切回 alive-only + 当前 listener
    try:
        await rebuild_and_reload()
    except Exception:
        log.exception("测活后 rebuild_and_reload 失败")


async def run_test_safe() -> None:
    try:
        await run_test()
    except Exception:
        log.exception("测活异常")


# ------------------- FastAPI 装配 -------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global mihomo_client
    mihomo_client = httpx.AsyncClient(timeout=30)

    try:
        await rebuild_and_reload()
    except Exception:
        log.exception("启动时初次 reload 失败（可能 mihomo 还没起来）")

    sched = AsyncIOScheduler(timezone=TZ)
    sched.add_job(run_test_safe, CronTrigger.from_crontab(DAILY_CRON), name="daily-test")
    sched.start()
    log.info("scheduler 启动，cron=%s", DAILY_CRON)

    cleanup_task = asyncio.create_task(cleanup_loop())
    initial_test_task = asyncio.create_task(run_test_safe())

    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        if not initial_test_task.done():
            initial_test_task.cancel()
        sched.shutdown(wait=False)
        await mihomo_client.aclose()


app = FastAPI(title="clashx-random-proxy", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/proxy/{port}")
async def get_proxy(port: int = PathParam(..., ge=1, le=65535)):
    validate_port(port)
    return await assign_port(port)


@app.get("/proxy/{port}/info")
async def proxy_info(port: int = PathParam(..., ge=1, le=65535)):
    validate_port(port)
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT pl.port, pl.proxy_name AS name, pl.country, pl.exit_ip,
                   pl.created_at, pl.expires_at
            FROM proxy_listeners pl
            WHERE pl.port = %s AND pl.expires_at > now()
        """, (port,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="该端口当前没有绑定")
    return row


@app.delete("/proxy/{port}")
async def release_proxy(port: int = PathParam(..., ge=1, le=65535)):
    validate_port(port)
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM proxy_listeners WHERE port = %s RETURNING port", (port,))
        gone = cur.fetchone() is not None
        conn.commit()
    if gone:
        await rebuild_and_reload()
    return {"released": gone, "port": port}


@app.get("/proxies/alive")
async def alive_stats():
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM clashxlist WHERE status = TRUE")
        alive = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM clashxlist")
        total = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM proxy_listeners WHERE expires_at > now()")
        active = cur.fetchone()["n"]
    return {"alive": alive, "total": total, "active_listeners": active}
