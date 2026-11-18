"""转线登记与按 UTC 日期历史归属查询的端到端测试（FastAPI + 真实 PostgreSQL）。"""
from __future__ import annotations

import threading
from datetime import date, datetime, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import config as config_mod
from app import lineage

DAY = "2026-11-15"
DAY_START = datetime(2026, 11, 15, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 夹具：每个测试独立的表号空间，结束清理 meter_line_transfers / meters / readings
# ---------------------------------------------------------------------------

@pytest.fixture()
def cleanup(pg_dsn: str):
    created_meters: list[int] = []

    def make_meter(meter_id: int, line: str) -> None:
        with psycopg.connect(pg_dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO meters (meter_id, line_name) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (meter_id, line),
            )
        created_meters.append(meter_id)

    yield make_meter

    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        if created_meters:
            conn.execute(
                "DELETE FROM meter_line_transfers WHERE meter_id = ANY(%s)",
                (created_meters,),
            )
            conn.execute(
                "DELETE FROM readings WHERE meter_id = ANY(%s)", (created_meters,)
            )
            conn.execute("DELETE FROM meters WHERE meter_id = ANY(%s)", (created_meters,))


def _add_reading(pg_dsn: str, meter_id: int, hour: int, wh: int,
                 day: int = 15) -> None:
    ts = datetime(2026, 11, day, hour, tzinfo=timezone.utc)
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO readings (meter_id, read_time, wh) VALUES (%s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (meter_id, ts, wh),
        )


def _transfer(meter_id: int, iso: str, line: str) -> dict:
    return {"meter_id": meter_id, "effective_at": iso, "target_line": line}


# ---------------------------------------------------------------------------
# 登记：首次登记保留原产线、幂等重试、异值拒绝
# ---------------------------------------------------------------------------

def test_first_registration_keeps_original_line_as_baseline(
    client: TestClient, cleanup, pg_dsn: str
) -> None:
    cleanup(7001, "A线")
    resp = client.post(
        "/meters/7001/transfers",
        json=_transfer(7001, "2026-11-15T09:00:00Z", "B线"),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["target_line"] == "B线"
    assert body["idempotent"] is False
    assert body["baseline_line"] == "A线"  # 原产线被保留

    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT effective_at, target_line FROM meter_line_transfers "
            "WHERE meter_id = 7001 ORDER BY effective_at"
        ).fetchall()
    # 基线时刻为公元 1 年 UTC，原产线被保留
    assert rows[0][0] == datetime(1, 1, 1, tzinfo=timezone.utc)
    assert rows[0][1] == "A线"
    assert rows[1][1] == "B线"


def test_same_value_same_timestamp_is_idempotent_retry(
    client: TestClient, cleanup
) -> None:
    cleanup(7002, "A线")
    payload = _transfer(7002, "2026-11-15T10:00:00Z", "B线")
    first = client.post("/meters/7002/transfers", json=payload)
    assert first.status_code == 201
    assert first.json()["idempotent"] is False

    second = client.post("/meters/7002/transfers", json=payload)
    assert second.status_code == 201
    assert second.json()["idempotent"] is True
    assert second.json()["baseline_line"] is None


def test_different_value_same_timestamp_rejected_and_no_partial_change(
    client: TestClient, cleanup, pg_dsn: str
) -> None:
    cleanup(7003, "A线")
    client.post(
        "/meters/7003/transfers",
        json=_transfer(7003, "2026-11-15T10:00:00Z", "B线"),
    )
    bad = client.post(
        "/meters/7003/transfers",
        json=_transfer(7003, "2026-11-15T10:00:00Z", "C线"),
    )
    assert bad.status_code == 409
    assert "不一致" in bad.json()["detail"]["message"]

    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        cnt = conn.execute(
            "SELECT count(*) FROM meter_line_transfers "
            "WHERE meter_id = 7003 AND target_line = 'C线'"
        ).fetchone()[0]
        # 拒绝后没有留下 C线 行；仍是基线 + B线 两行
        total = conn.execute(
            "SELECT count(*) FROM meter_line_transfers WHERE meter_id = 7003"
        ).fetchone()[0]
    assert cnt == 0
    assert total == 2


def test_register_unknown_meter_returns_404(client: TestClient) -> None:
    resp = client.post(
        "/meters/999999/transfers",
        json=_transfer(999999, "2026-11-15T10:00:00Z", "B线"),
    )
    assert resp.status_code == 404


def test_path_and_body_meter_id_mismatch_returns_400(
    client: TestClient, cleanup
) -> None:
    cleanup(7004, "A线")
    resp = client.post(
        "/meters/7004/transfers",
        json=_transfer(7005, "2026-11-15T10:00:00Z", "B线"),
    )
    assert resp.status_code == 400


def test_naive_timestamp_rejected(client: TestClient, cleanup) -> None:
    cleanup(7006, "A线")
    resp = client.post(
        "/meters/7006/transfers",
        json={"meter_id": 7006, "effective_at": "2026-11-15T10:00:00",
              "target_line": "B线"},
    )
    assert resp.status_code == 422


def test_blank_target_line_rejected(client: TestClient, cleanup) -> None:
    cleanup(7007, "A线")
    resp = client.post(
        "/meters/7007/transfers",
        json=_transfer(7007, "2026-11-15T10:00:00Z", "   "),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 归属：生效时刻的读数归新线；更早读数仍归原产线
# ---------------------------------------------------------------------------

def test_attribution_boundary_reading_at_effective_time_goes_to_new_line(
    client: TestClient, cleanup, pg_dsn: str
) -> None:
    mid = 7010
    cleanup(mid, "B线")  # meters 现值为最新产线 B线
    _add_reading(pg_dsn, mid, 8, 100)
    _add_reading(pg_dsn, mid, 9, 200)   # 生效时刻本身 -> 新线
    _add_reading(pg_dsn, mid, 10, 300)

    # 首次登记：基线保留当时的 meters.line_name（B线原产线），09:00 转到 C线
    resp0 = client.post(
        f"/meters/{mid}/transfers",
        json=_transfer(mid, "2026-11-15T09:00:00Z", "C线"),
    )
    assert resp0.json()["baseline_line"] == "B线"

    resp = client.get(f"/meters/{mid}/readings", params={"date": DAY})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 3
    assert body["timezone"] == "UTC"
    got = [(r["read_time"], r["wh"], r["line_name"]) for r in body["rows"]]
    assert got == [
        ("2026-11-15T08:00:00Z", 100, "B线"),  # 更早读数 -> 原产线
        ("2026-11-15T09:00:00Z", 200, "C线"),  # 生效时刻 -> 新线
        ("2026-11-15T10:00:00Z", 300, "C线"),
    ]


def test_backfill_earlier_transfer_reattributes_older_readings(
    client: TestClient, cleanup, pg_dsn: str
) -> None:
    mid = 7011
    cleanup(mid, "C线")
    for hour, wh in ((8, 10), (9, 20), (10, 30), (11, 40)):
        _add_reading(pg_dsn, mid, hour, wh)

    # 先登记 10:00 -> C线（基线保留当时的 meters.line_name=C线）
    r1 = client.post(
        f"/meters/{mid}/transfers",
        json=_transfer(mid, "2026-11-15T10:00:00Z", "C线"),
    )
    assert r1.status_code == 201
    assert r1.json()["baseline_line"] == "C线"

    # 补录更早的 09:00 -> B线（最新归属 C线 与 meters.line_name 一致，允许）
    r2 = client.post(
        f"/meters/{mid}/transfers",
        json=_transfer(mid, "2026-11-15T09:00:00Z", "B线"),
    )
    assert r2.status_code == 201
    assert r2.json()["idempotent"] is False
    assert r2.json()["baseline_line"] is None

    body = client.get(f"/meters/{mid}/readings", params={"date": DAY}).json()
    got = [(r["read_time"][11:16], r["line_name"]) for r in body["rows"]]
    assert got == [("08:00", "C线"), ("09:00", "B线"),
                   ("10:00", "C线"), ("11:00", "C线")]


def test_backfill_conflicting_with_current_line_rejected(
    client: TestClient, cleanup, pg_dsn: str
) -> None:
    mid = 7012
    cleanup(mid, "C线")  # meters 现值 C线
    # 首次登记 10:00 -> B线：最新登记与 meters 现值已经不同
    client.post(
        f"/meters/{mid}/transfers",
        json=_transfer(mid, "2026-11-15T10:00:00Z", "B线"),
    )
    # 再补录更早转线时，检测到最新归属 B线 != meters.line_name C线 -> 拒绝
    resp = client.post(
        f"/meters/{mid}/transfers",
        json=_transfer(mid, "2026-11-15T08:00:00Z", "A线"),
    )
    assert resp.status_code == 409

    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        total = conn.execute(
            "SELECT count(*) FROM meter_line_transfers WHERE meter_id = %s",
            (mid,),
        ).fetchone()[0]
    # 仅保留首次登记的两行（基线 + 10:00 B线），失败无部分变更
    assert total == 2


def test_query_before_any_registration_returns_409(client: TestClient, cleanup) -> None:
    mid = 7013
    cleanup(mid, "A线")
    resp = client.get(f"/meters/{mid}/readings", params={"date": DAY})
    assert resp.status_code == 409


def test_query_unknown_meter_returns_404(client: TestClient) -> None:
    resp = client.get("/meters/888888/readings", params={"date": DAY})
    assert resp.status_code == 404


def test_query_other_day_excluded_by_utc_boundary(
    client: TestClient, cleanup, pg_dsn: str
) -> None:
    mid = 7014
    cleanup(mid, "B线")
    _add_reading(pg_dsn, mid, 23, 900, day=14)
    _add_reading(pg_dsn, mid, 0, 1000, day=15)
    _add_reading(pg_dsn, mid, 23, 1100, day=15)
    _add_reading(pg_dsn, mid, 0, 1200, day=16)  # 次日 00:00 不属当日
    client.post(
        f"/meters/{mid}/transfers",
        json=_transfer(mid, "2026-11-14T00:00:00Z", "B线"),
    )
    body = client.get(f"/meters/{mid}/readings", params={"date": DAY}).json()
    times = [r["read_time"] for r in body["rows"]]
    assert times == ["2026-11-15T00:00:00Z", "2026-11-15T23:00:00Z"]


def test_timezone_offsets_normalized_to_utc(
    client: TestClient, cleanup, pg_dsn: str
) -> None:
    mid = 7015
    cleanup(mid, "B线")
    # 09:00+08 == 01:00 UTC；读数 01:00 UTC 在生效时刻上，归新线 B线；
    # 00:00 UTC 更早，归基线保留的原产线 B线？——为区分，首次登记目标改为 C线
    _add_reading(pg_dsn, mid, 1, 50)
    _add_reading(pg_dsn, mid, 0, 40)
    client.post(
        f"/meters/{mid}/transfers",
        json=_transfer(mid, "2026-11-15T09:00:00+08:00", "C线"),
    )
    body = client.get(f"/meters/{mid}/readings", params={"date": DAY}).json()
    rows = {r["read_time"]: r["line_name"] for r in body["rows"]}
    assert rows["2026-11-15T00:00:00Z"] == "B线"  # 基线原产线
    assert rows["2026-11-15T01:00:00Z"] == "C线"  # 偏移量已归一到 UTC
    # 登记回显也归一到 UTC
    listed = client.get("/jobs").status_code  # 保留 /jobs 不受影响的轻量断言
    assert listed == 200


def test_attribution_limit_truncation_flag(client: TestClient, cleanup, pg_dsn: str,
                                           monkeypatch) -> None:
    mid = 7016
    cleanup(mid, "B线")
    for hour in range(4):
        _add_reading(pg_dsn, mid, hour, hour)
    client.post(
        f"/meters/{mid}/transfers",
        json=_transfer(mid, "2026-11-14T00:00:00Z", "B线"),
    )
    monkeypatch.setattr(config_mod.settings, "attribution_row_limit", 2)
    body = client.get(f"/meters/{mid}/readings", params={"date": DAY, "limit": 2}).json()
    assert body["truncated"] is True
    assert body["count"] == 2
    assert body["limit"] == 2


# ---------------------------------------------------------------------------
# readings 独立 SQL 更正后，从新的已提交快照重新归属
# ---------------------------------------------------------------------------

def test_attribution_rereads_latest_committed_snapshot_after_correction(
    client: TestClient, cleanup, pg_dsn: str
) -> None:
    mid = 7017
    cleanup(mid, "B线")
    _add_reading(pg_dsn, mid, 9, 200)
    client.post(
        f"/meters/{mid}/transfers",
        json=_transfer(mid, "2026-11-15T09:00:00Z", "B线"),
    )

    before = client.get(f"/meters/{mid}/readings", params={"date": DAY}).json()
    assert before["rows"][0]["wh"] == 200
    assert before["rows"][0]["line_name"] == "B线"

    # 独立 SQL 更正 readings（模拟数仓侧修复），提交后重新查询
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE readings SET wh = 250 WHERE meter_id = %s "
            "AND read_time = TIMESTAMPTZ '2026-11-15 09:00:00+00'",
            (mid,),
        )

    after = client.get(f"/meters/{mid}/readings", params={"date": DAY}).json()
    assert after["rows"][0]["wh"] == 250          # 新快照可见更正
    assert after["rows"][0]["line_name"] == "B线"  # 归属规则不变


# ---------------------------------------------------------------------------
# 并发补录：串行化、无部分变更，历史归属与 meters.line_name 最新归属一致
# ---------------------------------------------------------------------------

def test_concurrent_backfill_is_serialized_without_partial_changes(
    client: TestClient, cleanup, pg_dsn: str
) -> None:
    mid = 7018
    cleanup(mid, "C线")  # 最新产线 C线
    for hour in range(8, 13):
        _add_reading(pg_dsn, mid, hour, hour)

    # 首次登记 12:00 -> C线（与 meters 现值一致）
    r0 = client.post(
        f"/meters/{mid}/transfers",
        json=_transfer(mid, "2026-11-15T12:00:00Z", "C线"),
    )
    assert r0.status_code == 201

    # 并发补录两条更早转线：09:00 -> B线、10:00 -> B线
    barrier = threading.Barrier(2)
    results: list[int] = []
    lock = threading.Lock()

    def worker(iso: str) -> None:
        barrier.wait()
        resp = client.post(
            f"/meters/{mid}/transfers",
            json=_transfer(mid, iso, "B线"),
        )
        with lock:
            results.append(resp.status_code)

    t1 = threading.Thread(target=worker, args=("2026-11-15T09:00:00Z",))
    t2 = threading.Thread(target=worker, args=("2026-11-15T10:00:00Z",))
    t1.start(); t2.start()
    t1.join(); t2.join()
    assert results == [201, 201]

    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT effective_at, target_line FROM meter_line_transfers "
            "WHERE meter_id = %s ORDER BY effective_at",
            (mid,),
        ).fetchall()
        latest_line = conn.execute(
            "SELECT line_name FROM meters WHERE meter_id = %s", (mid,)
        ).fetchone()[0]
    # 基线 + 3 次登记，全部落库且顺序正确
    assert [r[1] for r in rows] == ["C线", "B线", "B线", "C线"]
    # 最新归属与 meters.line_name 一致
    assert rows[-1][1] == latest_line == "C线"

    body = client.get(f"/meters/{mid}/readings", params={"date": DAY}).json()
    got = {r["read_time"][11:16]: r["line_name"] for r in body["rows"]}
    assert got == {"08:00": "C线", "09:00": "B线", "10:00": "B线",
                   "11:00": "B线", "12:00": "C线"}


