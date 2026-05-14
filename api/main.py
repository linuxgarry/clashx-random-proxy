#!/usr/bin/env python3
"""
random-proxy API 服务

提供端点：
  GET /proxy/{port}    申请/重置一个本地 listener 端口，背后挂随机活节点。
  GET /proxy/{port}/info   查看当前端口绑定的节点信息。
  DELETE /proxy/{port} 释放端口。
  GET /proxies/alive   alive 节点统计。
  GET /healthz         健康检查。

后台任务：每 30s 清理过期 listener，并触发 mihomo reload。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import psycopg
import yaml
from fastapi import FastAPI, HTTPException, Path
from psycopg.rows import dict_row

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("api")

PG_HOST = os.environ["PG_HOST"]
PG_PORT = int(os.environ.get("PG_PORT", "5432"))
PG_USER = os.environ["PG_USER"]
PG_PASSWORD = os.environ["PG_PASSWORD"]
PG_DB = os.environ.get("PG_DB", "proxy")

MIHOMO_URL = os.environ.get("MIHOMO_URL", "http://mihomo:9090")
MIHOMO_PROXY = os.environ.get("MIHOMO_PROXY", "http://mihomo:7890")
MIHOMO_HOST = os.environ.get("MIHOMO_HOST", "mihomo")
MIHOMO_SECRET = os.environ.get("MIHOMO_SECRET", "")
MIHOMO_CONFIG_FILE = os.environ.get("MIHOMO_CONFIG_FILE", "/mihomo/config.yaml")

PORT_MIN = int(os.environ.get("PORT_MIN", "10000"))
PORT_MAX = int(os.environ.get("PORT_MAX", "19999"))
TTL_SECONDS = int(os.environ.get("LISTENER_TTL", "300"))  # 5 分钟
GEO_TIMEOUT = int(os.environ.get("GEO_TIMEOUT", "10"))
GEO_URL = os.environ.get("GEO_URL", "http://ip-api.com/json/?fields=status,country,countryCode,query")

DSN = (
    f"host={PG_HOST} port={PG_PORT} user={PG_USER} "
    f"password={PG_PASSWORD} dbname={PG_DB} connect_timeout=10"
)

# 一把简单的 mutex，避免 reload 风暴
reload_lock = asyncio.Lock()


# ------------------- 工具函数 -------------------

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


# ------------------- mihomo 配置渲染 -------------------

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


def render_config(proxies: list[dict[str, Any]],
                  listeners_db: list[dict[str, Any]]) -> dict[str, Any]:
    proxy_clean = [
        {k: v for k, v in p.items() if not k.startswith("__")} for p in proxies
    ]
    name_by_id: dict[int, str] = {}
    for p in proxies:
        name_by_id[p["__db_id"]] = p["name"]

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
    tmp = MIHOMO_CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp, MIHOMO_CONFIG_FILE)


async def mihomo_reload(client: httpx.AsyncClient) -> None:
    r = await client.put(
        f"{MIHOMO_URL}/configs",
        params={"force": "true"},
        headers=auth_headers(),
        json={"path": MIHOMO_CONFIG_FILE},
        timeout=60,
    )
    if r.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"mihomo reload 失败 {r.status_code}: {r.text[:200]}",
        )


async def rebuild_and_reload() -> None:
    """读 PG → 写 config → reload mihomo。串行化执行。"""
    async with reload_lock:
        with db_conn() as conn, conn.cursor() as cur:
            proxies = fetch_alive_proxies(cur)
            listeners = fetch_active_listeners(cur)
        cfg = render_config(proxies, listeners)
        write_config(cfg)
        async with httpx.AsyncClient() as client:
            await mihomo_reload(client)


# ------------------- 国家信息 -------------------

async def query_geo_via_port(port: int) -> dict[str, Any]:
    """通过 mihomo 上指定端口的 listener 查询出口 IP / 国家。"""
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


# ------------------- 端点逻辑 -------------------

async def assign_port(port: int) -> dict[str, Any]:
    """挑一个 alive 节点，绑定到 port，重写 config 并 reload。"""
    with db_conn() as conn, conn.cursor() as cur:
        # 找一个不在用的 alive 节点；如果全在用，就随便挑一个 alive
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

        # upsert：再次访问相同 port 直接覆盖
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

    # 刷新 mihomo 配置
    await rebuild_and_reload()
    # 给 listener 开起来留点时间
    await asyncio.sleep(0.5)

    # 试着拿国家 / 出口 IP
    geo = await query_geo_via_port(port)

    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            UPDATE proxy_listeners
            SET country = %s, exit_ip = %s
            WHERE port = %s
        """, (geo.get("country"), geo.get("exit_ip"), port))
        # 同时把出口信息回写主表，避免下次再查
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
    """清理过期 listener，需要时才 reload。"""
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


# ------------------- FastAPI 装配 -------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时先 reload 一次，把 DB 状态同步到 mihomo
    try:
        await rebuild_and_reload()
    except Exception:
        log.exception("启动时初次 reload 失败（可能 mihomo 还没起来）")

    task = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="random-proxy", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/proxy/{port}")
async def get_proxy(port: int = Path(..., ge=1, le=65535)):
    validate_port(port)
    return await assign_port(port)


@app.get("/proxy/{port}/info")
async def proxy_info(port: int = Path(..., ge=1, le=65535)):
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
async def release_proxy(port: int = Path(..., ge=1, le=65535)):
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
