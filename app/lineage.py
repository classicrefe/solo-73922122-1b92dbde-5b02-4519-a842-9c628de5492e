"""转线登记与“按采集时所属产线”的历史归属查询。

数据模型（见 sql/lineage.sql，应用启动时幂等执行，旧数据卷也能自动补建）：
- meters.line_name 仍是表计的**当前**产线（只存现值）；
- meter_line_transfers 记录每次转线：(meter_id, effective_at, target_line)，
  同一表号同一时刻至多一条；
- 某表号**首次**登记时，额外写入 effective_at = 公元 1 年 UTC 的基线行，
  target_line 取登记时刻的 meters.line_name（原产线），
  使首次生效时刻之前的读数仍能归到原产线。

归属规则（生效时刻的读数归新线）：
    读数 r 归属到满足 effective_at <= r.read_time 的最近一条登记的 target_line。

并发与原子性：
- 登记在单事务内完成，先对 meters 行 SELECT ... FOR UPDATE 串行化同表号的
  并发补录，再做查重 / 首登基线 / 插入，任一检查失败整体 ROLLBACK，
  不会留下部分变更；
- 主键 (meter_id, effective_at) 是最后一道防线，并发下同值重试幂等、
  异值由唯一冲突 + 行内容比对拒绝（409）。

查询侧：
- 单表按 UTC 日期查询，REPEATABLE READ 只读事务固定一个已提交快照；
  readings 经独立 SQL 更正后，本接口每次调用都从最新的已提交快照重新归属。
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import psycopg

from .config import settings

# 首次登记的基线时刻：公元 1 年 UTC。读数时刻不可能早于它，
# 因此基线行对所有真实读数都满足 effective_at <= read_time。
# 不用 PostgreSQL 的 '-infinity'：psycopg3 无法把它解码为 Python datetime。
BASELINE_EFFECTIVE_AT = datetime(1, 1, 1, tzinfo=timezone.utc)
_BASELINE_SQL = "TIMESTAMPTZ '0001-01-01 00:00:00+00'"

# 启动时幂等补建的结构（与 sql/lineage.sql 保持一致；旧数据卷首次启动新代码时生效）
LINEAGE_DDL = f"""
CREATE TABLE IF NOT EXISTS meter_line_transfers (
    meter_id     bigint NOT NULL REFERENCES meters(meter_id),
    effective_at timestamptz NOT NULL,
    target_line  text   NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (meter_id, effective_at),
    CONSTRAINT mlt_target_line_not_blank CHECK (length(btrim(target_line)) > 0),
    CONSTRAINT mlt_effective_at_not_before_baseline
        CHECK (effective_at >= {_BASELINE_SQL})
);

CREATE INDEX IF NOT EXISTS idx_mlt_meter_eff
    ON meter_line_transfers (meter_id, effective_at DESC);
