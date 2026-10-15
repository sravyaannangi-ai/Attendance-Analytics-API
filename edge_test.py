"""Edge-case test for the attendance API (optional, NOT part of the submission).

Start the API first, then in a second terminal:  python edge_test.py
It writes test data (EMP8001..EMP8010, EMP8999, March 2026) into the same database the API uses and
deletes its own previous test data first, so you can run it as often as you like.
Uses the MONGO_URI / MONGO_DB environment variables (defaults: localhost / attendance_db)."""
import datetime as dt
import json
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from pymongo import MongoClient

BASE = "http://localhost:8000"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
db = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)[os.getenv("MONGO_DB", "attendance_db")]
passed = failed = 0


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method, headers={"Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def ms(y, mo, d, h=9, mi=30, s=0, milli=0):
    """IST wall-clock time -> epoch milliseconds."""
    return int(dt.datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp()) * 1000 + milli


def at(y, mo, d, h=9, mi=30):
    return dt.datetime(y, mo, d, h, mi, tzinfo=IST).astimezone(dt.timezone.utc)


def check(name, got, want):
    global passed, failed
    ok = got == want
    passed, failed = passed + ok, failed + (not ok)
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"\n      got  {got}\n      want {want}"))


# ------------------------------------------------------------------ seed data (March 2026) -----------
CODES = [f"EMP80{n:02d}" for n in range(1, 11)] + ["EMP8999"]
db.employees.delete_many({"emp_code": {"$in": CODES}})
db.attendance_logs.delete_many({"emp_code": {"$in": CODES}})


def employee(code, dept, joined):
    return {"emp_code": code, "name": code, "email": f"{code}@x.com", "department": dept, "shift_start": "09:30",
            "shift_end": "18:30", "joined_on": joined, "created_at": dt.datetime.now(dt.timezone.utc)}


def log(code, day, status, late=0, ot=0, wh=None, half=False):
    date = f"2026-03-{day:02d}"
    present = status in ("PRESENT", "WFH", "ON_DUTY")
    return {"emp_code": code, "date": date, "status": status, "punch_in": at(2026, 3, day) if present else None,
            "punch_out": at(2026, 3, day, 18, 30) if present and wh else None, "work_hours": wh,
            "late_minutes": late, "overtime_minutes": ot, "half_day": half, "history": []}


db.employees.insert_many([employee("EMP8001", "EDGE", "2026-01-05"), employee("EMP8002", "EDGE", "2026-01-05"),
                          employee("EMP8003", "EDGE", "2026-03-16"), employee("EMP8004", "EDGE", "2026-01-05"),
                          employee("EMP8005", "EDGE", "2026-04-01"), employee("EMP8006", "EDGE2", "2026-01-05")])
legacy = log("EMP8004", 2, "PRESENT", late=45, wh=8.5)          # legacy document: no half_day/history/overtime
for field in ("half_day", "history", "overtime_minutes"):
    legacy.pop(field)
db.attendance_logs.insert_many([
    log("EMP8001", 2, "PRESENT", late=30, wh=9.0),              # Mon, late
    log("EMP8001", 3, "WFH", wh=4.0, half=True),                # Tue, half day = 0.5
    log("EMP8001", 4, "LEAVE"),                                 # Wed
    log("EMP8001", 5, "ON_DUTY", wh=8.0),                       # Thu
    log("EMP8001", 6, "ABSENT"),                                # Fri (a real ABSENT record)
    log("EMP8001", 7, "PRESENT", late=15, ot=60, wh=8.0),       # SATURDAY: no present day, but late/overtime count
    legacy,                                                     # EMP8004 ties with EMP8001 on 45 late minutes
    log("EMP8003", 16, "PRESENT", late=12, wh=9.0),             # mid-month joiner
    log("EMP8003", 17, "PRESENT", wh=9.0),
    log("EMP8006", 2, "PRESENT", late=100, wh=8.0),             # other department, biggest late total
    log("EMP8999", 2, "PRESENT", late=500, wh=8.0),             # orphan log: employee does not exist
])

