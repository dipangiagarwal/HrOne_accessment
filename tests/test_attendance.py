"""
Tests for Attendance endpoints:
- POST /attendance/punch-in
- POST /attendance/punch-out
- GET /attendance
- Concurrent punch-in and punch-out race conditions
"""

import concurrent.futures
import pytest


def test_punch_in_and_punch_out_flow(client_app):
    client_app.post("/employees", json={
        "emp_code": "EMP0301",
        "name": "Flow Tester",
        "email": "flow@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Punch-in at 2026-07-06 09:28:00 IST (1783310280000 ms)
    punch_in_ms = 1783310280000
    resp_in = client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0301",
        "punched_at": punch_in_ms,
        "status": "PRESENT"
    })
    assert resp_in.status_code == 201
    in_data = resp_in.json()
    assert in_data["date"] == "2026-07-06"
    assert in_data["punch_in"] == punch_in_ms
    assert in_data["punch_out"] is None
    assert in_data["work_hours"] is None
    assert in_data["late_minutes"] == 0
    assert in_data["overtime_minutes"] == 0
    assert in_data["half_day"] is False
    assert in_data["history"] == []
    assert "_id" not in in_data
    assert "id" not in in_data

    # Punch-out at 2026-07-06 18:35:00 IST (1783343100000 ms)
    # Elapsed seconds = 32820 -> 9.116666... hours -> rounded half-up: 9.12 hours
    punch_out_ms = 1783343100000
    resp_out = client_app.post("/attendance/punch-out", json={
        "emp_code": "EMP0301",
        "punched_at": punch_out_ms
    })
    assert resp_out.status_code == 200
    out_data = resp_out.json()
    assert out_data["punch_out"] == punch_out_ms
    assert out_data["work_hours"] == 9.12
    assert out_data["overtime_minutes"] == 0  # 5 min is < 30 min threshold
    assert out_data["half_day"] is False

    # Second punch-out on already-closed record returns 409
    resp_out_again = client_app.post("/attendance/punch-out", json={
        "emp_code": "EMP0301",
        "punched_at": punch_out_ms + 1000
    })
    assert resp_out_again.status_code == 409


def test_punch_in_unknown_employee(client_app):
    resp = client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP9999",
        "status": "PRESENT"
    })
    assert resp.status_code == 404


def test_punch_in_duplicate_same_day(client_app):
    client_app.post("/employees", json={
        "emp_code": "EMP0302",
        "name": "Dup Tester",
        "email": "dup@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    ms = 1783310400000  # 2026-07-06
    resp1 = client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0302",
        "punched_at": ms
    })
    assert resp1.status_code == 201

    # Second punch-in for same employee and date -> 409
    resp2 = client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0302",
        "punched_at": ms + 3600000
    })
    assert resp2.status_code == 409


def test_concurrent_punch_in(client_app):
    client_app.post("/employees", json={
        "emp_code": "EMP0303",
        "name": "Race Puncher",
        "email": "racepunch@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    ms = 1783310400000

    def send_punch_in():
        return client_app.post("/attendance/punch-in", json={
            "emp_code": "EMP0303",
            "punched_at": ms
        }).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(send_punch_in) for _ in range(5)]
        statuses = [f.result() for f in futures]

    assert statuses.count(201) == 1
    assert statuses.count(409) == 4


def test_concurrent_punch_out(client_app):
    client_app.post("/employees", json={
        "emp_code": "EMP0304",
        "name": "Race Punch Out",
        "email": "racepunchout@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    in_ms = 1783310400000
    client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0304",
        "punched_at": in_ms
    })
    out_ms = in_ms + 8 * 3600 * 1000

    def send_punch_out():
        return client_app.post("/attendance/punch-out", json={
            "emp_code": "EMP0304",
            "punched_at": out_ms
        }).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(send_punch_out) for _ in range(5)]
        statuses = [f.result() for f in futures]

    assert statuses.count(200) == 1
    assert statuses.count(409) == 4


def test_punch_out_validation_constraints(client_app):
    client_app.post("/employees", json={
        "emp_code": "EMP0305",
        "name": "Constraint Tester",
        "email": "constraint@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    in_ms = 1783310400000
    client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0305",
        "punched_at": in_ms
    })

    # punch_out <= punch_in -> 422
    resp_before = client_app.post("/attendance/punch-out", json={
        "emp_code": "EMP0305",
        "punched_at": in_ms - 1000
    })
    assert resp_before.status_code == 422

    # punch_out > 24 hours after punch_in -> 422
    resp_too_late = client_app.post("/attendance/punch-out", json={
        "emp_code": "EMP0305",
        "punched_at": in_ms + (24 * 3600 + 1) * 1000
    })
    assert resp_too_late.status_code == 422


def test_list_attendance_filters_and_sorting(client_app):
    client_app.post("/employees", json={
        "emp_code": "EMP0306",
        "name": "List Tester 1",
        "email": "lt1@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    client_app.post("/employees", json={
        "emp_code": "EMP0307",
        "name": "List Tester 2",
        "email": "lt2@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })

    # Record 1: EMP0306 on 2026-07-06
    client_app.post("/attendance/punch-in", json={"emp_code": "EMP0306", "punched_at": 1783310400000})
    # Record 2: EMP0307 on 2026-07-06
    client_app.post("/attendance/punch-in", json={"emp_code": "EMP0307", "punched_at": 1783310400000})
    # Record 3: EMP0306 on 2026-07-07
    client_app.post("/attendance/punch-in", json={"emp_code": "EMP0306", "punched_at": 1783396800000})

    # List all: sorted by date descending, then emp_code ascending
    resp = client_app.get("/attendance")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 3
    # 2026-07-07 comes first
    assert items[0]["date"] == "2026-07-07"
    assert items[0]["emp_code"] == "EMP0306"
    # On 2026-07-06, EMP0306 comes before EMP0307 (emp_code ascending)
    assert items[1]["date"] == "2026-07-06"
    assert items[1]["emp_code"] == "EMP0306"
    assert items[2]["date"] == "2026-07-06"
    assert items[2]["emp_code"] == "EMP0307"

    # Filter date_from > date_to -> 422
    resp_bad_range = client_app.get("/attendance?date_from=2026-07-10&date_to=2026-07-01")
    assert resp_bad_range.status_code == 422
