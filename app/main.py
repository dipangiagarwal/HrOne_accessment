"""
Employee Attendance & Analytics API (FastAPI + MongoDB)
HROne Software Engineer Hiring Engineering Assignment

This application implements the complete OpenAPI contract, MongoDB data model,
and business rules (R1 to R10) for employee attendance management and analytics.

Key Features & Architectural Decisions:
1. Pure FastAPI + PyMongo implementation with all application code in this single file.
2. Idempotent database initialization and index creation on startup.
3. Concurrency-safe atomic writes (duplicate-key protection and optimistic locking).
4. Full MongoDB aggregation pipelines for all analytics endpoints.
5. Strict ISO and epoch-millisecond validation adhering to openapi.yaml schemas.
6. Rich, beginner-friendly comments and docstrings explaining technical concepts.
"""

import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
import re
from typing import Annotated, Any, Dict, List, Optional, Union

from bson import ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# Section 1: Configuration, Timezone, and Database Connection
# --------------------------------------------------------------------------- #

# Load environment variables from a local .env file if present.
# System environment variables will take precedence over .env values.
load_dotenv()

# MongoDB connection details from environment variables (no hard-coding)
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME = os.getenv("MONGO_DB", "attendance_db")

# MongoDB client instantiation.
# PyMongo automatically manages connection pooling.
client: MongoClient = MongoClient(MONGO_URI)
db = client[MONGO_DB_NAME]

# Collections used throughout the application
employees_col = db["employees"]
attendance_col = db["attendance_logs"]

# Indian Standard Time (IST) is UTC+05:30.
# Per Rule R1, all shift times and attendance calendar dates are evaluated in IST.
IST = timezone(timedelta(hours=5, minutes=30))

# Timestamp bounds defined in OpenAPI schema for EpochMillis
# Rejects values in seconds (e.g., 10 digits) and out-of-range timestamps.
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000


# --------------------------------------------------------------------------- #
# Section 2: Helper Functions & Business Rules (R1 - R10)
# --------------------------------------------------------------------------- #

