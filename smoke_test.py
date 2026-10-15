"""Optional end-to-end check. Start the API first, then:  python smoke_test.py
Uses only the standard library. Creates its own employees (EMP9001..), so run it on a test database."""
import json
import urllib.error
import urllib.request

BASE = "http://localhost:8000"


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def show(title, result):
    print(f"\n== {title}\n{result[0]}  {json.dumps(result[1], indent=1)[:700]}")


# 2026-07-06 is a Monday. IST = UTC+5:30, so 09:40:01 IST = 04:10:01 UTC.
import datetime as dt
def ms(y, mo, d, h, mi, s=0):  # IST wall clock -> epoch ms
    return int((dt.datetime(y, mo, d, h, mi, s, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))).timestamp() * 1000)

show("health", call("GET", "/health"))
for code, dept, ss, se in [("EMP9001", "QA", "09:30", "18:30"), ("EMP9002", "QA", "09:30", "18:30"),
                           ("EMP9003", "QA", "22:00", "06:00")]:
    show(f"create {code}", call("POST", "/employees", {"emp_code": code, "name": code, "email": f"{code}@x.com",
                                                      "department": dept, "shift_start": ss, "shift_end": se,
                                                      "joined_on": "2026-07-01"}))
show("duplicate employee -> 409", call("POST", "/employees", {"emp_code": "EMP9001", "name": "x", "email": "a@b.co",
                                                              "department": "QA", "joined_on": "2026-07-01"}))
show("punch-in 09:40:01 (late 10)", call("POST", "/attendance/punch-in", {"emp_code": "EMP9001", "punched_at": ms(2026, 7, 6, 9, 40, 1)}))
show("same punch-in again -> 409", call("POST", "/attendance/punch-in", {"emp_code": "EMP9001", "punched_at": ms(2026, 7, 6, 9, 41)}))
show("punch-out 19:10 (overtime 40)", call("POST", "/attendance/punch-out", {"emp_code": "EMP9001", "punched_at": ms(2026, 7, 6, 19, 10)}))
show("punch-out again -> 409", call("POST", "/attendance/punch-out", {"emp_code": "EMP9001", "punched_at": ms(2026, 7, 6, 19, 20)}))
show("seconds instead of ms -> 422", call("POST", "/attendance/punch-in", {"emp_code": "EMP9002", "punched_at": 1783312500}))
show("overnight punch-in 21:55", call("POST", "/attendance/punch-in", {"emp_code": "EMP9003", "punched_at": ms(2026, 7, 6, 21, 55)}))
show("overnight punch-out 06:40 next day", call("POST", "/attendance/punch-out", {"emp_code": "EMP9003", "punched_at": ms(2026, 7, 7, 6, 40)}))
show("regularize", call("PATCH", "/attendance/EMP9001/2026-07-06", {"punch_in": ms(2026, 7, 6, 9, 28), "reason": "biometric glitch", "regularized_by": "hr.admin"}))
show("regularize nothing -> 422", call("PATCH", "/attendance/EMP9001/2026-07-06", {"punch_in": ms(2026, 7, 6, 9, 28), "reason": "again again", "regularized_by": "hr.admin"}))
show("list attendance", call("GET", "/attendance?emp_code=EMP9001"))
show("employee monthly", call("GET", "/analytics/employees/EMP9001/monthly?month=2026-07"))
show("department summary", call("GET", "/analytics/departments/summary?month=2026-07&department=QA"))
show("late leaderboard", call("GET", "/analytics/leaderboard/late?month=2026-07"))
show("department trend", call("GET", "/analytics/departments/QA/trend?from=2026-07-01&to=2026-07-09"))
for ep, qs in [("attendance_list", "emp_code=EMP9001"), ("employee_monthly", "emp_code=EMP9001&month=2026-07"),
               ("department_summary", "month=2026-07"), ("late_leaderboard", "month=2026-07"),
               ("department_trend", "department=QA&from=2026-07-01&to=2026-07-09")]:
    s, body = call("GET", f"/admin/explain/{ep}?{qs}")
    text = json.dumps(body)
    print(f"\n== explain {ep}: status {s}, IXSCAN={'IXSCAN' in text}, COLLSCAN={'COLLSCAN' in text}")
