"""
Tests for Business Rules R1 - R8 boundary conditions:
- R1: Overnight shift assignment to previous day and next-day shift end
- R2: Late minutes grace period (09:40:00 -> 0, 09:40:01 -> 10, 10:15:59 -> 45)
- R3: Overtime threshold (29 min -> 0, 30 min -> 30)
- R4 & R8: Half-up rounding on work hours (e.g. 4.525 -> 4.53)
- R5: Half-day calculation threshold (4.50 -> False, 4.49 -> True)
- R7: Working days calculation from joined_on
"""

from datetime import datetime, timezone, timedelta
import pytest
from app.main import (
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    compute_half_day,
    derive_attendance_date,
    count_working_days,
    IST
)


def test_r2_late_minutes_boundary():
    # Shift start: 09:30 IST on 2026-07-06
    # 09:30:00 IST = 04:00:00 UTC
    dt_base = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)

    # 1. Exactly 10 minutes: 09:40:00 IST -> 0 late minutes
    dt_10m = dt_base + timedelta(minutes=10)
    assert compute_late_minutes(dt_10m, "09:30", "18:30", "2026-07-06") == 0

    # 2. Exactly 10 minutes and 1 second: 09:40:01 IST -> 10 late minutes
    dt_10m_1s = dt_base + timedelta(minutes=10, seconds=1)
    assert compute_late_minutes(dt_10m_1s, "09:30", "18:30", "2026-07-06") == 10

    # 3. 10:15:59 IST (45 minutes and 59 seconds) -> 45 late minutes
    dt_45m_59s = dt_base + timedelta(minutes=45, seconds=59)
    assert compute_late_minutes(dt_45m_59s, "09:30", "18:30", "2026-07-06") == 45

    # 4. Punch in before shift start: 09:20:00 IST -> 0 late minutes
    dt_early = dt_base - timedelta(minutes=10)
    assert compute_late_minutes(dt_early, "09:30", "18:30", "2026-07-06") == 0


def test_r3_overtime_boundary():
    # Shift end: 18:30 IST on 2026-07-06
    # 18:30:00 IST = 13:00:00 UTC
    dt_end_base = datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc)

    # 1. 29 minutes 59 seconds overtime -> under 30-min minimum -> 0
    dt_29m_59s = dt_end_base + timedelta(minutes=29, seconds=59)
    assert compute_overtime(dt_29m_59s, "09:30", "18:30", "2026-07-06") == 0

    # 2. Exactly 30 minutes overtime -> 30
    dt_30m = dt_end_base + timedelta(minutes=30)
    assert compute_overtime(dt_30m, "09:30", "18:30", "2026-07-06") == 30

    # 3. 45 minutes overtime -> 45
    dt_45m = dt_end_base + timedelta(minutes=45)
    assert compute_overtime(dt_45m, "09:30", "18:30", "2026-07-06") == 45


def test_r4_and_r8_work_hours_half_up_rounding():
    dt_start = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)

    # Exactly 4.525 hours = 16290 seconds
    # Standard round() rounds to 4.52 (banker's rounding)
    # Half-up rounding MUST round to 4.53
    dt_4_525 = dt_start + timedelta(seconds=16290)
    assert compute_work_hours(dt_start, dt_4_525) == 4.53

    # Exactly 4.495 hours = 16182 seconds -> rounds to 4.50
    dt_4_495 = dt_start + timedelta(seconds=16182)
    assert compute_work_hours(dt_start, dt_4_495) == 4.50

    # 4.494 hours = 16178.4 -> 16178 seconds -> 4.49388... -> 4.49
    dt_4_494 = dt_start + timedelta(seconds=16178)
    assert compute_work_hours(dt_start, dt_4_494) == 4.49


def test_r5_half_day_boundary():
    # Exactly 4.50 hours: half_day is False
    assert compute_half_day(4.50) is False

    # Strictly less than 4.50 hours: half_day is True
    assert compute_half_day(4.49) is True

    # 3.5 hours: True
    assert compute_half_day(3.5) is True

    # None: False
    assert compute_half_day(None) is False


def test_r1_overnight_shifts():
    # Overnight shift: 22:00 to 06:00
    # 1. Punch-in at 21:55 IST on 2026-07-06 (16:25 UTC) -> date is 2026-07-06
    dt_in_evening = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)
    assert derive_attendance_date(dt_in_evening, "22:00", "06:00") == "2026-07-06"

    # 2. Punch-in at 01:15 IST on 2026-07-07 (19:45 UTC on July 6)
    # Earlier than shift_end (06:00) -> belongs to previous calendar day (2026-07-06)
    dt_in_night = datetime(2026, 7, 6, 19, 45, 0, tzinfo=timezone.utc)
    assert derive_attendance_date(dt_in_night, "22:00", "06:00") == "2026-07-06"

    # 3. Overnight shift overtime:
    # Shift 22:00 to 06:00 on 2026-07-06. Shift end is 2026-07-07 06:00 IST (00:30 UTC on July 7)
    # Punch out at 2026-07-07 06:40 IST (01:10 UTC on July 7) -> 40 minutes overtime
    dt_out = datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc)
    assert compute_overtime(dt_out, "22:00", "06:00", "2026-07-06") == 40


def test_r7_working_days_calculation():
    # July 2026 has 31 days.
    # 2026-07-01 is Wednesday. July has 23 weekdays (Mon-Fri).
    # Employee joined before July:
    assert count_working_days("2026-01-05", "2026-07") == 23

    # Employee joined mid-month on 2026-07-13 (Monday):
    # Weekdays from 2026-07-13 to 2026-07-31:
    # Total weekdays = 23 - (1st to 10th weekdays = 8 weekdays) = 15 weekdays
    assert count_working_days("2026-07-13", "2026-07") == 15

    # Employee joined after the month on 2026-08-01:
    assert count_working_days("2026-08-01", "2026-07") == 0