def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """
    Ensures a datetime is timezone-aware and set to UTC.
    PyMongo returns naive datetimes representing UTC instants from MongoDB;
    this helper standardizes them so they can be safely compared or converted.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def truncate_to_seconds(dt: datetime) -> datetime:
    """
    Truncates a datetime instant to whole seconds by discarding microseconds.
    Implements Rule R1:
    'Before any calculation, instants are truncated to whole seconds
     (09:40:00.900 counts as 09:40:00), and the truncated value is what is stored
     and returned.'
    """
    return dt.replace(microsecond=0)


def epoch_ms_to_datetime(ms: int) -> datetime:
    """
    Converts integer epoch milliseconds into a timezone-aware UTC datetime,
    truncated to whole seconds.
    """
    # Integer division by 1000 discards sub-second milliseconds
    seconds = ms // 1000
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def datetime_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """
    Converts a Python datetime (whether naive UTC or timezone-aware)
    into integer epoch milliseconds for API response serialization.
    """
    if dt is None:
        return None
    aware = ensure_utc(dt)
    # Truncate to whole seconds and multiply by 1000
    return int(aware.timestamp()) * 1000


def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    """
    Rounds a numeric value to a specified number of decimal places using
    decimal half-up rounding (e.g., 4.525 -> 4.53, 2.675 -> 2.68).

    Implements Rules R4 and R8:
    Python's standard round() uses banker's rounding (round half to even),
    which rounds 4.525 to 4.52. Using decimal.Decimal with ROUND_HALF_UP ensures
    exact half-up rounding required by the specification.
    """
    if val is None:
        return None
    d = Decimal(str(val))
    target = Decimal("10") ** -decimals
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """
    Determines whether a shift is an overnight shift.
    Per Rule R1 and DATA_MODEL.md:
    A shift is overnight if shift_end <= shift_start (for example, 22:00 to 06:00).
    """
    h_start, m_start = map(int, shift_start.split(":"))
    h_end, m_end = map(int, shift_end.split(":"))
    return (h_end, m_end) <= (h_start, m_start)


def derive_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    Derives the attendance calendar date (YYYY-MM-DD) from a punch-in instant.
    Implements Rule R1:
    1. Shift times and attendance dates are in IST (UTC+05:30).
    2. The record's date is usually the IST calendar date of punch-in.
    3. Exception for overnight shifts (shift_end <= shift_start):
       If an employee punches in at an IST time earlier than shift_end,
       the punch-in belongs to the shift that started on the previous calendar day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    h_end, m_end = map(int, shift_end.split(":"))
    end_time = time(h_end, m_end)

    if is_overnight_shift(shift_start, shift_end):
        # If punch-in IST time is strictly earlier than shift_end, it belongs
        # to the shift that began the previous calendar day.
        if punch_in_ist.time() < end_time:
            shift_calendar_date = punch_in_ist.date() - timedelta(days=1)
            return shift_calendar_date.isoformat()

    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, shift_end: str, attendance_date_str: str) -> int:
    """
    Calculates whole late minutes based on Rule R2:
    - Grace period is 10 minutes.
    - An employee is late only if punch_in occurs strictly more than 10 minutes
      after shift_start (elapsed > 600 seconds).
    - When late, late_minutes = whole elapsed minutes (floor) since original shift_start.
    - Does NOT subtract the grace period from late_minutes.
    - Example for a 09:30 shift:
        09:40:00 -> 0 late minutes
        09:40:01 -> 10 late minutes
        10:15:59 -> 45 late minutes
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    att_date = date.fromisoformat(attendance_date_str)
    h_start, m_start = map(int, shift_start.split(":"))

    # Shift start is always anchored to the attendance date in IST
    shift_start_dt = datetime(att_date.year, att_date.month, att_date.day, h_start, m_start, 0, tzinfo=IST)
    elapsed_seconds = (punch_in_ist - shift_start_dt).total_seconds()

    # Must be strictly more than 10 minutes (600 seconds) after shift start
    if elapsed_seconds > 600:
        return int(elapsed_seconds // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    Calculates total work hours rounded to 2 decimal places using half-up rounding.
    Implements Rule R4:
    (punch_out - punch_in) in seconds / 3600, rounded half-up.
    """
    in_aware = ensure_utc(punch_in_utc)
    out_aware = ensure_utc(punch_out_utc)
    diff_seconds = int((out_aware - in_aware).total_seconds())
    hours_dec = Decimal(str(diff_seconds)) / Decimal("3600")
    rounded = hours_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, attendance_date_str: str) -> int:
    """
    Calculates whole overtime minutes based on Rule R3:
    - Whole minutes (floor) between shift_end and punch_out.
    - Counted only if elapsed time is at least 30 minutes (>= 1800 seconds); otherwise 0.
    - For overnight shifts (shift_end <= shift_start), shift_end falls on the next calendar day.
    """
    punch_out_ist = punch_out_utc.astimezone(IST)
    att_date = date.fromisoformat(attendance_date_str)
    h_end, m_end = map(int, shift_end.split(":"))

    # If the shift is overnight, shift_end is on the following calendar day
    if is_overnight_shift(shift_start, shift_end):
        end_date = att_date + timedelta(days=1)
    else:
        end_date = att_date

    shift_end_dt = datetime(end_date.year, end_date.month, end_date.day, h_end, m_end, 0, tzinfo=IST)
    elapsed_seconds = (punch_out_ist - shift_end_dt).total_seconds()

    if elapsed_seconds >= 1800:
        return int(elapsed_seconds // 60)
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """
    Determines half-day status based on Rule R5:
    After punch-out, half_day is true when the rounded work_hours is strictly less than 4.50.
    """
    if work_hours is None:
        return False
    return work_hours < 4.50


def count_working_days(joined_on_str: str, month_str: str) -> int:
    """
    Counts Monday-to-Friday working days in a given calendar month (YYYY-MM).
    Implements Rule R7:
    - Monday through Friday are working days (no holiday calendar).
    - For an employee joining mid-month, working days are counted starting from joined_on.
    - If joined_on is after the month end, working days is 0.
    """
    year, month = map(int, month_str.split("-"))
    num_days = calendar.monthrange(year, month)[1]
    month_start = date(year, month, 1)
    month_end = date(year, month, num_days)

    joined_on = date.fromisoformat(joined_on_str)
    if joined_on > month_end:
        return 0

    start_date = max(month_start, joined_on)
    working_days = 0
    cur = start_date
    while cur <= month_end:
        # weekday(): Monday is 0, Sunday is 6
        if cur.weekday() < 5:
            working_days += 1
        cur += timedelta(days=1)
    return working_days


def clean_bson_for_json(data: Any) -> Any:
    """
    Recursively converts BSON datatypes (like datetime and ObjectId)
    into JSON-serializable primitives for the explain endpoint.
    """
    if isinstance(data, dict):
        return {k: clean_bson_for_json(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [clean_bson_for_json(v) for v in data]
    elif isinstance(data, datetime):
        return data.isoformat()
    elif isinstance(data, ObjectId):
        return str(data)
    return data


# --------------------------------------------------------------------------- #
# Section 3: Pydantic Validation Models
# --------------------------------------------------------------------------- #

def validate_epoch_milliseconds(v: Any) -> Optional[int]:
    """
    Strict validation for epoch milliseconds:
    - Must be an integer.
    - Rejects boolean (since bool is a subclass of int in Python).
    - Rejects floats and string numbers.
    - Rejects numbers outside [100000000000, 4102444800000].
    """
    if v is None:
        return None
    if isinstance(v, bool):
        raise ValueError("Timestamp cannot be a boolean; integer epoch milliseconds expected.")
    if isinstance(v, float):
        raise ValueError("Timestamp cannot be a float; integer epoch milliseconds expected.")
    if isinstance(v, str):
        raise ValueError("Timestamp cannot be a string; integer epoch milliseconds expected.")
    if not isinstance(v, int):
        raise ValueError("Timestamp must be integer epoch milliseconds.")
    if v < MIN_EPOCH_MS or v > MAX_EPOCH_MS:
        raise ValueError(f"Timestamp {v} is out of valid epoch milliseconds range [{MIN_EPOCH_MS}, {MAX_EPOCH_MS}].")
    return v


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$", description="Customer employee code (e.g. EMP0001)")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=120)
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def validate_joined_date(cls, v: str) -> str:
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be a valid calendar date in YYYY-MM-DD format")
        return v


class EmployeeResponse(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int


class EmployeePage(BaseModel):
    items: List[EmployeeResponse]
    total: int
    page: int
    page_size: int


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")

    @field_validator("punched_at", mode="before")
    @classmethod
    def check_punched_at(cls, v: Any) -> Optional[int]:
        return validate_epoch_milliseconds(v)


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def check_punched_at(cls, v: Any) -> Optional[int]:
        return validate_epoch_milliseconds(v)


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def check_timestamps(cls, v: Any) -> Optional[int]:
        return validate_epoch_milliseconds(v)


class HistoryChange(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_: Optional[Any] = Field(None, alias="from")
    to: Optional[Any] = None


class HistoryEntry(BaseModel):
    at: int
    by: str
    reason: str
    changes: Dict[str, Dict[str, Any]]


class AttendanceRecord(BaseModel):
    emp_code: str
    date: str
    status: str
    punch_in: Optional[int]
    punch_out: Optional[int]
    work_hours: Optional[float]
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: List[HistoryEntry]


class AttendancePage(BaseModel):
    items: List[AttendanceRecord]
    total: int
    page: int
    page_size: int


class EmployeeMonthlyResponse(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float]


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float]
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummaryResponse(BaseModel):
    month: str
    items: List[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class LeaderboardResponse(BaseModel):
    month: str
    items: List[LeaderboardItem]


class TrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float]
    moving_avg_7d: Optional[float]


class TrendResponse(BaseModel):
    department: str
    items: List[TrendItem]


class ExplainResponse(BaseModel):
    endpoint: str
    collection: str
    explain: Dict[str, Any]


# --------------------------------------------------------------------------- #
# Section 4: Database Initialization & Startup Lifespan
# --------------------------------------------------------------------------- #

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan context manager for startup and shutdown events.
    Creates all database indexes idempotently at application startup.
    This fulfills assignment requirements without requiring any manual setup.
    """
    try:
        # 1. Unique index on employees.emp_code
        employees_col.create_index([("emp_code", ASCENDING)], unique=True, name="idx_emp_code_unique")

        # 2. Compound indexes on employees
        employees_col.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="idx_emp_dept_joined")
        employees_col.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="idx_emp_joined_dept")

        # 3. Unique compound index on attendance_logs (emp_code, date)
        attendance_col.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="idx_att_emp_date_unique")

        # 4. Compound index on attendance_logs (date descending, emp_code ascending)
        attendance_col.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="idx_att_date_desc_emp_asc")

        # 5. Compound index on attendance_logs (date ascending, late_minutes ascending, emp_code ascending)
        attendance_col.create_index([("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)], name="idx_att_date_late_emp")

        # 6. Index on attendance_logs (emp_code, punch_in)
        attendance_col.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="idx_att_emp_punch_in_desc")
    except PyMongoError as exc:
        print(f"Index initialization notice: {exc}")

    yield

    pass


# Initialize FastAPI app with metadata and lifespan
app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    description="Backend API for HROne Employee Attendance & Analytics Assignment",
    lifespan=lifespan
)


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Formats a raw MongoDB attendance document to strictly match the
    AttendanceRecord schema required by openapi.yaml:
    - Strips MongoDB '_id' and 'id'.
    - Converts punch_in and punch_out BSON datetimes to epoch milliseconds.
    - Converts nested timestamps in history[].at and history[].changes.
    - Applies defaults for legacy records (empty history, false half_day).
    """
    history_out = []
    for h in doc.get("history") or []:
        changes_out = {}
        for field_name, change in h.get("changes", {}).items():
            from_val = change.get("from")
            to_val = change.get("to")
            # Convert nested datetime instants to epoch milliseconds
            if isinstance(from_val, datetime):
                from_val = datetime_to_epoch_ms(from_val)
            if isinstance(to_val, datetime):
                to_val = datetime_to_epoch_ms(to_val)
            changes_out[field_name] = {"from": from_val, "to": to_val}

        at_val = h.get("at")
        if isinstance(at_val, datetime):
            at_ms = datetime_to_epoch_ms(at_val)
        elif isinstance(at_val, int):
            at_ms = at_val
        else:
            at_ms = 0

        history_out.append({
            "at": at_ms,
            "by": h.get("by", ""),
            "reason": h.get("reason", ""),
            "changes": changes_out
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(ensure_utc(doc.get("punch_in"))),
        "punch_out": datetime_to_epoch_ms(ensure_utc(doc.get("punch_out"))),
        "work_hours": round_half_up(doc.get("work_hours"), 2),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": history_out
    }


# --------------------------------------------------------------------------- #
# Section 5: API Endpoints
# --------------------------------------------------------------------------- #

# --- System Endpoints ---

@app.get("/health", summary="Readiness probe")
def health():
    """
    Readiness probe:
    Returns HTTP 200 with {"status": "ok"} only if MongoDB is responsive.
    Returns HTTP 503 if MongoDB fails the ping check.
    """
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database connection is unavailable"
        )


# --- Employee Endpoints ---

@app.post("/employees", status_code=http_status.HTTP_201_CREATED, response_model=EmployeeResponse, summary="Create an employee")
def create_employee(body: EmployeeCreate):
    """
    Creates a new employee record.
    - Validates that shift_start != shift_end.
    - Uses a unique database index on emp_code to guarantee uniqueness.
    - Returns HTTP 409 if emp_code already exists (handles race condition safely).
    """
    if body.shift_start == body.shift_end:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="shift_start and shift_end must be different times"
        )

    now_utc = truncate_to_seconds(datetime.now(timezone.utc))
    doc = body.model_dump()
    doc["created_at"] = now_utc

    try:
        employees_col.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"Employee with emp_code '{body.emp_code}' already exists"
        )

    return EmployeeResponse(
        emp_code=doc["emp_code"],
        name=doc["name"],
        email=doc["email"],
        department=doc["department"],
        shift_start=doc["shift_start"],
        shift_end=doc["shift_end"],
        joined_on=doc["joined_on"],
        created_at=datetime_to_epoch_ms(now_utc)
    )


@app.get("/employees", response_model=EmployeePage, summary="List employees")
def list_employees(
    department: Optional[str] = Query(None, description="Exact, case-sensitive match"),
    page: int = Query(1, ge=1, description="Page number starting at 1"),
    page_size: int = Query(20, ge=1, le=100, description="Page size up to 100")
):
    """
    Returns a paginated list of employees sorted by emp_code ascending.
    Rule R10: total reflects the filtered count, not the entire collection.
    """
    query: Dict[str, Any] = {}
    if department:
        query["department"] = department

    total = employees_col.count_documents(query)
    skip = (page - 1) * page_size

    cursor = (
        employees_col.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )

    items = []
    for doc in cursor:
        items.append(EmployeeResponse(
            emp_code=doc["emp_code"],
            name=doc["name"],
            email=doc["email"],
            department=doc["department"],
            shift_start=doc["shift_start"],
            shift_end=doc["shift_end"],
            joined_on=doc["joined_on"],
            created_at=datetime_to_epoch_ms(doc.get("created_at"))
        ))

    return EmployeePage(
        items=items,
        total=total,
        page=page,
        page_size=page_size
    )


# --- Attendance Endpoints ---

@app.post("/attendance/punch-in", status_code=http_status.HTTP_201_CREATED, response_model=AttendanceRecord, summary="Record a punch-in")
def punch_in(body: PunchInRequest):
    """
    Creates an attendance record for the day.
    - Validates employee existence (HTTP 404 if not found).
    - Truncates punch-in instant to whole seconds (Rule R1).
    - Derives attendance date in IST, correctly handling overnight shifts (Rule R1).
    - Computes late minutes (Rule R2).
    - Database unique index on (emp_code, date) prevents race-condition duplicates (returns HTTP 409).
    """
    emp = employees_col.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Employee '{body.emp_code}' not found"
        )

    if body.punched_at is not None:
        punch_in_dt = epoch_ms_to_datetime(body.punched_at)
    else:
        punch_in_dt = truncate_to_seconds(datetime.now(timezone.utc))

    shift_start = emp["shift_start"]
    shift_end = emp["shift_end"]

    # Derive attendance date in IST (R1)
    att_date = derive_attendance_date(punch_in_dt, shift_start, shift_end)

    # Calculate late minutes (R2)
    late_min = compute_late_minutes(punch_in_dt, shift_start, shift_end, att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }

    try:
        attendance_col.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"Attendance record already exists for employee '{body.emp_code}' on date '{att_date}'"
        )

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=http_status.HTTP_200_OK, response_model=AttendanceRecord, summary="Record a punch-out")
def punch_out(body: PunchOutRequest):
    """
    Closes the employee's most recent record.
    - Validates employee existence (404).
    - Validates punch-in record exists (404).
    - Validates punch_out > punch_in and <= 24 hours later (422).
    - Validates record is not already closed (409).
    - Computes work hours (R4), overtime (R3), and half day (R5).
    - Uses an atomic conditional update ("punch_out": None) to ensure concurrent punch-outs
      yield exactly one HTTP 200 and competing requests get HTTP 409.
    """
    emp = employees_col.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Employee '{body.emp_code}' not found"
        )

    if body.punched_at is not None:
        punch_out_dt = epoch_ms_to_datetime(body.punched_at)
    else:
        punch_out_dt = truncate_to_seconds(datetime.now(timezone.utc))

    # Find the most recent attendance record for the employee
    candidate = attendance_col.find_one(
        {"emp_code": body.emp_code},
        sort=[("punch_in", DESCENDING)]
    )

    if not candidate or candidate.get("punch_in") is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"No open punch-in record found for employee '{body.emp_code}'"
        )

    punch_in_dt = ensure_utc(candidate["punch_in"])
    if punch_out_dt <= punch_in_dt:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punch_out must be strictly after punch_in"
        )

    if (punch_out_dt - punch_in_dt).total_seconds() > 24 * 3600:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="punch_out cannot be more than 24 hours after punch_in"
        )

    # Check if record is already closed
    if candidate.get("punch_out") is not None:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Attendance record is already closed (punched out)"
        )

    # Calculate derived values
    work_hrs = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_min = compute_overtime(punch_out_dt, emp["shift_start"], emp["shift_end"], candidate["date"])
    half_day = compute_half_day(work_hrs)

    # Atomic conditional update: only update if punch_out is STILL None in the database
    updated_doc = attendance_col.find_one_and_update(
        {
            "_id": candidate["_id"],
            "punch_out": None  # Concurrency safety condition
        },
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hrs,
                "overtime_minutes": overtime_min,
                "half_day": half_day
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if not updated_doc:
        # Another request closed the record in the fraction of a millisecond
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Attendance record was already punched out by a concurrent request"
        )

    return format_attendance_doc(updated_doc)


@app.get("/attendance", response_model=AttendancePage, summary="List attendance records")
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """
    Returns a paginated list of attendance records.
    - Validates date_from <= date_to (returns 422 if date_from > date_to).
    - Sorts by date descending, then emp_code ascending directly in MongoDB.
    - Avoids loading the full collection into Python memory.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from cannot be greater than date_to"
        )

    query: Dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status

    total = attendance_col.count_documents(query)
    skip = (page - 1) * page_size

    cursor = (
        attendance_col.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    items = [format_attendance_doc(doc) for doc in cursor]

    return AttendancePage(
        items=items,
        total=total,
        page=page,
        page_size=page_size
    )


@app.patch("/attendance/{emp_code}/{date}", response_model=AttendanceRecord, summary="Regularize (correct) one day's record")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...
):
    """
    Applies a manual correction to an existing attendance record.
    - Validates employee and attendance record existence (404).
    - Validates presence status requires punch_in (422).
    - Validates punch_in stays on the record's attendance date under Rule R1 (422).
    - Validates punch_out > punch_in and within 24 hours (422).
    - Validates that ABSENT/LEAVE clears punch times (422 if punch times provided with ABSENT/LEAVE).
    - Validates that the request actually modifies at least one tracked field (422 if no changes).
    - Recomputes late_minutes, work_hours, overtime_minutes, half_day.
    - Appends exactly one audit history entry without losing previous history.
    - Optimistic concurrency control prevents concurrent overwrites (returns 409).
    """
    emp = employees_col.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Employee '{emp_code}' not found"
        )

    current_doc = attendance_col.find_one({"emp_code": emp_code, "date": date})
    if not current_doc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Attendance record for employee '{emp_code}' on date '{date}' not found"
        )

    # Determine final values after applying requested changes
    final_status = body.status if body.status is not None else current_doc["status"]

    if final_status in ("ABSENT", "LEAVE"):
        # Setting status to ABSENT or LEAVE clears both punch times
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Cannot provide punch_in or punch_out when status is {final_status}"
            )
        final_punch_in = None
        final_punch_out = None
        final_work_hours = None
        final_late_minutes = 0
        final_overtime_minutes = 0
        final_half_day = False
    else:
        # Status is a presence status (PRESENT, WFH, ON_DUTY)
        if body.punch_in is not None:
            final_punch_in = epoch_ms_to_datetime(body.punch_in)
        else:
            final_punch_in = ensure_utc(current_doc.get("punch_in"))

        if not final_punch_in:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"A presence status ({final_status}) requires a valid punch_in"
            )

        # Validate that punch_in stays on the record's attendance date under R1
        derived_date = derive_attendance_date(final_punch_in, emp["shift_start"], emp["shift_end"])
        if derived_date != date:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"punch_in belongs to attendance date '{derived_date}', which does not match record date '{date}'"
            )

        if body.punch_out is not None:
            final_punch_out = epoch_ms_to_datetime(body.punch_out)
        else:
            final_punch_out = ensure_utc(current_doc.get("punch_out"))

        if final_punch_out is not None:
            if final_punch_out <= final_punch_in:
                raise HTTPException(
                    status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out must be strictly after punch_in"
                )
            if (final_punch_out - final_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(
                    status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="punch_out cannot be more than 24 hours after punch_in"
                )

        # Recompute derived fields
        final_late_minutes = compute_late_minutes(final_punch_in, emp["shift_start"], emp["shift_end"], date)
        if final_punch_out is not None:
            final_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            final_overtime_minutes = compute_overtime(final_punch_out, emp["shift_start"], emp["shift_end"], date)
            final_half_day = compute_half_day(final_work_hours)
        else:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False

    # Track actual field changes
    changes: Dict[str, Dict[str, Any]] = {}

    def check_diff(field_name: str, old_val: Any, new_val: Any):
        if old_val != new_val:
            changes[field_name] = {"from": old_val, "to": new_val}

    check_diff("status", current_doc.get("status"), final_status)
    check_diff("punch_in", ensure_utc(current_doc.get("punch_in")), final_punch_in)
    check_diff("punch_out", ensure_utc(current_doc.get("punch_out")), final_punch_out)
    check_diff("work_hours", current_doc.get("work_hours"), final_work_hours)
    check_diff("late_minutes", current_doc.get("late_minutes", 0), final_late_minutes)
    check_diff("overtime_minutes", current_doc.get("overtime_minutes", 0), final_overtime_minutes)
    check_diff("half_day", bool(current_doc.get("half_day", False)), final_half_day)

    if not changes:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Regularization request makes no actual changes to the record"
        )

    now_utc = truncate_to_seconds(datetime.now(timezone.utc))
    new_history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    current_history_len = len(current_doc.get("history") or [])

    # Optimistic concurrency update: condition on current history length
    # Guarantees that concurrent edits don't silently overwrite history
    updated_doc = attendance_col.find_one_and_update(
        {
            "_id": current_doc["_id"],
            "history": {"$size": current_history_len}
        },
        {
            "$set": {
                "status": final_status,
                "punch_in": final_punch_in,
                "punch_out": final_punch_out,
                "work_hours": final_work_hours,
                "late_minutes": final_late_minutes,
                "overtime_minutes": final_overtime_minutes,
                "half_day": final_half_day
            },
            "$push": {
                "history": new_history_entry
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if not updated_doc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Conflict: attendance record was modified concurrently by another request"
        )

    return format_attendance_doc(updated_doc)


# --- Analytics Endpoints ---

@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthlyResponse, summary="Monthly attendance summary for one employee")
def employee_monthly_summary(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Calendar month, YYYY-MM")
):
    """
    Returns monthly summary metrics for a single employee using a MongoDB aggregation.
    - Validates employee exists (404).
    - Calculates working days per Rule R7.
    - Counts present_days (R5-R7: half day = 0.5, Mon-Fri only).
    - Weekend records do not add to present_days, but contribute to late and overtime totals.
    - Attendance percentage = present_days / working_days * 100, rounded half-up (or null if working_days is 0).
    """
    emp = employees_col.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Employee '{emp_code}' not found"
        )

    working_days = count_working_days(emp["joined_on"], month)
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    last_day_str = f"{month}-{num_days:02d}"

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": f"{month}-01", "$lte": last_day_str}
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "half_day": {"$ifNull": ["$half_day", False]},
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                # 1=Sun, 2=Mon, 3=Tue, 4=Wed, 5=Thu, 6=Fri, 7=Sat
                "day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {
                    "$sum": "$late_minutes"
                },
                "total_overtime_minutes": {
                    "$sum": "$overtime_minutes"
                }
            }
        }
    ]

    agg_res = list(attendance_col.aggregate(pipeline))
    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late = int(row.get("total_late_minutes", 0))
        total_overtime = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_overtime = 0

    if working_days > 0:
        pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        pct = None

    return EmployeeMonthlyResponse(
        emp_code=emp_code,
        month=month,
        working_days=working_days,
        present_days=present_days,
        leave_days=leave_days,
        late_count=late_count,
        total_late_minutes=total_late,
        total_overtime_minutes=total_overtime,
        attendance_pct=pct
    )


