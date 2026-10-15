# DECISIONS.md

1. **Indexes.** We created `employees.emp_code` (unique) to enforce identity and speed lookups; `employees(department, joined_on)` for department filtering and headcount; `attendance_logs(emp_code, date)` (unique) to prevent duplicate punch-ins and back lookups; `attendance_logs(date, emp_code)` to serve listing and sorting; and `attendance_logs(date, late_minutes, emp_code)` for the leaderboard pipeline. We rejected `(status, date)` because status has low cardinality, making date-led compound indexes far more selective.

2. **Punch-in race.** Both requests derive the IST attendance date and attempt `insert_one`. MongoDB's unique compound index on `(emp_code, date)` atomically accepts the first insert and rejects the second with a `DuplicateKeyError`. Our code catches this exception and returns HTTP 409 ("already punched in for this date"). The database constraint prevents duplicate records without application-level locking.

3. **Ties.** We use MongoDB's `$setWindowFields` with `$rank: {}` for standard competition ranking (1, 2, 2, 4). Because `limit` is applied after ranking via `{"$match": {"rank": {"$lte": limit}}}`, all employees sharing a rank within the limit are returned. The response may exceed `limit` rows when ties occur at the cutoff, sorted by `emp_code` ascending.

4. **Headcount.** Our pipeline starts from `employees` rather than `attendance_logs`, filtering by `joined_on <= month_end`. We then `$lookup` each employee's attendance logs. Because the root stream is employees, individuals with zero logs are naturally preserved and counted in department headcount.

5. **100x Data.** At 10 million records, we would pre-aggregate daily department metrics into a roll-up collection at shift boundaries and shard `attendance_logs` by a hashed compound key on `emp_code` and `date`.
