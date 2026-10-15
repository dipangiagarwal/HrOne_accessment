"""
Tests for Employee endpoints:
- POST /employees (success, duplicate conflict 409, invalid shift times 422, format validation)
- GET /employees (pagination, sorting by emp_code asc, department filtering, total count)
"""

import concurrent.futures
import pytest


def test_create_employee_success(client_app):
    payload = {
        "emp_code": "EMP0101",
        "name": "Ananya Sharma",
        "email": "ananya@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-15"
    }
    resp = client_app.post("/employees", json=payload)
    assert resp.status_code == 201
    data = resp.json()
    assert data["emp_code"] == "EMP0101"
    assert data["name"] == "Ananya Sharma"
    assert data["department"] == "Engineering"
    assert isinstance(data["created_at"], int)
    assert data["created_at"] >= 100_000_000_000
    assert "_id" not in data


def test_create_employee_duplicate_conflict(client_app):
    payload = {
        "emp_code": "EMP0102",
        "name": "Rohan Patel",
        "email": "rohan@example.com",
        "department": "Sales",
        "joined_on": "2026-02-01"
    }
    resp1 = client_app.post("/employees", json=payload)
    assert resp1.status_code == 201

    # Second request with identical emp_code must return 409
    resp2 = client_app.post("/employees", json=payload)
    assert resp2.status_code == 409
    assert "already exists" in resp2.json()["detail"]


def test_create_employee_invalid_shift_times(client_app):
    # shift_start and shift_end must be different
    payload = {
        "emp_code": "EMP0103",
        "name": "Same Shift Times",
        "email": "sameshifttimes@example.com",
        "department": "Operations",
        "shift_start": "09:30",
        "shift_end": "09:30",
        "joined_on": "2026-03-01"
    }
    resp = client_app.post("/employees", json=payload)
    assert resp.status_code == 422


def test_create_employee_invalid_patterns(client_app):
    # Invalid emp_code (not matching ^EMP\d{4,6}$)
    bad_code = {
        "emp_code": "INVALID123",
        "name": "Bad Code",
        "email": "bad@example.com",
        "department": "QA",
        "joined_on": "2026-01-01"
    }
    assert client_app.post("/employees", json=bad_code).status_code == 422

    # Invalid email
    bad_email = {
        "emp_code": "EMP0104",
        "name": "Bad Email",
        "email": "not-an-email",
        "department": "QA",
        "joined_on": "2026-01-01"
    }
    assert client_app.post("/employees", json=bad_email).status_code == 422

    # Invalid joined_on date
    bad_date = {
        "emp_code": "EMP0105",
        "name": "Bad Date",
        "email": "baddate@example.com",
        "department": "QA",
        "joined_on": "2026-02-31"  # Invalid calendar date
    }
    assert client_app.post("/employees", json=bad_date).status_code == 422


def test_employee_pagination_and_sorting(client_app):
    # Create 5 employees in reverse alphabetical order of emp_code
    codes = ["EMP0050", "EMP0040", "EMP0030", "EMP0020", "EMP0010"]
    for code in codes:
        client_app.post("/employees", json={
            "emp_code": code,
            "name": f"Employee {code}",
            "email": f"{code.lower()}@example.com",
            "department": "Engineering" if code in ("EMP0010", "EMP0030") else "HR",
            "joined_on": "2026-01-01"
        })

    # Page 1, page_size 2: must return EMP0010, EMP0020 (sorted ascending)
    resp = client_app.get("/employees?page=1&page_size=2")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 5
    assert data["page"] == 1
    assert data["page_size"] == 2
    assert [item["emp_code"] for item in data["items"]] == ["EMP0010", "EMP0020"]

    # Page 2, page_size 2: must return EMP0030, EMP0040
    resp2 = client_app.get("/employees?page=2&page_size=2")
    assert [item["emp_code"] for item in resp2.json()["items"]] == ["EMP0030", "EMP0040"]

    # Filter by department="Engineering": total must reflect filtered count (2)
    resp_dept = client_app.get("/employees?department=Engineering")
    dept_data = resp_dept.json()
    assert dept_data["total"] == 2
    assert [item["emp_code"] for item in dept_data["items"]] == ["EMP0010", "EMP0030"]


def test_concurrent_employee_creation(client_app):
    # Concurrent duplicate requests: exactly one returns 201, rest return 409
    payload = {
        "emp_code": "EMP0999",
        "name": "Race Candidate",
        "email": "race@example.com",
        "department": "Tech",
        "joined_on": "2026-01-01"
    }

    def send_request():
        return client_app.post("/employees", json=payload).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(send_request) for _ in range(5)]
        results = [f.result() for f in futures]

    assert results.count(201) == 1
    assert results.count(409) == 4