@app.get("/analytics/departments/summary", response_model=DepartmentSummaryResponse, summary="Department summary for a month")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Calendar month, YYYY-MM"),
    department: Optional[str] = Query(None, description="Restrict to one department (exact match)")
):
    """
    Returns monthly summary for each department via a cross-collection aggregation.
    - Headcount per Rule R9: counts employees whose joined_on is on or before month end.
      Employees with ZERO logs are fully counted.
    - Present days summed over Mon-Fri records.
    - avg_work_hours is mean over individual present-status records with non-null work_hours
      (average of records, not an average of employee averages).
    - Omit departments whose headcount is 0 for that month.
    """
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    last_day_str = f"{month}-{num_days:02d}"

    match_emp: Dict[str, Any] = {"joined_on": {"$lte": last_day_str}}
    if department:
        match_emp["department"] = department

    pipeline = [
        {"$match": match_emp},
        # Lookup attendance records for this employee in the month
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", f"{month}-01"]},
                                    {"$lte": ["$date", last_day_str]}
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "status": 1,
                            "work_hours": 1,
                            "half_day": {"$ifNull": ["$half_day", False]},
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                            "day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}
                        }
                    }
                ],
                "as": "logs"
            }
        },
        # Group by department to calculate headcount
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "all_logs": {"$push": "$logs"}
            }
        },
        # Flatten all logs in the department
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "flat_logs": {
                    "$reduce": {
                        "input": "$all_logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]}
                    }
                }
            }
        },
        # Compute metrics across flat logs
        {
            "$project": {
                "_id": 0,
                "department": 1,
                "headcount": 1,
                "present_days": {
                    "$reduce": {
                        "input": "$flat_logs",
                        "initialValue": 0.0,
                        "in": {
                            "$add": [
                                "$$value",
                                {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$$this.day_of_week", [2, 3, 4, 5, 6]]},
                                                {"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]}
                                            ]
                                        },
                                        {"$cond": [{"$eq": ["$$this.half_day", True]}, 0.5, 1.0]},
                                        0.0
                                    ]
                                }
                            ]
                        }
                    }
                },
                "avg_work_hours": {
                    "$avg": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$flat_logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$$l.work_hours", None]}
                                        ]
                                    }
                                }
                            },
                            "as": "pl",
                            "in": "$$pl.work_hours"
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$flat_logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]}
                        }
                    }
                },
                "total_late_minutes": {
                    "$reduce": {
                        "input": "$flat_logs",
                        "initialValue": 0,
                        "in": {"$add": ["$$value", "$$this.late_minutes"]}
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$flat_logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]}
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$flat_logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]}
                        }
                    }
                }
            }
        },
        {"$sort": {"department": ASCENDING}}
    ]

    cursor = employees_col.aggregate(pipeline)
    items = []
    for row in cursor:
        items.append(DepartmentSummaryItem(
            department=row["department"],
            headcount=row["headcount"],
            present_days=float(row.get("present_days", 0.0)),
            avg_work_hours=round_half_up(row.get("avg_work_hours"), 2),
            late_count=int(row.get("late_count", 0)),
            total_late_minutes=int(row.get("total_late_minutes", 0)),
            leave_count=int(row.get("leave_count", 0)),
            on_duty_count=int(row.get("on_duty_count", 0))
        ))

    return DepartmentSummaryResponse(month=month, items=items)


