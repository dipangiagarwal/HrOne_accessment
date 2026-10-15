"""
Tests for Analytics Endpoints:
- GET /analytics/employees/{emp_code}/monthly
- GET /analytics/departments/summary
- GET /analytics/leaderboard/late
- GET /analytics/departments/{department}/trend
"""

import pytest


def test_employee_monthly_summary(client_app):
    client_app.post("/employees", json={
        "emp_code": "EMP0501",
        "name": "Monthly Emp",
        "email": "monthly@example.com",
        "department": "Engineering",
        "joined_on": "2026-07-13"  # 15 working days in July 2026
    })

    # Monday 2026-07-13: PRESENT, full day
    client_app.post("/attendance/punch-in", json={"emp_code": "EMP0501", "punched_at": 1783915200000})  # 09:30 IST
    client_app.post("/attendance/punch-out", json={"emp_code": "EMP0501", "punched_at": 1783947600000}) # 18:30 IST

    # Tuesday 2026-07-14: WFH, half day (3 hours)
    client_app.post("/attendance/punch-in", json={"emp_code": "EMP0501", "punched_at": 1784001600000, "status": "WFH"})
    client_app.post("/attendance/punch-out", json={"emp_code": "EMP0501", "punched_at": 1784012400000})

    resp = client_app.get("/analytics/employees/EMP0501/monthly?month=2026-07")
    assert resp.status_code == 200
    data = resp.json()
    assert data["emp_code"] == "EMP0501"
    assert data["month"] == "2026-07"
    assert data["working_days"] == 15
    assert data["present_days"] == 1.5  # 1.0 (full day) + 0.5 (half day)
    # attendance_pct = 1.5 / 15 * 100 = 10.0
    assert data["attendance_pct"] == 10.0


def test_department_summary_includes_zero_log_employees(client_app):
    # EMP0502 has logs
    client_app.post("/employees", json={
        "emp_code": "EMP0502",
        "name": "Emp With Logs",
        "email": "withlogs@example.com",
        "department": "Design",
        "joined_on": "2026-01-01"
    })
    # EMP0503 has ZERO logs
    client_app.post("/employees", json={
        "emp_code": "EMP0503",
        "name": "Emp Zero Logs",
        "email": "zerologs@example.com",
        "department": "Design",
        "joined_on": "2026-01-01"
    })
    # EMP0504 joined in August (should NOT be counted in July headcount)
    client_app.post("/employees", json={
        "emp_code": "EMP0504",
        "name": "Emp Future Joiner",
        "email": "future@example.com",
        "department": "Design",
        "joined_on": "2026-08-01"
    })

    # Log 1 day for EMP0502
    client_app.post("/attendance/punch-in", json={"emp_code": "EMP0502", "punched_at": 1783310400000})
    client_app.post("/attendance/punch-out", json={"emp_code": "EMP0502", "punched_at": 1783342800000})

    resp = client_app.get("/analytics/departments/summary?month=2026-07&department=Design")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    design = items[0]
    assert design["department"] == "Design"
    # Headcount MUST be 2 (EMP0502 and EMP0503), excluding future joiner EMP0504
    assert design["headcount"] == 2
    assert design["present_days"] == 1.0