# ------------------------------------------------------------------ analytics -----------------------
print("\n--- employee monthly ---")
s, b = call("GET", "/analytics/employees/EMP8001/monthly?month=2026-03")
check("EMP8001 (weekend, half day, leave, on duty)", b, {
    "emp_code": "EMP8001", "month": "2026-03", "working_days": 22, "present_days": 2.5, "leave_days": 1,
    "late_count": 2, "total_late_minutes": 45, "total_overtime_minutes": 60, "attendance_pct": 11.36})
s, b = call("GET", "/analytics/employees/EMP8002/monthly?month=2026-03")
check("EMP8002 (no logs at all): zeros, pct 0.0 not null", (b["present_days"], b["working_days"], b["attendance_pct"]), (0.0, 22, 0.0))
s, b = call("GET", "/analytics/employees/EMP8003/monthly?month=2026-03")
check("EMP8003 (joined Mar 16): 12 working days", (b["working_days"], b["present_days"], b["attendance_pct"]), (12, 2.0, 16.67))
s, b = call("GET", "/analytics/employees/EMP8005/monthly?month=2026-03")
check("EMP8005 (joins in April): working_days 0, pct null", (b["working_days"], b["attendance_pct"]), (0, None))
check("unknown employee -> 404", call("GET", "/analytics/employees/EMP0000/monthly?month=2026-03")[0], 404)
check("bad month -> 422", call("GET", "/analytics/employees/EMP8001/monthly?month=2026-13")[0], 422)

print("\n--- department summary ---")
s, b = call("GET", "/analytics/departments/summary?month=2026-03&department=EDGE")
check("EDGE: headcount 4 (no-log employee counted, April joiner not), average over records", b["items"], [{
    "department": "EDGE", "headcount": 4, "present_days": 5.5, "avg_work_hours": 7.93, "late_count": 4,
    "total_late_minutes": 102, "leave_count": 1, "on_duty_count": 1}])
s, b = call("GET", "/analytics/departments/summary?month=2026-03&department=EDGE2")
check("EDGE2", b["items"], [{"department": "EDGE2", "headcount": 1, "present_days": 1.0, "avg_work_hours": 8.0,
                             "late_count": 1, "total_late_minutes": 100, "leave_count": 0, "on_duty_count": 0}])
s, b = call("GET", "/analytics/departments/summary?month=2026-03")
names = [i["department"] for i in b["items"] if i["department"] in ("EDGE", "EDGE2")]
check("unfiltered list is sorted by department", names, ["EDGE", "EDGE2"])

print("\n--- late leaderboard (tie: EMP8001 and EMP8004 both 45) ---")
rows = lambda b: [(i["rank"], i["emp_code"], i["total_late_minutes"]) for i in b["items"]]
s, b = call("GET", "/analytics/leaderboard/late?month=2026-03&limit=10")
check("ranks 1,2,2,4; orphan EMP8999 ignored", rows(b), [(1, "EMP8006", 100), (2, "EMP8001", 45), (2, "EMP8004", 45), (4, "EMP8003", 12)])
s, b = call("GET", "/analytics/leaderboard/late?month=2026-03&limit=2")
check("limit=2 cuts AFTER ranking: tied rank-2 rows both returned (3 rows)", rows(b), [(1, "EMP8006", 100), (2, "EMP8001", 45), (2, "EMP8004", 45)])
s, b = call("GET", "/analytics/leaderboard/late?month=2026-03&limit=3")
check("limit=3: rank 4 is excluded", len(b["items"]), 3)
s, b = call("GET", "/analytics/leaderboard/late?month=2026-03&department=EDGE")
check("ranking inside one department: 1,1,3", rows(b), [(1, "EMP8001", 45), (1, "EMP8004", 45), (3, "EMP8003", 12)])
check("limit=0 -> 422", call("GET", "/analytics/leaderboard/late?month=2026-03&limit=0")[0], 422)

print("\n--- department trend (Mar 1 = Sunday) ---")
s, b = call("GET", "/analytics/departments/EDGE/trend?from=2026-03-01&to=2026-03-09")
got = [(r["date"][-2:], r["is_working_day"], r["headcount"], r["present_count"], r["late_count"], r["attendance_rate"], r["moving_avg_7d"])
       for r in b["items"]]