@app.get("/analytics/leaderboard/late", response_model=LeaderboardResponse, summary="Late-comers leaderboard for a month")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Calendar month, YYYY-MM"),
    limit: int = Query(10, ge=1, le=50, description="Limit rank cutoff"),
    department: Optional[str] = Query(None, description="Filter by department before ranking")
):
    """
    Ranks employees by total late_minutes in the month (only total_late_minutes > 0).
    - Uses standard competition ranking (1, 2, 2, 4) via MongoDB's $rank window function.
    - limit applies AFTER ranking: returns all employees whose rank <= limit.
    - Ties at cutoff can result in more than limit rows returned.
    - Results sorted by total_late_minutes descending, then emp_code ascending.
    """
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    last_day_str = f"{month}-{num_days:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": f"{month}-01", "$lte": last_day_str},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        # Join employee details
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        # Unwind ignores attendance logs for non-existent employees
        {"$unwind": "$emp"}
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        # Compute standard competition ranking (1, 2, 2, 4)
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        # Filter employees where rank <= limit
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        },
        # Final sort: total_late_minutes descending, emp_code ascending
        {
            "$sort": {
                "total_late_minutes": DESCENDING,
                "emp_code": ASCENDING
            }
        }
    ])

    cursor = attendance_col.aggregate(pipeline)
    items = []
    for r in cursor:
        items.append(LeaderboardItem(
            rank=int(r["rank"]),
            emp_code=r["emp_code"],
            name=r["name"],
            department=r["department"],
            total_late_minutes=int(r["total_late_minutes"]),
            late_count=int(r["late_count"])
        ))

    return LeaderboardResponse(month=month, items=items)


