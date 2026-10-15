"""
Tests for Timestamp Validation:
- Valid epoch milliseconds
- Rejecting seconds (e.g. 10 digits)
- Rejecting floats
- Rejecting strings
- Rejecting booleans (bool subclass of int)
- Rejecting out of range
- Sub-second truncation
"""

import pytest


def test_timestamp_validation_matrix(client_app):
    # Setup test employee
    client_app.post("/employees", json={
        "emp_code": "EMP0201",
        "name": "Timestamp Tester",
        "email": "timestamp@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })

    # 1. Valid epoch milliseconds (e.g., 2026-07-06 09:30:00 IST = 1783310400000)
    valid_ms = 1783310400000
    resp = client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0201",
        "punched_at": valid_ms,
        "status": "PRESENT"
    })
    assert resp.status_code == 201
    assert resp.json()["punch_in"] == valid_ms

    # Setup another employee for failure tests
    client_app.post("/employees", json={
        "emp_code": "EMP0202",
        "name": "Timestamp Tester 2",
        "email": "timestamp2@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })

    # 2. Reject seconds mistakenly supplied (e.g. 1783310400 < 100000000000)
    resp_sec = client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0202",
        "punched_at": 1783310400
    })
    assert resp_sec.status_code == 422

    # 3. Reject float timestamp
    resp_float = client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0202",
        "punched_at": 1783310400000.5
    })
    assert resp_float.status_code == 422

    # 4. Reject string timestamp
    resp_str = client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0202",
        "punched_at": "1783310400000"
    })
    assert resp_str.status_code == 422

    # 5. Reject boolean timestamp (bool is subclass of int in Python)
    resp_bool_true = client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0202",
        "punched_at": True
    })
    assert resp_bool_true.status_code == 422

    resp_bool_false = client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0202",
        "punched_at": False
    })
    assert resp_bool_false.status_code == 422

    # 6. Reject out of range (< 100000000000 or > 4102444800000)
    resp_out_low = client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0202",
        "punched_at": 99_999_999_999
    })
    assert resp_out_low.status_code == 422

    resp_out_high = client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0202",
        "punched_at": 4_102_444_800_001
    })
    assert resp_out_high.status_code == 422


def test_timestamp_subsecond_truncation(client_app):
    client_app.post("/employees", json={
        "emp_code": "EMP0203",
        "name": "Truncation Tester",
        "email": "trunc@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })

    # Sub-second milliseconds: 1783310400999 ms -> truncated to whole second 1783310400000 ms
    resp = client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0203",
        "punched_at": 1783310400999,
        "status": "PRESENT"
    })
    assert resp.status_code == 201
    assert resp.json()["punch_in"] == 1783310400000