"""


class LineageError(RuntimeError):
    """转线登记 / 归属查询的领域错误基类。"""


class MeterNotFound(LineageError):
    def __init__(self, meter_id: int):
        super().__init__(f"表号 {meter_id} 不存在于 meters")
        self.meter_id = meter_id


class LineageConflict(LineageError):
    """同表号同时刻已登记异值，或补录与最新归属矛盾。HTTP 映射为 409。"""


def ensure_schema() -> None:
    """应用启动时调用：幂等补建转线登记表（沿用旧数据卷也安全）。"""
    conn = psycopg.connect(settings.pg_dsn, connect_timeout=settings.connect_timeout)
    try:
        conn.execute(LINEAGE_DDL)
        conn.commit()
    finally:
        conn.close()


def _open_write_conn() -> psycopg.Connection:
    # 登记是写事务：autocommit=False，事务边界显式控制。
    # lock_timeout 给同表号串行锁一个上限：生产环境事务都很短，
    # 极端排队下快速失败（映射为可重试 409）而不是无限期占用连接。
    conn = psycopg.connect(
        settings.pg_dsn,
        connect_timeout=settings.connect_timeout,
        application_name="recon-api-lineage",
    )
    conn.execute(f"SET SESSION lock_timeout = {int(settings.lock_timeout_ms)}")
    return conn


def register_transfer(meter_id: int, effective_at: datetime, target_line: str) -> dict:
    """登记一次转线，返回事务提交后的登记状态。

    - 表号不存在 -> MeterNotFound（404）；
    - 同表号同时刻已存在：同值幂等返回（idempotent=True），异值拒绝（409）；
    - 首次登记：补一条公元 1 年 UTC 基线行保留原产线；
    - 补录更早转线：必须与 meters.line_name 的最新归属一致，否则 409；
    - 全程单事务，失败回滚，不留部分变更。
    """
    effective_at = _as_utc(effective_at)
    if effective_at < BASELINE_EFFECTIVE_AT:
        raise LineageConflict(
            f"生效时刻 {effective_at.isoformat()} 早于基线时刻 "
            f"{BASELINE_EFFECTIVE_AT.isoformat()}，不允许登记"
        )
    conn = _open_write_conn()
    try:
        # 1) 串行化同表号的并发补录，并确认表号存在
        row = conn.execute(
            "SELECT line_name FROM meters WHERE meter_id = %s FOR UPDATE",
            (meter_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            raise MeterNotFound(meter_id)
        current_line = row[0]

        # 2) 同时刻查重：同值幂等，异值拒绝
        existing = conn.execute(
            "SELECT target_line FROM meter_line_transfers "
            "WHERE meter_id = %s AND effective_at = %s",
            (meter_id, effective_at),
        ).fetchone()
        if existing is not None:
            if existing[0] == target_line:
                conn.rollback()  # 无写入，结束锁等待
                return {
                    "meter_id": meter_id,
                    "effective_at": effective_at,
                    "target_line": target_line,
                    "idempotent": True,
                    "baseline_line": None,
                }
            conn.rollback()
            raise LineageConflict(
                f"表号 {meter_id} 在 {effective_at.isoformat()} 已登记产线 "
                f"{existing[0]!r}，与本次提交的 {target_line!r} 不一致，已拒绝"
            )

        baseline_line: str | None = None
        # 基线行（公元 1 年 UTC）不算业务转线，判断“是否已有登记 / 是否补录更早”
        # 时要排除它
        earliest = conn.execute(
            "SELECT min(effective_at) FROM meter_line_transfers "
            "WHERE meter_id = %s AND effective_at > %s",
            (meter_id, BASELINE_EFFECTIVE_AT),
        ).fetchone()
        has_any = earliest is not None and earliest[0] is not None

        if not has_any:
            # 3a) 首次登记：保留原产线供更早读数使用
            baseline_line = current_line
            conn.execute(
                "INSERT INTO meter_line_transfers "
                f"(meter_id, effective_at, target_line) VALUES (%s, {_BASELINE_SQL}, %s)",
                (meter_id, current_line),
            )
        else:
            # 3b) 补录一条比所有既有登记更早的转线：
            # 历史归属的最新一段必须与 meters.line_name 的最新归属一致
            if effective_at < earliest[0]:
                latest_row = conn.execute(
                    "SELECT target_line FROM meter_line_transfers "
                    "WHERE meter_id = %s ORDER BY effective_at DESC LIMIT 1",
                    (meter_id,),
                ).fetchone()
                latest_line = latest_row[0] if latest_row else None
                if latest_line != current_line:
                    conn.rollback()
                    raise LineageConflict(
                        f"补录的更早转线与最新归属不一致：meters.line_name="
                        f"{current_line!r}，而最新登记产线为 {latest_line!r}，"
                        "请先更正 meters 或既有登记"
                    )

        # 4) 写入本次登记（主键兜底并发竞争）
        try:
            conn.execute(
                "INSERT INTO meter_line_transfers "
                "(meter_id, effective_at, target_line) VALUES (%s, %s, %s)",
                (meter_id, effective_at, target_line),
            )
        except psycopg.errors.UniqueViolation as exc:
            conn.rollback()
            # 并发下另一事务已插入同时刻登记：再读一次区分同值 / 异值
            return _recheck_conflict(meter_id, effective_at, target_line, exc)

        conn.commit()
        return {
            "meter_id": meter_id,
            "effective_at": effective_at,
            "target_line": target_line,
            "idempotent": False,
            "baseline_line": baseline_line,
        }
    except LineageError:
        raise
    except psycopg.errors.LockNotAvailable as exc:
        # 同表号串行锁等待超过 lock_timeout：数据未改，调用方可安全重试
        raise LineageConflict(
            f"表号 {meter_id} 的登记正在排队（lock_timeout="
            f"{settings.lock_timeout_ms}ms），本次未写入，请重试"
        ) from exc
    except Exception as exc:
        conn.rollback()
        raise LineageError(f"登记失败：{exc.__class__.__name__}: {exc}") from exc
    finally:
        conn.close()


def _recheck_conflict(
    meter_id: int, effective_at: datetime, target_line: str, original: Exception
) -> dict:
    probe = _open_write_conn()
    try:
        row = probe.execute(
            "SELECT target_line FROM meter_line_transfers "
            "WHERE meter_id = %s AND effective_at = %s",
            (meter_id, effective_at),
        ).fetchone()
    finally:
        probe.close()
    if row is not None and row[0] == target_line:
        # 并发提交的是同值登记：视为可重试的幂等成功
        return {
            "meter_id": meter_id,
            "effective_at": effective_at,
            "target_line": target_line,
            "idempotent": True,
            "baseline_line": None,
        }
    other = row[0] if row is not None else None
    raise LineageConflict(
        f"表号 {meter_id} 在 {effective_at.isoformat()} 已登记产线 "
        f"{other!r}，与本次提交的 {target_line!r} 不一致，已拒绝"
    ) from original


def query_daily_attribution(meter_id: int, day: date, limit: int) -> list[dict] | None:
    """按 UTC 日期返回单表读数及其采集时刻所属产线。

    整个查询在一个 REPEATABLE READ 只读事务内完成，绑定调用时的已提交快照；
    readings 经独立 SQL 更正并提交后，再次调用会从新的已提交快照重新归属。
    边界：[day 00:00 UTC, day+1 00:00 UTC)；生效时刻的读数归新线（<= 比较）。

    返回：
    - None：表号不存在于 meters（HTTP 404）；
    - []  ：表号存在且已登记，但当日 UTC 无读数；
    - 其余：归属后的读数行。
    表号存在但从未登记转线时抛 LineageConflict（无法确定历史归属）。
    """
    day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    conn = psycopg.connect(
        settings.pg_dsn,
        connect_timeout=settings.connect_timeout,
        application_name="recon-api-lineage",
        autocommit=True,
    )
    try:
        conn.execute(
            f"SET SESSION statement_timeout = {int(settings.statement_timeout_ms)}"
        )
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        try:
            meter_row = conn.execute(
                "SELECT 1 FROM meters WHERE meter_id = %s", (meter_id,)
            ).fetchone()
            if meter_row is None:
                conn.execute("ROLLBACK")
                return None

            transfer_cnt = conn.execute(
                "SELECT count(*) FROM meter_line_transfers WHERE meter_id = %s",
                (meter_id,),
            ).fetchone()[0]
            if transfer_cnt == 0:
                conn.execute("ROLLBACK")
                raise LineageConflict(
                    f"表号 {meter_id} 尚未登记任何转线，无法确定历史归属；"
                    "请先 POST /meters/{meter_id}/transfers（首次登记会保留原产线）"
                )

            rows = conn.execute(
                """
                SELECT r.read_time,
                       r.wh,
                       att.line_name AS line_name
                FROM readings AS r
                CROSS JOIN LATERAL (
                    SELECT t.target_line AS line_name
                    FROM meter_line_transfers AS t
                    WHERE t.meter_id = r.meter_id
                      AND t.effective_at <= r.read_time
                    ORDER BY t.effective_at DESC
                    LIMIT 1
                ) AS att
                WHERE r.meter_id = %s
                  AND r.read_time >= %s
                  AND r.read_time <  (%s::timestamptz + INTERVAL '1 day')
                ORDER BY r.read_time
                LIMIT %s
                """,
                (meter_id, day_start, day_start, limit),
            ).fetchall()
        except Exception:
            # 回滚失败不应掩盖原始异常
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()

    return [
        {
            "read_time": ts.astimezone(timezone.utc),
            "wh": wh,
            "line_name": line_name,
        }
        for ts, wh, line_name in rows
    ]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        # Pydantic 层已强制要求带时区偏移，这里仅作防御
        raise LineageError("effective_at 必须带时区偏移（建议 UTC，如 2026-11-15T00:00:00Z）")
    return value.astimezone(timezone.utc)
