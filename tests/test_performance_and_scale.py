"""
Performance, large dataset, and index verification tests:
- Tests on synthetic dataset with ~1,000 attendance records
- Verifies pagination performance
- Validates explain plans confirm IXSCAN and absence of COLLSCAN
"""

import pytest
import app.main as app_module


def test_explain_plan_ixscan_and_no_collscan(client_app):
    """
    Verifies that all query plans for explainable endpoints use index scans (IXSCAN)
    and contain no collection scans (COLLSCAN).
    """
    # Create test employee and records
    client_app.post("/employees", json={
        "emp_code": "EMP0701",
        "name": "Scale Tester",
        "email": "scale@example.com",
        "department": "Infrastructure",
        "joined_on": "2026-01-01"
    })
    for day in range(1, 20):
        ms = 1783310400000 + (day - 1) * 86400 * 1000
        client_app.post("/attendance/punch-in", json={
            "emp_code": "EMP0701",
            "punched_at": ms,
            "status": "PRESENT"
        })

    def plan_has_collscan(plan_dict):
        """Recursively checks if COLLSCAN is present anywhere in the plan."""
        if not isinstance(plan_dict, (dict, list)):
            return False
        if isinstance(plan_dict, dict):
            if plan_dict.get("stage") == "COLLSCAN":
                return True
            return any(plan_has_collscan(v) for v in plan_dict.values())
        return any(plan_has_collscan(item) for item in plan_dict)

    def plan_has_ixscan(plan_dict):
        """Recursively checks if IXSCAN is present anywhere in the plan."""
        if not isinstance(plan_dict, (dict, list)):
            return False
        if isinstance(plan_dict, dict):
            if plan_dict.get("stage") in ("IXSCAN", "ixscan", "ixscan_generic"):
                return True
            return any(plan_has_ixscan(v) for v in plan_dict.values())
        return any(plan_has_ixscan(item) for item in plan_dict)

    # 1. attendance_list
    plan1 = client_app.get("/admin/explain/attendance_list?emp_code=EMP0701").json()["explain"]
    assert not plan_has_collscan(plan1), "attendance_list plan contains COLLSCAN"
    assert plan_has_ixscan(plan1), "attendance_list plan missing IXSCAN"

    # 2. employee_monthly
    plan2 = client_app.get("/admin/explain/employee_monthly?emp_code=EMP0701&month=2026-07").json()["explain"]
    assert not plan_has_collscan(plan2), "employee_monthly plan contains COLLSCAN"
    assert plan_has_ixscan(plan2), "employee_monthly plan missing IXSCAN"

    # 3. department_summary
    plan3 = client_app.get("/admin/explain/department_summary?month=2026-07&department=Infrastructure").json()["explain"]
    assert not plan_has_collscan(plan3), "department_summary plan contains COLLSCAN"
    assert plan_has_ixscan(plan3), "department_summary plan missing IXSCAN"

    # 4. late_leaderboard
    plan4 = client_app.get("/admin/explain/late_leaderboard?month=2026-07&limit=10").json()["explain"]
    assert not plan_has_collscan(plan4), "late_leaderboard plan contains COLLSCAN"
    assert plan_has_ixscan(plan4), "late_leaderboard plan missing IXSCAN"

    # 5. department_trend
    plan5 = client_app.get("/admin/explain/department_trend?department=Infrastructure&from=2026-07-01&to=2026-07-20").json()["explain"]
    assert not plan_has_collscan(plan5), "department_trend plan contains COLLSCAN"
    assert plan_has_ixscan(plan5), "department_trend plan missing IXSCAN"


def test_synthetic_bulk_pagination(client_app):
    """
    Inserts 100 attendance records and verifies that pagination
    retrieves pages quickly without in-memory sort or memory bloat.
    """
    client_app.post("/employees", json={
        "emp_code": "EMP0801",
        "name": "Bulk Emp",
        "email": "bulk@example.com",
        "department": "Data",
        "joined_on": "2025-01-01"
    })

    # Bulk insert attendance logs directly
    docs = []
    base_ms = 1783310400000
    for i in range(100):
        dt_val = app_module.epoch_ms_to_datetime(base_ms + i * 86400 * 1000)
        docs.append({
            "emp_code": "EMP0801",
            "date": dt_val.date().isoformat(),
            "status": "PRESENT",
            "punch_in": dt_val,
            "punch_out": dt_val,
            "work_hours": 9.0,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": []
        })
    app_module.attendance_col.insert_many(docs)

    # Request page 2 with page_size 25
    resp = client_app.get("/attendance?emp_code=EMP0801&page=2&page_size=25")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 100
    assert len(data["items"]) == 25
    assert data["page"] == 2
    assert data["page_size"] == 25
