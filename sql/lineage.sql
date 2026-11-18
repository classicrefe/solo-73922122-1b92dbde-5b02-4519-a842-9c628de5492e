-- ============================================================
-- 转线登记表（幂等）
-- 首次启动由 init.sql 通过 \ir 引用执行；
-- 沿用旧数据卷（init.sql 不会重跑）时由应用启动时执行同样的 DDL 补建。
-- ============================================================

-- 转线登记：表号 + 生效时刻 -> 目标产线
-- 同一表号同一时刻至多一条；首次登记会另写一条 effective_at=公元 1 年 UTC
-- 的基线行，保留首次登记时 meters.line_name 作为原产线，
-- 供更早读数的历史归属使用。
-- （不使用 '-infinity'：psycopg3 无法将其解码为 Python datetime。）
CREATE TABLE IF NOT EXISTS meter_line_transfers (
    meter_id     bigint NOT NULL REFERENCES meters(meter_id),
    effective_at timestamptz NOT NULL,
    target_line  text   NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (meter_id, effective_at),
    CONSTRAINT mlt_target_line_not_blank CHECK (length(btrim(target_line)) > 0),
    CONSTRAINT mlt_effective_at_not_before_baseline
        CHECK (effective_at >= TIMESTAMPTZ '0001-01-01 00:00:00+00')
);

-- 归属查询按表号、生效时刻倒序取最近一条
CREATE INDEX IF NOT EXISTS idx_mlt_meter_eff
    ON meter_line_transfers (meter_id, effective_at DESC);