def test_late_leaderboard_competition_ranking_and_ties(client_app):
    # Setup 4 employees in Sales
    client_app.post("/employees", json={"emp_code": "EMP0510", "name": "Alice", "email": "alice@example.com", "department": "Sales", "joined_on": "2026-01-01"})
    client_app.post("/employees", json={"emp_code": "EMP0511", "name": "Bob", "email": "bob@example.com", "department": "Sales", "joined_on": "2026-01-01"})
    client_app.post("/employees", json={"emp_code": "EMP0512", "name": "Charlie", "email": "charlie@example.com", "department": "Sales", "joined_on": "2026-01-01"})
    client_app.post("/employees", json={"emp_code": "EMP0513", "name": "David", "email": "david@example.com", "department": "Sales", "joined_on": "2026-01-01"})

    # Punch in with late minutes:
    # Alice: 50 min late (10:20 IST)
    # Bob: 30 min late (10:00 IST)
    # Charlie: 30 min late (10:00 IST) -> Tied with Bob!
    # David: 15 min late (09:45 IST)
    # Base 09:30 IST on 2026-07-06 = 1783310400000 ms
    base_ms = 1783310400000
    client_app.post("/attendance/punch-in", json={"emp_code": "EMP0510", "punched_at": base_ms + 50 * 60 * 1000})
    client_app.post("/attendance/punch-in", json={"emp_code": "EMP0511", "punched_at": base_ms + 30 * 60 * 1000})
    client_app.post("/attendance/punch-in", json={"emp_code": "EMP0512", "punched_at": base_ms + 30 * 60 * 1000})
    client_app.post("/attendance/punch-in", json={"emp_code": "EMP0513", "punched_at": base_ms + 15 * 60 * 1000})

    # Standard competition ranking:
    # Rank 1: Alice (50 min)
    # Rank 2: Bob (30 min)
    # Rank 2: Charlie (30 min)
    # Rank 4: David (15 min)  -> Next rank is skipped (1, 2, 2, 4)!

    # If limit=2:
    # limit applies AFTER ranking: all employees with rank <= 2 are returned!
    # So Alice (rank 1), Bob (rank 2), and Charlie (rank 2) MUST ALL BE RETURNED!
    # Total rows returned = 3 (exceeding limit=2 due to tie at cutoff)
    resp = client_app.get("/analytics/leaderboard/late?month=2026-07&limit=2&department=Sales")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 3
    assert items[0]["emp_code"] == "EMP0510"
    assert items[0]["rank"] == 1
    assert items[0]["total_late_minutes"] == 50

    # Tied rows are ordered by emp_code ascending: EMP0511 before EMP0512
    assert items[1]["emp_code"] == "EMP0511"
    assert items[1]["rank"] == 2
    assert items[1]["total_late_minutes"] == 30

    assert items[2]["emp_code"] == "EMP0512"
    assert items[2]["rank"] == 2
    assert items[2]["total_late_minutes"] == 30


def test_department_trend_gap_filling_and_moving_avg(client_app):
    client_app.post("/employees", json={
        "emp_code": "EMP0520",
        "name": "Trend Emp",
        "email": "trend@example.com",
        "department": "Analytics",
        "joined_on": "2026-07-01"
    })

    # Record attendance on only 1 day: 2026-07-06 (Monday)
    client_app.post("/attendance/punch-in", json={"emp_code": "EMP0520", "punched_at": 1783310400000})
    client_app.post("/attendance/punch-out", json={"emp_code": "EMP0520", "punched_at": 1783342800000})

    # Request range 2026-07-01 to 2026-07-10 (10 days)
    resp = client_app.get("/analytics/departments/Analytics/trend?from=2026-07-01&to=2026-07-10")
    assert resp.status_code == 200
    data = resp.json()
    items = data["items"]
    # All 10 calendar days must be present in the output
    assert len(items) == 10
    dates = [item["date"] for item in items]
    assert dates[0] == "2026-07-01"
    assert dates[-1] == "2026-07-10"

    # Working day with no records has rate 0.0
    day1 = items[0]  # Wednesday
    assert day1["is_working_day"] is True
    assert day1["headcount"] == 1
    assert day1["present_count"] == 0.0
    assert day1["attendance_rate"] == 0.0

    # Weekends (2026-07-04 and 2026-07-05) have attendance_rate = null
    sat = items[3]
    assert sat["date"] == "2026-07-04"
    assert sat["is_working_day"] is False
    assert sat["attendance_rate"] is None

    # Error cases:
    # 1. to < from -> 422
    assert client_app.get("/analytics/departments/Analytics/trend?from=2026-07-10&to=2026-07-01").status_code == 422
    # 2. range > 92 days -> 422
    assert client_app.get("/analytics/departments/Analytics/trend?from=2026-01-01&to=2026-05-01").status_code == 422
    # 3. unknown department -> 404
    assert client_app.get("/analytics/departments/NonExistentDept/trend?from=2026-07-01&to=2026-07-10").status_code == 404