check("exactly 9 rows, weekends and empty days included", len(got), 9)
check("rows", got, [
    ("01", False, 3, 0.0, 0, None, None), ("02", True, 3, 2.0, 2, 0.6667, 0.6667),
    ("03", True, 3, 0.5, 0, 0.1667, 0.4167), ("04", True, 3, 0.0, 0, 0.0, 0.2778),
    ("05", True, 3, 1.0, 0, 0.3333, 0.2917), ("06", True, 3, 0.0, 0, 0.0, 0.2333),
    ("07", False, 3, 1.0, 1, None, 0.2333), ("08", False, 3, 0.0, 0, None, 0.2333),
    ("09", True, 3, 0.0, 0, 0.0, 0.1)])
check("unknown department -> 404", call("GET", "/analytics/departments/NOPE/trend?from=2026-03-01&to=2026-03-09")[0], 404)
check("to before from -> 422", call("GET", "/analytics/departments/EDGE/trend?from=2026-03-09&to=2026-03-01")[0], 422)
check("range of 93 days -> 422", call("GET", "/analytics/departments/EDGE/trend?from=2026-01-01&to=2026-04-03")[0], 422)

# ------------------------------------------------------------------ rules through the API ------------
print("\n--- R1/R2 through punch-in / punch-out ---")
call("POST", "/employees", {"emp_code": "EMP8010", "name": "R2 Test", "email": "r2@x.com", "department": "EDGE3", "joined_on": "2026-01-05"})
for day, (h, mi, sec, milli), want in [(10, (9, 40, 0, 900), 0), (11, (9, 40, 1, 0), 10), (12, (10, 15, 59, 0), 45)]:
    s, b = call("POST", "/attendance/punch-in", {"emp_code": "EMP8010", "punched_at": ms(2026, 3, day, h, mi, sec, milli)})
    check(f"punch-in {h}:{mi}:{sec:02d} -> late {want}", (s, b.get("late_minutes")), (201, want))
    if milli:
        check("   instant truncated to whole seconds", b["punch_in"], ms(2026, 3, day, h, mi, sec))

call("POST", "/employees", {"emp_code": "EMP8008", "name": "Night", "email": "n@x.com", "department": "EDGE3",
                            "shift_start": "22:00", "shift_end": "06:00", "joined_on": "2026-01-05"})
s, b = call("POST", "/attendance/punch-in", {"emp_code": "EMP8008", "punched_at": ms(2026, 3, 10, 5, 0)})
check("overnight: 05:00 punch-in belongs to the PREVIOUS day", (b["date"], b["late_minutes"]), ("2026-03-09", 420))
s, b = call("POST", "/attendance/punch-out", {"emp_code": "EMP8008", "punched_at": ms(2026, 3, 10, 6, 30)})
check("overnight punch-out: 1.5 h, overtime 30, half day", (s, b["work_hours"], b["overtime_minutes"], b["half_day"]), (200, 1.5, 30, True))

call("POST", "/employees", {"emp_code": "EMP8009", "name": "None", "email": "z@x.com", "department": "EDGE3", "joined_on": "2026-01-05"})
check("punch-out with no punch-in -> 404", call("POST", "/attendance/punch-out", {"emp_code": "EMP8009", "punched_at": ms(2026, 3, 10)})[0], 404)
check("punch-out unknown employee -> 404", call("POST", "/attendance/punch-out", {"emp_code": "EMP0000"})[0], 404)
check("punch-in unknown employee -> 404", call("POST", "/attendance/punch-in", {"emp_code": "EMP0000"})[0], 404)
check("punch-out more than 24 h later -> 422", call("POST", "/attendance/punch-out", {"emp_code": "EMP8010", "punched_at": ms(2026, 3, 12, 10, 15, 59) + 86_401_001})[0], 422)
check("punch-in status ABSENT -> 422", call("POST", "/attendance/punch-in", {"emp_code": "EMP8009", "status": "ABSENT"})[0], 422)
check("punched_at as float -> 422", call("POST", "/attendance/punch-in", {"emp_code": "EMP8009", "punched_at": 1783312500000.5})[0], 422)