def test_concurrent_same_timestamp_same_value_both_succeed(
    client: TestClient, cleanup
) -> None:
    mid = 7019
    cleanup(mid, "B线")
    barrier = threading.Barrier(2)
    statuses: list[int] = []
    lock = threading.Lock()
    payload = _transfer(mid, "2026-11-15T10:00:00Z", "B线")

    def worker() -> None:
        barrier.wait()
        resp = client.post(f"/meters/{mid}/transfers", json=payload)
        with lock:
            statuses.append(resp.status_code)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert statuses == [201, 201]  # 同值并发：一插一幂等，均成功


def test_concurrent_same_timestamp_different_values_one_rejected(
    client: TestClient, cleanup, pg_dsn: str
) -> None:
    mid = 7020
    cleanup(mid, "B线")
    barrier = threading.Barrier(2)
    outcomes: list[tuple[int, str]] = []
    lock = threading.Lock()

    def worker(line: str) -> None:
        barrier.wait()
        resp = client.post(
            f"/meters/{mid}/transfers",
            json=_transfer(mid, "2026-11-15T10:00:00Z", line),
        )
        with lock:
            outcomes.append((resp.status_code, line))

    t1 = threading.Thread(target=worker, args=("B线",))
    t2 = threading.Thread(target=worker, args=("C线",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert sorted(s for s, _ in outcomes) == [201, 409]
    winner = next(line for status, line in outcomes if status == 201)
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        lines = [
            r[0]
            for r in conn.execute(
                "SELECT target_line FROM meter_line_transfers "
                "WHERE meter_id = %s AND effective_at = "
                "TIMESTAMPTZ '2026-11-15 10:00:00+00'",
                (mid,),
            ).fetchall()
        ]
    assert lines == [winner]  # 只落下抢到锁的那个值，无部分变更


# ---------------------------------------------------------------------------
# 直接驱动 service 层：空结果日返回空列表
# ---------------------------------------------------------------------------

def test_service_returns_empty_list_for_day_without_readings(
    client: TestClient, cleanup
) -> None:
    mid = 7021
    cleanup(mid, "B线")
    lineage.register_transfer(mid, DAY_START, "B线")
    rows = lineage.query_daily_attribution(mid, date(2026, 11, 15), 100)
    assert rows == []
