"""API 请求 / 响应模型。"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

JobStatus = Literal[
    "pending",      # 已受理，等待运行名额
    "running",      # 运行中
    "cancelling",   # 已收到取消请求，正在中止本轮 SQL
    "succeeded",    # 终态：核对完成（可能一致，也可能有差异）
    "failed",       # 终态：任一侧失败
    "cancelled",    # 终态：用户取消
    "timeout",      # 终态：超过单轮时限
]

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "timeout"}
ACTIVE_STATUSES = {"pending", "running", "cancelling"}


class CreateJobRequest(BaseModel):
    left_sql: str = Field(..., min_length=1, description="左侧只读 SELECT（如改写后的日结 SQL）")
    right_sql: str = Field(..., min_length=1, description="右侧只读 SELECT（基准 SQL）")
    name: str | None = Field(None, max_length=128, description="可选备注名")


class DiffRow(BaseModel):
    # 与结果集列顺序对齐；NULL 用 null 表示，空串为 ""，二者不同
    values: list[Any]
    left_count: int
    right_count: int


class ReconcileResult(BaseModel):
    identical: bool
    columns: list[str]
    column_types: list[str]          # bigint | text
    left_row_total: int
    right_row_total: int
    matched_group_count: int         # 行内容与次数都相同的去重行数
    diff_group_total: int            # 存在差异的去重行种类总数（不受 20 条展示上限影响）
    diffs_shown: int
    diffs: list[DiffRow]             # 最多 20 种


class JobOut(BaseModel):
    job_id: str
    name: str | None
    status: JobStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    deadline: datetime | None
    cancel_requested: bool
    error: str | None
    result: ReconcileResult | None

    @staticmethod
    def utcnow() -> datetime:
        return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 转线登记
# ---------------------------------------------------------------------------

class TransferRequest(BaseModel):
    meter_id: int = Field(..., description="表号，须已存在于 meters")
    effective_at: datetime = Field(
        ...,
        description="转线生效时刻（须带时区，建议 UTC）；该时刻的读数归新线",
    )
    target_line: str = Field(..., min_length=1, description="目标产线名称（非空、非纯空白）")

    @field_validator("effective_at")
    @classmethod
    def _require_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("effective_at 必须带时区偏移，例如 2026-11-15T00:00:00Z")
        return value

    @field_validator("target_line")
    @classmethod
    def _strip_line(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("target_line 不能为纯空白")
        return stripped


class TransferOut(BaseModel):
    meter_id: int
    effective_at: datetime                 # 统一输出 UTC
    target_line: str
    idempotent: bool = Field(
        ..., description="true 表示同时刻同值登记已存在，本次为重试（幂等成功）"
    )
    baseline_line: str | None = Field(
        None, description="首次登记时写入 -infinity 基线行所用的原产线；其余情况为 null"
    )


# ---------------------------------------------------------------------------
# 单表按 UTC 日期的历史归属查询
# ---------------------------------------------------------------------------

class AttributionRow(BaseModel):
    read_time: datetime                    # 读数时刻（UTC）
    wh: int                                # 累计电量 Wh
    line_name: str                         # 该读数采集时刻所属产线


class AttributionOut(BaseModel):
    meter_id: int
    date: date                             # 查询的 UTC 日期
    timezone: str = "UTC"
    count: int
    truncated: bool
    limit: int
    rows: list[AttributionRow]
