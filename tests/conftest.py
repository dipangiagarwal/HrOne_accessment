"""
Pytest configuration and fixtures.
Provides an isolated MongoDB test database and FastAPI TestClient.
"""

import os
import pytest
from fastapi.testclient import TestClient

# Use an isolated test database name so tests never touch production or sample data
TEST_DB_NAME = "attendance_test_db"
os.environ["MONGO_DB"] = TEST_DB_NAME

import app.main as app_module


@pytest.fixture(scope="session")
def db_client():
    """Provides a MongoDB client connected to the test database."""
    test_db = app_module.client[TEST_DB_NAME]
    # Set app module db references to the test database
    app_module.db = test_db
    app_module.employees_col = test_db["employees"]
    app_module.attendance_col = test_db["attendance_logs"]
    yield test_db
    # Cleanup after test session
    app_module.client.drop_database(TEST_DB_NAME)


@pytest.fixture(autouse=True)
def clean_database(db_client):
    """
    Cleans collections before each test to ensure test isolation,
    and runs startup index creation.
    """
    db_client.employees.delete_many({})
    db_client.attendance_logs.delete_many({})
    # Re-run index creation
    app_module.employees_col.create_index([("emp_code", 1)], unique=True, name="idx_emp_code_unique")
    app_module.employees_col.create_index([("department", 1), ("joined_on", 1)], name="idx_emp_dept_joined")
    app_module.employees_col.create_index([("joined_on", 1), ("department", 1)], name="idx_emp_joined_dept")
    app_module.attendance_col.create_index([("emp_code", 1), ("date", 1)], unique=True, name="idx_att_emp_date_unique")
    app_module.attendance_col.create_index([("date", -1), ("emp_code", 1)], name="idx_att_date_desc_emp_asc")
    app_module.attendance_col.create_index([("date", 1), ("late_minutes", 1), ("emp_code", 1)], name="idx_att_date_late_emp")
    app_module.attendance_col.create_index([("emp_code", 1), ("punch_in", -1)], name="idx_att_emp_punch_in_desc")
    yield


@pytest.fixture
def client_app():
    """Provides a TestClient for making requests to the FastAPI application."""
    with TestClient(app_module.app) as test_client:
        yield test_client
