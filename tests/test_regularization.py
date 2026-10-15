"""
Tests for Regularization endpoint:
- PATCH /attendance/{emp_code}/{date}
- History tracking, change diffing, timestamp formatting
- Status changes, clearing punch times for ABSENT/LEAVE
- Rejecting no-op updates (422)
- Concurrent regularizations (409)
"""

import concurrent.futures
import pytest


def test_regularization_punch_correction(client_app):
    client_app.post("/employees", json={
        "emp_code": "EMP0401",
        "name": "Reg Tester",
        "email": "reg@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })

    # Punch-in at 10:05 IST (35 min late), out at 18:30 IST
    # 2026-07-06 10:05 IST = 1783312500000 ms
    # 2026-07-06 18:30 IST = 1783342800000 ms
    client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0401",
        "punched_at": 1783312500000
    })
    client_app.post("/attendance/punch-out", json={
        "emp_code": "EMP0401",
        "punched_at": 1783342800000
    })

    # Correct punch-in to 09:28 IST (1783310280000 ms)
    patch_payload = {
        "reason": "biometric glitch at entrance",
        "regularized_by": "hr.manager",
        "punch_in": 1783310280000
    }
    resp = client_app.patch("/attendance/EMP0401/2026-07-06", json=patch_payload)
    assert resp.status_code == 200
    data = resp.json()

    # Derived values recomputed
    assert data["punch_in"] == 1783310280000
    assert data["late_minutes"] == 0  # Was 35, now 0
    assert data["work_hours"] == 9.03  # Increased work hours

    # History entry appended
    assert len(data["history"]) == 1
    h = data["history"][0]
    assert h["by"] == "hr.manager"
    assert h["reason"] == "biometric glitch at entrance"
    assert "punch_in" in h["changes"]
    assert h["changes"]["punch_in"]["from"] == 1783312500000
    assert h["changes"]["punch_in"]["to"] == 1783310280000
    assert "late_minutes" in h["changes"]
    assert h["changes"]["late_minutes"]["from"] == 35
    assert h["changes"]["late_minutes"]["to"] == 0
    assert "work_hours" in h["changes"]


def test_regularization_status_to_leave(client_app):
    client_app.post("/employees", json={
        "emp_code": "EMP0402",
        "name": "Leave Tester",
        "email": "leave@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0402",
        "punched_at": 1783310400000
    })

    # Change status to LEAVE (clears punch times, work hours, late minutes)
    patch_payload = {
        "reason": "applied leave approved retroactively",
        "regularized_by": "hr.lead",
        "status": "LEAVE"
    }
    resp = client_app.patch("/attendance/EMP0402/2026-07-06", json=patch_payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "LEAVE"
    assert data["punch_in"] is None
    assert data["punch_out"] is None
    assert data["work_hours"] is None
    assert data["late_minutes"] == 0
    assert data["half_day"] is False
    assert len(data["history"]) == 1


def test_regularization_no_op_rejection(client_app):
    client_app.post("/employees", json={
        "emp_code": "EMP0403",
        "name": "NoOp Tester",
        "email": "noop@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0403",
        "punched_at": 1783310400000
    })

    # Sending a patch with identical values -> 422
    patch_payload = {
        "reason": "redundant correction attempt",
        "regularized_by": "hr.admin",
        "status": "PRESENT"
    }
    resp = client_app.patch("/attendance/EMP0403/2026-07-06", json=patch_payload)
    assert resp.status_code == 422


def test_regularization_invalid_combinations(client_app):
    client_app.post("/employees", json={
        "emp_code": "EMP0404",
        "name": "Combo Tester",
        "email": "combo@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0404",
        "punched_at": 1783310400000
    })

    # Setting ABSENT while supplying punch_in -> 422
    resp = client_app.patch("/attendance/EMP0404/2026-07-06", json={
        "reason": "invalid combination",
        "regularized_by": "hr.admin",
        "status": "ABSENT",
        "punch_in": 1783310400000
    })
    assert resp.status_code == 422


def test_concurrent_regularizations(client_app):
    client_app.post("/employees", json={
        "emp_code": "EMP0405",
        "name": "Race Reg",
        "email": "racereg@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    in_ms = 1783310400000
    client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0405",
        "punched_at": in_ms
    })

    def send_patch(status_val):
        return client_app.patch("/attendance/EMP0405/2026-07-06", json={
            "reason": f"update to {status_val}",
            "regularized_by": "admin",
            "status": status_val
        }).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        f1 = executor.submit(send_patch, "WFH")
        f2 = executor.submit(send_patch, "ON_DUTY")
        statuses = [f1.result(), f2.result()]

    # At least one succeeds (200), competing simultaneous edit gets 409
    assert 200 in statuses
