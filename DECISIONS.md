# DECISIONS.md

1. **Indexes.** On logs: unique `(emp_code, date)` stops double punch-ins and serves per-employee queries; `(date desc, emp_code)` gives the list sort order and month scans; `(status, date desc, emp_code)` serves the status filter; `(emp_code, punch_in desc)` finds the record to punch out. On employees: unique `emp_code`, `(department, emp_code)`, `(department, joined_on)` and `(joined_on)`. I rejected a `status`-only index because it is not selective enough.

2. **Punch-in race.** Both requests work out the same date and call `insert_one`. The unique index lets exactly one succeed; the other gets `DuplicateKeyError`, which I return as 409. I never check the logs collection first, so there is no gap for a second request to slip through.

3. **Ties.** The leaderboard uses `$rank` (1, 2, 2, 4) and filters `rank <= limit` after ranking, so everyone tied at the cutoff is returned, even if that is more than `limit` rows.

4. **Headcount.** The pipeline starts on `employees` (joined on or before month end), then `$lookup`s their logs. People with no logs keep an empty array and still add 1 to headcount.

5. **100x data.** I would pre-aggregate monthly and daily numbers into a summary collection, updated on every punch or correction, so analytics read small documents instead of scanning logs.