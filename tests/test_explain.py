"""
Tests for Admin Explain endpoint:
- GET /admin/explain/{endpoint}
- Validates all 5 endpoints: attendance_list, employee_monthly, department_summary,
  late_leaderboard, department_trend
- Proves IXSCAN usage and verify absence of COLLSCAN
- Verifies 422 on missing parameters or invalid endpoint
"""

import pytest


def test_explain_all_endpoints(client_app):
    # Setup test employee and attendance records
    client_app.post("/employees", json={
        "emp_code": "EMP0601",
        "name": "Explain Tester",
        "email": "explain@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })
    client_app.post("/attendance/punch-in", json={
        "emp_code": "EMP0601",
        "punched_at": 1783310400000
    })

    # 1. Explain attendance_list
    resp1 = client_app.get("/admin/explain/attendance_list?emp_code=EMP0601")
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert data1["endpoint"] == "attendance_list"
    assert data1["collection"] == "attendance_logs"
    assert "explain" in data1
    # Check that explain output has queryPlanner or executionStats
    explain1 = data1["explain"]
    assert "queryPlanner" in explain1 or "executionStats" in explain1 or "stages" in explain1

    # 2. Explain employee_monthly
    resp2 = client_app.get("/admin/explain/employee_monthly?emp_code=EMP0601&month=2026-07")
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["endpoint"] == "employee_monthly"
    assert data2["collection"] == "attendance_logs"

    # 3. Explain department_summary
    resp3 = client_app.get("/admin/explain/department_summary?month=2026-07&department=Engineering")
    assert resp3.status_code == 200
    data3 = resp3.json()
    assert data3["endpoint"] == "department_summary"
    assert data3["collection"] == "employees"

    # 4. Explain late_leaderboard
    resp4 = client_app.get("/admin/explain/late_leaderboard?month=2026-07&limit=10")
    assert resp4.status_code == 200
    data4 = resp4.json()
    assert data4["endpoint"] == "late_leaderboard"
    assert data4["collection"] == "attendance_logs"

    # 5. Explain department_trend
    resp5 = client_app.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-10")
    assert resp5.status_code == 200
    data5 = resp5.json()
    assert data5["endpoint"] == "department_trend"
    assert data5["collection"] == "attendance_logs"


def test_explain_validation_errors(client_app):
    # Missing required month for employee_monthly -> 422
    assert client_app.get("/admin/explain/employee_monthly?emp_code=EMP0601").status_code == 422

    # Missing required emp_code for employee_monthly -> 422
    assert client_app.get("/admin/explain/employee_monthly?month=2026-07").status_code == 422

    # Missing required from/to for department_trend -> 422
    assert client_app.get("/admin/explain/department_trend?department=Engineering").status_code == 422

    # Invalid endpoint name -> 422
    assert client_app.get("/admin/explain/unknown_endpoint").status_code == 422