print("\n--- regularize rules ---")
url = "/attendance/EMP8010/2026-03-10"
body = lambda **k: {"reason": "test reason", "regularized_by": "hr", **k}
check("ABSENT together with punch_in -> 422", call("PATCH", url, body(status="ABSENT", punch_in=ms(2026, 3, 10, 9, 0)))[0], 422)
check("punch_in on another date -> 422", call("PATCH", url, body(punch_in=ms(2026, 3, 11, 9, 0)))[0], 422)
check("punch_out before punch_in -> 422", call("PATCH", url, body(punch_out=ms(2026, 3, 10, 8, 0)))[0], 422)
check("no record for that date -> 404", call("PATCH", "/attendance/EMP8010/2026-03-30", body(status="LEAVE"))[0], 404)
check("derived field in body is ignored (still 'changes nothing')", call("PATCH", url, body(work_hours=99))[0], 422)
s, b = call("PATCH", url, body(status="ABSENT"))
check("-> ABSENT clears punch times; history lists only what changed",
      (s, b["punch_in"], b["work_hours"], set(b["history"][0]["changes"])), (200, None, None, {"status", "punch_in"}))

print("\n--- legacy record, listing, pagination ---")
s, b = call("GET", "/attendance?emp_code=EMP8004")
check("legacy document (no half_day/history) is returned with defaults",
      (b["items"][0]["half_day"], b["items"][0]["history"], b["items"][0]["overtime_minutes"]), (False, [], 0))
s, b = call("GET", "/attendance?emp_code=EMP8001&status=WFH")
check("status filter + total reflects filter", (b["total"], len(b["items"])), (1, 1))
check("date_from after date_to -> 422", call("GET", "/attendance?date_from=2026-03-09&date_to=2026-03-01")[0], 422)
check("page=0 -> 422", call("GET", "/attendance?page=0")[0], 422)
check("page_size=101 -> 422", call("GET", "/attendance?page_size=101")[0], 422)
s, b = call("GET", "/employees?department=EDGE&page=2&page_size=2")
check("employees page 2 (sorted by emp_code, total of filter)", ([e["emp_code"] for e in b["items"]], b["total"]), (["EMP8003", "EMP8004"], 5))
s, b = call("GET", "/attendance?emp_code=EMP8001&page_size=3")
check("list is sorted by date descending", [i["date"] for i in b["items"]], ["2026-03-07", "2026-03-06", "2026-03-05"])
check("explain without required params -> 422", call("GET", "/admin/explain/employee_monthly")[0], 422)

# ------------------------------------------------------------------ concurrency ---------------------
print("\n--- concurrency ---")
call("POST", "/employees", {"emp_code": "EMP8007", "name": "Race", "email": "r@x.com", "department": "EDGE3", "joined_on": "2026-01-05"})
codes = lambda results: sorted(r[0] for r in results)
with ThreadPoolExecutor(10) as pool:
    res = list(pool.map(lambda _: call("POST", "/attendance/punch-in", {"emp_code": "EMP8007", "punched_at": ms(2026, 3, 10, 9, 0)}), range(10)))
check("10 parallel punch-ins -> one 201, nine 409", codes(res), [201] + [409] * 9)
with ThreadPoolExecutor(10) as pool:
    res = list(pool.map(lambda _: call("POST", "/attendance/punch-out", {"emp_code": "EMP8007", "punched_at": ms(2026, 3, 10, 18, 0)}), range(10)))
check("10 parallel punch-outs -> one 200, nine 409", codes(res), [200] + [409] * 9)
with ThreadPoolExecutor(6) as pool:
    res = list(pool.map(lambda n: call("PATCH", "/attendance/EMP8007/2026-03-10", {
        "punch_out": ms(2026, 3, 10, 18, 1 + n), "reason": f"parallel edit {n}", "regularized_by": "hr"}), range(6)))
ok = sum(1 for r in res if r[0] == 200)
s, b = call("GET", "/attendance?emp_code=EMP8007")
check("6 parallel corrections: only 200/409, and no history entry is lost",
      (all(r[0] in (200, 409) for r in res), ok >= 1, len(b["items"][0]["history"]) == ok), (True, True, True))

print(f"\n{passed} passed, {failed} failed")
