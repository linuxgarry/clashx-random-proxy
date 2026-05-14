-- 给 clashxlist 加测活相关字段
ALTER TABLE clashxlist
    ADD COLUMN IF NOT EXISTS status      BOOLEAN,
    ADD COLUMN IF NOT EXISTS country     TEXT,
    ADD COLUMN IF NOT EXISTS exit_ip     TEXT,
    ADD COLUMN IF NOT EXISTS latency_ms  INTEGER,
    ADD COLUMN IF NOT EXISTS last_check  TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_clashxlist_status     ON clashxlist (status);
CREATE INDEX IF NOT EXISTS idx_clashxlist_last_check ON clashxlist (last_check);

-- 端口映射记录表（API 用，重启清空）
CREATE TABLE IF NOT EXISTS proxy_listeners (
    port        INTEGER PRIMARY KEY,
    proxy_id    BIGINT NOT NULL REFERENCES clashxlist(id) ON DELETE CASCADE,
    proxy_name  TEXT,
    country     TEXT,
    exit_ip     TEXT,
    created_at  TIMESTAMPTZ DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_listeners_expires ON proxy_listeners (expires_at);
CREATE INDEX IF NOT EXISTS idx_listeners_proxy   ON proxy_listeners (proxy_id);