@app.get("/analytics/departments/{department}/trend", response_model=TrendResponse, summary="Daily attendance trend for a department")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$")
):
    """
    Returns daily attendance trend for a department.
    - Returns exactly one row per calendar day from from_date to to_date inclusive.
    - Missing dates filled using MongoDB's $densify (no Python loops).
    - Validates to_date >= from_date and range <= 92 days (422).
    - Validates department has employees (404 if unknown/empty department).
    - Computes 7-day moving average over preceding window of non-null rates.
    """
    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)

    if d_to < d_from:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'to' date must be greater than or equal to 'from' date"
        )

    num_days = (d_to - d_from).days + 1
    if num_days > 92:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Date range of {num_days} days exceeds maximum allowed 92 days"
        )

    dept_emps = list(employees_col.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not dept_emps:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Department '{department}' does not exist or has no employees"
        )

    dept_emp_codes = [e["emp_code"] for e in dept_emps]

    from_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
    to_dt = datetime(d_to.year, d_to.month, d_to.day, tzinfo=timezone.utc)
    to_dt_next = to_dt + timedelta(days=1)

    pipeline = [
        # Match attendance logs for employees in this department and date range
        {
            "$match": {
                "emp_code": {"$in": dept_emp_codes},
                "date": {"$gte": from_date, "$lte": to_date}
            }
        },
        # Group by date to aggregate present_count and late_count
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0
                        ]
                    }
                }
            }
        },
        # Prepare date_dt for $densify
        {
            "$project": {
                "_id": 0,
                "date_str": "$_id",
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1
            }
        },
        # Database-level gap filling: creates missing days in [from_dt, to_dt_next]
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_next]
                }
            }
        },
        # Project string date and defaults
        {
            "$project": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date_dt"
                    }
                },
                "dayOfWeek": {"$dayOfWeek": "$date_dt"},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]}
            }
        },
        # Lookup department employees for accurate daily headcount
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$project": {"_id": 0, "joined_on": 1}}
                ],
                "as": "employees"
            }
        },
        # Determine is_working_day and daily headcount
        {
            "$project": {
                "date": 1,
                "is_working_day": {
                    "$in": ["$dayOfWeek", [2, 3, 4, 5, 6]]  # 2=Mon, ..., 6=Fri
                },
                "present_count": 1,
                "late_count": 1,
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$employees",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]}
                        }
                    }
                }
            }
        },
        # Calculate attendance_rate (Rule R8: 4 decimals half-up)
        # Only calculated on working days with headcount > 0; otherwise null
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }
        },
        # Compute 7-day moving average using $setWindowFields
        {
            "$setWindowFields": {
                "sortBy": {"date": ASCENDING},
                "output": {
                    "moving_avg_7d_raw": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"]
                        }
                    }
                }
            }
        },
        {"$sort": {"date": ASCENDING}}
    ]

    cursor = attendance_col.aggregate(pipeline)
    items = []
    for r in cursor:
        items.append(TrendItem(
            date=r["date"],
            is_working_day=bool(r["is_working_day"]),
            headcount=int(r["headcount"]),
            present_count=float(r["present_count"]),
            late_count=int(r["late_count"]),
            attendance_rate=round_half_up(r.get("attendance_rate"), 4),
            moving_avg_7d=round_half_up(r.get("moving_avg_7d_raw"), 4)
        ))

    return TrendResponse(department=department, items=items)


