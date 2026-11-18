"""FastAPI 应用入口与 HTTP 路由。"""
from __future__ import annotations

import contextlib
from datetime import date

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from . import db as dbmod
from . import lineage
from .config import settings
from .lineage import LineageConflict, LineageError, MeterNotFound
from .manager import SlotBusyError, manager
from .models import (
    AttributionOut,
    AttributionRow,
    CreateJobRequest,
    JobOut,
    TransferOut,
    TransferRequest,
)
from .sqlvalidation import SqlValidationError


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    # 幂等补建转线登记表：首次启动由 init.sql 建表，沿用旧数据卷时由这里兜底，
    # 保证 docker compose 首次及复用数据卷启动后 /docs 均可演示新接口。
    lineage.ensure_schema()
    yield


app = FastAPI(
    title="电表日结 SQL 核对与转线归属 API",
    description=(
        "核对：提交两条只读 SELECT，基于同一已提交快照并行读取，"
        "按行内容与出现次数做有序多重集核对（忽略行序，NULL≠空串），"
        "差异最多展示 20 种。单轮 30 秒，全局同时只允许一个任务。\n\n"
        "转线：登记表号、生效时刻与目标产线；首次登记保留原产线供更早读数使用，"
        "并可按 UTC 日期查询单表每个读数采集时刻所属的产线。"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["运维"])
def health() -> dict:
    """存活探针；不触碰数据库。"""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 核对任务（原有接口，行为不变）
# ---------------------------------------------------------------------------

@app.get("/jobs", response_model=list[JobOut], tags=["核对任务"])
def list_jobs() -> list[JobOut]:
    """列出近期任务（含终态），状态查询始终可用。"""
    return manager.list_jobs()


@app.post("/jobs", response_model=JobOut, status_code=201, tags=["核对任务"])
def create_job(payload: CreateJobRequest) -> JobOut:
    try:
        job = manager.create_job(payload.left_sql, payload.right_sql, payload.name)
    except SqlValidationError as exc:
        raise HTTPException(status_code=400, detail=f"SQL 校验失败：{exc}") from exc
    except SlotBusyError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "当前已有核对任务在运行，一次只允许一组",
                "active_job_id": exc.active_job_id,
            },
        ) from exc
    return job.snapshot()


@app.get("/jobs/{job_id}", response_model=JobOut, tags=["核对任务"])
def get_job(job_id: str) -> JobOut:
    job = manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job.snapshot()


@app.post("/jobs/{job_id}/cancel", response_model=JobOut, tags=["核对任务"])
def cancel_job(job_id: str) -> JobOut:
    try:
        job = manager.request_cancel(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    return job.snapshot()


# ---------------------------------------------------------------------------
# 转线登记与历史归属
# ---------------------------------------------------------------------------

@app.post(
    "/meters/{meter_id}/transfers",
    response_model=TransferOut,
    status_code=201,
    tags=["转线归属"],
)
def register_transfer(meter_id: int, payload: TransferRequest) -> TransferOut:
    """登记一次转线。

    - 路径中的表号与请求体表号必须一致；
    - 首次登记自动保留原产线（写入公元 1 年 UTC 的基线行）；
    - 同表号同时刻同值重试幂等返回 201（idempotent=true），异值拒绝 409；
    - 单事务提交，失败回滚，不留部分变更。
    """
    if payload.meter_id != meter_id:
        raise HTTPException(
            status_code=400,
            detail=f"路径表号 {meter_id} 与请求体表号 {payload.meter_id} 不一致",
        )
    try:
        out = lineage.register_transfer(
            meter_id=meter_id,
            effective_at=payload.effective_at,
            target_line=payload.target_line,
        )
    except MeterNotFound as exc:
        raise HTTPException(status_code=404, detail={"message": str(exc)}) from exc
    except LineageConflict as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc)}) from exc
    except LineageError as exc:
        raise HTTPException(status_code=422, detail={"message": str(exc)}) from exc
    return TransferOut(**out)


@app.get(
    "/meters/{meter_id}/readings",
    response_model=AttributionOut,
    tags=["转线归属"],
)
def get_meter_readings(
    meter_id: int,
    date: date = Query(
        ...,
        alias="date",
        description="UTC 日期（YYYY-MM-DD），按 [当日 00:00, 次日 00:00) UTC 过滤",
    ),
    limit: int = Query(
        default=settings.attribution_row_limit,
        ge=1,
        le=settings.attribution_row_limit,
        description=f"单次返回上限（最大 {settings.attribution_row_limit}）",
    ),
) -> AttributionOut:
    """单表按 UTC 日期查询读数及采集时刻所属产线。

    每次调用都在新的 REPEATABLE READ 只读事务中读取当前已提交快照；
    readings 经独立 SQL 更正提交后，再次调用会基于新快照重新归属。
    无转线登记时返回 409，提示先登记（首次登记会保留原产线）。
    """
    try:
        rows = lineage.query_daily_attribution(meter_id, date, limit + 1)
    except MeterNotFound as exc:
        raise HTTPException(status_code=404, detail={"message": str(exc)}) from exc
    except LineageConflict as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc)}) from exc
    except LineageError as exc:
        raise HTTPException(status_code=422, detail={"message": str(exc)}) from exc

    if rows is None:
        raise HTTPException(
            status_code=404,
            detail={"message": f"表号 {meter_id} 不存在于 meters"},
        )

    truncated = len(rows) > limit
    if truncated:
        rows = rows[:limit]
    return AttributionOut(
        meter_id=meter_id,
        date=date,
        count=len(rows),
        truncated=truncated,
        limit=limit,
        rows=[
            AttributionRow(read_time=r["read_time"], wh=r["wh"], line_name=r["line_name"])
            for r in rows
        ],
    )


@app.exception_handler(dbmod.DbError)
def _db_error_handler(_request, exc: dbmod.DbError) -> JSONResponse:
    return JSONResponse(status_code=422, detail={"message": str(exc)})


@app.exception_handler(LineageError)
def _lineage_error_handler(_request, exc: LineageError) -> JSONResponse:
    # 更具体的子类已在路由内转换；兜底映射为 422
    return JSONResponse(status_code=422, detail={"message": str(exc)})
