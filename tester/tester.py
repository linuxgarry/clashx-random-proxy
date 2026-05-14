#!/usr/bin/env python3
"""
代理测活 daemon。
- 启动后立即跑一次，之后每天 03:00 跑（cron）。
- 流程：
  1. 从 PG 读 clashxlist 的 raw 字段（YAML 节点 JSON 化后存的）
  2. 把所有节点写入 mihomo config.yaml 的 proxies: 段
  3. PUT /configs 让 mihomo 热加载
  4. 并发调 /proxies/{name}/delay 测每个节点 generate_204 延迟
  5. 回写 status / latency_ms / last_check
- 不在 tester 阶段查 country/exit_ip，留给 API 按需查（避免 5w 节点全跑一遍）
"""
import asyncio
import json
import logging
import os
import random
import re
import string
import sys
from datetime import datetime
from pathlib import Path

import httpx
import psycopg
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("tester")

PG_HOST = os.environ["PG_HOST"]
PG_PORT = int(os.environ.get("PG_PORT", "5432"))
PG_USER = os.environ["PG_USER"]
PG_PASSWORD = os.environ["PG_PASSWORD"]
PG_DB = os.environ.get("PG_DB", "proxy")

MIHOMO_API = os.environ.get("MIHOMO_API", "http://mihomo:9090")
MIHOMO_SECRET = os.environ.get("MIHOMO_SECRET", "")
CONFIG_PATH = Path(os.environ.get("MIHOMO_CONFIG", "/mihomo/config.yaml"))

TEST_URL = os.environ.get("TEST_URL", "http://www.gstatic.com/generate_204")
TEST_TIMEOUT_MS = int(os.environ.get("TEST_TIMEOUT_MS", "5000"))
TEST_CONCURRENCY = int(os.environ.get("TEST_CONCURRENCY", "100"))
DAILY_CRON = os.environ.get("DAILY_CRON", "0 3 * * *")  # 03:00 every day

# 节点名要符合 mihomo 规范，避免特殊字符把 yaml 弄花
NAME_SANITIZE = re.compile(r"[^A-Za-z0-9._-]+")


def conn_str() -> str:
    return f"host={PG_HOST} port={PG_PORT} user={PG_USER} password={PG_PASSWORD} dbname={PG_DB}"


def auth_headers() -> dict:
    h = {"Content-Type": "application/json"}
    if MIHOMO_SECRET:
        h["Authorization"] = f"Bearer {MIHOMO_SECRET}"
    return h


def safe_name(raw_name: str, idx: int) -> str:
    base = NAME_SANITIZE.sub("_", raw_name or "").strip("_") or "node"
    # 加 id 后缀避免重名
    return f"{base[:32]}__{idx}"


def load_proxies_from_db() -> list[dict]:
    """从 clashxlist 读出能解析的 yaml 节点 dict。"""
    out = []
    with psycopg.connect(conn_str()) as conn, conn.cursor() as cur:
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
            out.append(node)
    return out


def write_mihomo_config(proxies: list[dict], listeners: list[dict] | None = None) -> None:
    """生成 mihomo 配置写入磁盘。proxies 段已经清洗过 name。"""
    cfg = {
        "mixed-port": 7890,
        "allow-lan": True,
        "mode": "rule",
        "log-level": "info",
        "ipv6": False,
        "external-controller": "0.0.0.0:9090",
        "external-controller-cors": {
            "allow-origins": ["*"],
            "allow-private-network": True,
        },
        "secret": MIHOMO_SECRET,
        "dns": {
            "enable": True,
            "listen": "0.0.0.0:1053",
            "ipv6": False,
            "enhanced-mode": "fake-ip",
            "fake-ip-range": "198.18.0.1/16",
            "default-nameserver": ["223.5.5.5", "119.29.29.29"],
            "nameserver": [
                "https://1.1.1.1/dns-query",
                "https://8.8.8.8/dns-query",
            ],
        },
        "proxies": [{k: v for k, v in p.items() if not k.startswith("__")} for p in proxies],
        "proxy-groups": [
            {"name": "ALIVE", "type": "select", "proxies": ["DIRECT"] + [p["name"] for p in proxies][:1] or ["DIRECT"]},
        ],
        "rules": ["MATCH,ALIVE"],
    }
    if listeners:
        cfg["listeners"] = listeners
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    tmp.replace(CONFIG_PATH)


async def reload_mihomo() -> bool:
    async with httpx.AsyncClient(timeout=30) as client:
        # 让 mihomo 从磁盘重读配置文件
        r = await client.put(
            f"{MIHOMO_API}/configs",
            params={"force": "true"},
            headers=auth_headers(),
            json={"path": str(CONFIG_PATH)},
        )
        if r.status_code >= 400:
            log.error("mihomo reload failed: %s %s", r.status_code, r.text[:200])
            return False
    return True


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

    proxies = load_proxies_from_db()
    log.info("加载 %d 个节点", len(proxies))
    if not proxies:
        log.warning("没有节点可测")
        return

    write_mihomo_config(proxies)
    if not await reload_mihomo():
        log.error("reload 失败，跳过本次测试")
        return
    # 给 mihomo 一点时间消化大配置
    await asyncio.sleep(3)

    sem = asyncio.Semaphore(TEST_CONCURRENCY)
    results: dict[int, int | None] = {}

    async with httpx.AsyncClient(timeout=TEST_TIMEOUT_MS / 1000 + 5) as client:
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

    # 回写 DB
    now = datetime.utcnow()
    rows = [(v, v is not None, now, k) for k, v in results.items()]
    with psycopg.connect(conn_str()) as conn, conn.cursor() as cur:
        cur.executemany(
            "UPDATE clashxlist SET latency_ms=%s, status=%s, last_check=%s WHERE id=%s",
            rows,
        )
        conn.commit()

    elapsed = (datetime.utcnow() - started).total_seconds()
    log.info("=== 测活完成，耗时 %.1fs ===", elapsed)


async def main() -> None:
    # 启动时立即跑一次
    try:
        await run_test()
    except Exception:
        log.exception("首次测活异常")

    sched = AsyncIOScheduler(timezone=os.environ.get("TZ", "Asia/Shanghai"))
    sched.add_job(run_test, CronTrigger.from_crontab(DAILY_CRON), name="daily-test")
    sched.start()
    log.info("scheduler 启动，cron=%s", DAILY_CRON)

    # 阻塞
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