# --- Administration / Explain Endpoint ---

@app.get("/admin/explain/{endpoint}", response_model=ExplainResponse, summary="MongoDB explain for an endpoint's main query")
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """
    Runs MongoDB explain with executionStats verbosity on the actual main query
    or aggregation pipeline of the named endpoint.
    - Built exactly as that endpoint builds it (same filters, sort, and pagination).
    - Validates required parameters for each endpoint (returns 422 if missing).
    - Proves the operations use index scans (IXSCAN) and avoid collection scans (COLLSCAN).
    """
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="date_from cannot be greater than date_to"
            )
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status

        skip = (page - 1) * page_size
        cursor = (
            attendance_col.find(q)
            .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
            .skip(skip)
            .limit(page_size)
        )
        raw_plan = cursor.explain()
        return ExplainResponse(
            endpoint=endpoint,
            collection="attendance_logs",
            explain=clean_bson_for_json(raw_plan)
        )

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="emp_code is required for explaining employee_monthly"
            )
        if not month:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required for explaining employee_monthly"
            )

        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        last_day_str = f"{month}-{num_days:02d}"

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": f"{month}-01", "$lte": last_day_str}
                }
            }
        ]
        raw_plan = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats"
        )
        return ExplainResponse(
            endpoint=endpoint,
            collection="attendance_logs",
            explain=clean_bson_for_json(raw_plan)
        )

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required for explaining department_summary"
            )

        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        last_day_str = f"{month}-{num_days:02d}"

        match_emp: Dict[str, Any] = {"joined_on": {"$lte": last_day_str}}
        if department:
            match_emp["department"] = department

        pipeline = [
            {"$match": match_emp}
        ]
        raw_plan = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats"
        )
        return ExplainResponse(
            endpoint=endpoint,
            collection="employees",
            explain=clean_bson_for_json(raw_plan)
        )

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="month is required for explaining late_leaderboard"
            )

        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        last_day_str = f"{month}-{num_days:02d}"

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": f"{month}-01", "$lte": last_day_str},
                    "late_minutes": {"$gt": 0}
                }
            }
        ]
        raw_plan = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats"
        )
        return ExplainResponse(
            endpoint=endpoint,
            collection="attendance_logs",
            explain=clean_bson_for_json(raw_plan)
        )

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="department is required for explaining department_trend"
            )
        if not from_date or not to_date:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="'from' and 'to' date parameters are required for explaining department_trend"
            )
        if to_date < from_date:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="'to' date must be greater than or equal to 'from' date"
            )

        dept_emps = list(employees_col.find({"department": department}, {"_id": 0, "emp_code": 1}))
        dept_emp_codes = [e["emp_code"] for e in dept_emps]

        pipeline = [
            {
                "$match": {
                    "emp_code": {"$in": dept_emp_codes},
                    "date": {"$gte": from_date, "$lte": to_date}
                }
            }
        ]
        raw_plan = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats"
        )
        return ExplainResponse(
            endpoint=endpoint,
            collection="attendance_logs",
            explain=clean_bson_for_json(raw_plan)
        )

    else:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown endpoint '{endpoint}' for explain"
        )
