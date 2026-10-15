# Employee Attendance & Analytics API

A high-performance, concurrency-safe backend REST API for employee attendance tracking and workforce analytics built with **FastAPI** and **MongoDB**.

This project implements the complete **HROne Software Engineer Hiring Engineering Assignment**, adhering strictly to the `openapi.yaml` contract, `DATA_MODEL.md` storage specifications, and business rules R1 through R10.

---

## 1. Technology Stack

- **Python**: 3.11+ (verified on 3.13)
- **Framework**: FastAPI (high performance, automatic OpenAPI documentation, Pydantic v2 validation)
- **Database**: MongoDB 6.0+ / 7.0 (aggregation pipelines with `$densify`, `$setWindowFields`, and compound indexes)
- **MongoDB Driver**: PyMongo 4.7+
- **Server**: Uvicorn ASGI server
- **Environment Management**: `python-dotenv`
- **Testing**: Pytest & HTTPX

---

## 2. Implemented Features

### System
- `GET /health` — Readiness probe verifying MongoDB connectivity with `ping` (returns 200 when connected, 503 if unreachable).

### Employees
- `POST /employees` — Creates an employee with client-supplied `emp_code`, pattern validation, and shift checks (`shift_start != shift_end`). Concurrency-safe via unique index on `emp_code` (returns 201 on success, 409 on duplicate).
- `GET /employees` — Paginated listing with exact department filter, sorted by `emp_code` ascending. `total` reflects filtered count.

### Attendance Management
- `POST /attendance/punch-in` — Records daily punch-in. Truncates timestamps to whole seconds (R1), calculates IST attendance date including overnight shifts (R1), computes late minutes with 10-minute grace period (R2), and prevents duplicate punches under concurrency via compound unique index on `(emp_code, date)` (returns 201 or 409).
- `POST /attendance/punch-out` — Closes an open attendance record. Validates that punch-out is after punch-in and within 24 hours. Calculates work hours with decimal half-up rounding (R4), overtime for >= 30 minutes (R3), and half-day status (< 4.50 hours, R5). Atomic conditional update guarantees concurrency safety (returns 200 or 409).
- `GET /attendance` — Fast paginated listing with date range, employee, and status filters. Sorts by `date` descending, then `emp_code` ascending directly in MongoDB. Validates `date_from <= date_to` (422).
- `PATCH /attendance/{emp_code}/{date}` — Regularization (manual correction). Recomputes derived fields, tracks specific field changes, and appends immutable audit history entries. Optimistic locking prevents lost updates.

### Analytics (MongoDB Aggregation Pipelines)
- `GET /analytics/employees/{emp_code}/monthly` — Monthly summary for an employee. Calculates working days from `joined_on` (Mon–Fri), counts present days (half day = 0.5), leave days, late count, total late minutes, overtime minutes, and attendance percentage with half-up rounding.
- `GET /analytics/departments/summary` — Cross-collection aggregation across `employees` and `attendance_logs`. Includes employees with zero attendance records in headcount, calculates average work hours over qualifying present records, and computes department totals.
- `GET /analytics/leaderboard/late` — Ranks employees by total late minutes using standard competition ranking (1, 2, 2, 4) via `$setWindowFields` with `$rank: {}`. Applies `limit` after ranking (tied employees at cutoff are returned).
- `GET /analytics/departments/{department}/trend` — Daily attendance trend across a date range (up to 92 days). Fills missing dates in database using `$densify` (no Python loops), calculates daily headcount based on join date, computes attendance rates on working days, and evaluates a 7-day moving average over preceding non-null rates.

### Administration & Verification
- `GET /admin/explain/{endpoint}` — Runs MongoDB `explain` with `executionStats` verbosity on the actual main query or pipeline of the specified endpoint. Proves `IXSCAN` usage and zero collection scans (`COLLSCAN`).

---

## 3. Prerequisites

- **Python 3.11** or newer.
- **MongoDB 6.0** or newer (or MongoDB Atlas free tier M0).
- Git.

---

## 4. Setup and Installation

### Step 1: Clone the repository
```bash
git clone <repository-url>
cd hrOne
```

### Step 2: Create and activate a virtual environment
**On Linux / macOS:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

**On Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

### Step 3: Install dependencies
```bash
pip install -r requirements.txt
```

### Step 4: Configure environment variables
Create a `.env` file in the project root (or set system environment variables):
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```
*(System environment variables take priority over `.env`)*.

---

## 5. Starting MongoDB & Loading Sample Data

### Local MongoDB (Docker option):
```bash
docker run -d -p 27017:27017 --name mongo-local mongo:7
```

### Load Sample Data:
```bash
python sample_seed.py
```
> **Warning**: `sample_seed.py` drops and reloads the sample collections in `MONGO_DB`.

---

## 6. Running the Application

Start the FastAPI application with Uvicorn:
```bash
uvicorn app.main:app --port 8000
```
To run with hot-reload during development:
```bash
uvicorn app.main:app --port 8000 --reload
```

The application creates all required indexes automatically and idempotently on startup. It is ready when `GET http://localhost:8000/health` returns HTTP 200:
```json
{"status": "ok"}
```

---

## 7. API Documentation (Swagger UI)

Interactive API documentation and schema explorer are available at:
- **Swagger UI**: `http://localhost:8000/docs`
- **ReDoc**: `http://localhost:8000/redoc`
- **OpenAPI JSON**: `http://localhost:8000/openapi.json`

---

## 8. Running Automated Tests

Run the complete test suite with `pytest`:
```bash
pytest -v
```
All tests run against isolated test databases or real MongoDB collections, testing CRUD, boundary cases, overnight shifts, competition ranking ties, gap filling, and concurrency.

---

## 9. Key Implementation Decisions

1. **One-File Architecture**: All application code lives in `app/main.py` per assignment rules, organized into clear logical sections.
2. **Database-Enforced Concurrency**: No application-level locks. MongoDB unique compound indexes prevent race conditions during employee creation and punch-in. Atomic conditional updates (`find_one_and_update`) protect punch-out and regularization.
3. **Pure Aggregations**: All analytics are executed directly inside MongoDB using aggregation pipelines (`$lookup`, `$densify`, `$setWindowFields`, `$rank`), avoiding loading 100k records into Python.
4. **Exact Half-Up Rounding**: Implemented via Python's `decimal.Decimal` with `ROUND_HALF_UP` to prevent banker's rounding discrepancies on halfway values.
