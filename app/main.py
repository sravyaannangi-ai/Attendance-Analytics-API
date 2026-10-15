"""
Employee Attendance & Analytics API (FastAPI + MongoDB).  All application code lives in this one file.

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (real environment variables win over a local .env file)

Layout of this file
  1. setup            - Mongo client, indexes created at startup
  2. pure helpers     - time zone, R1-R5 calculations, rounding (no database, easy to unit test)
  3. request models   - pydantic validation (422)
  4. employees / attendance endpoints
  5. analytics endpoints (aggregation pipelines; builders are shared with the explain endpoint)
  6. admin explain endpoint
"""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # python-dotenv never overrides variables that are already set in the environment

# --------------------------------------------------------------------------- #
# 1. Setup
# --------------------------------------------------------------------------- #
# tz_aware=True: datetimes read from MongoDB are timezone-aware (UTC), never naive.
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True,
                     serverSelectionTimeoutMS=5000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db["employees"]
logs = db["attendance_logs"]

_indexes_ready = False


def ensure_indexes():
    """Idempotent: creating an index that already exists is a no-op."""
    global _indexes_ready
    specs = [
        # employees
        (employees, [("emp_code", ASCENDING)], {"unique": True, "name": "emp_code_unique"}),
        (employees, [("department", ASCENDING), ("emp_code", ASCENDING)], {"name": "dept_emp_code"}),
        (employees, [("department", ASCENDING), ("joined_on", ASCENDING)], {"name": "dept_joined_on"}),
        (employees, [("joined_on", ASCENDING)], {"name": "joined_on"}),
        # attendance_logs
        (logs, [("emp_code", ASCENDING), ("date", ASCENDING)], {"unique": True, "name": "emp_date_unique"}),
        (logs, [("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "date_desc_emp"}),
        (logs, [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], {"name": "status_date_emp"}),
        (logs, [("emp_code", ASCENDING), ("punch_in", DESCENDING)], {"name": "emp_punch_in"}),
    ]
    for coll, keys, opts in specs:
        coll.create_index(keys, **opts)
    _indexes_ready = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # keep the app up; /health will retry and report 503 until Mongo answers
        print(f"WARNING: could not create indexes at startup: {exc}")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)

# --------------------------------------------------------------------------- #
# 2. Pure helpers (no database access)
# --------------------------------------------------------------------------- #
# --- helpers:begin ---
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")      # R6
GRACE_SECONDS = 600                            # R2: late only if STRICTLY more than 10:00 after shift start
MIN_OVERTIME_MINUTES = 30                      # R3


def aware(dt):
    """Treat a naive datetime as UTC (defensive; the client already returns aware ones)."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def to_ms(dt):
    """datetime -> integer epoch milliseconds (integer maths, no float rounding)."""
    if dt is None:
        return None
    return (aware(dt) - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms):
    return EPOCH + timedelta(milliseconds=ms)


def instant_from_ms(ms):
    """API instant -> datetime truncated to whole seconds (R1)."""
    return from_ms(ms).replace(microsecond=0)


def now_seconds():
    return datetime.now(UTC).replace(microsecond=0)


def half_up(value, places):
    """Round half-up (R4, R8). Python's round() is banker's rounding, so Decimal is used."""
    return Decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def valid_date(text):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text or ""):
        raise ValueError("expected YYYY-MM-DD")
    return Date.fromisoformat(text)          # raises ValueError for 2026-02-30 etc.


def hm(text):
    h, m = text.split(":")
    return int(h), int(m)


def is_overnight(emp):
    return hm(emp["shift_end"]) <= hm(emp["shift_start"])


def attendance_date(emp, punch_in):
    """R1: IST calendar date of the punch-in; overnight shifts: an early-morning punch-in belongs to yesterday."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(emp):
        end_h, end_m = hm(emp["shift_end"])
        if (local.hour, local.minute, local.second) < (end_h, end_m, 0):
            day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(emp, date_str):
    """(shift start, shift end) as aware datetimes for the attendance date; overnight shifts end the next day."""
    day = Date.fromisoformat(date_str)
    sh, sm = hm(emp["shift_start"])
    eh, em = hm(emp["shift_end"])
    start = datetime(day.year, day.month, day.day, sh, sm, tzinfo=IST)
    end = datetime(day.year, day.month, day.day, eh, em, tzinfo=IST)
    if is_overnight(emp):
        end += timedelta(days=1)
    return start, end


def late_minutes(punch_in, shift_start_dt):
    """R2: 0 up to and including 10:00 after shift start, otherwise whole minutes since shift start."""
    secs = int((punch_in - shift_start_dt).total_seconds())
    return secs // 60 if secs > GRACE_SECONDS else 0


def overtime_minutes(punch_out, shift_end_dt):
    """R3: whole minutes after shift end, only if at least 30."""
    mins = int((punch_out - shift_end_dt).total_seconds()) // 60
    return mins if mins >= MIN_OVERTIME_MINUTES else 0


def derive(emp, status, date_str, punch_in, punch_out):
    """All derived fields for a record (see the table in DATA_MODEL.md section 3)."""
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE or punch_in is None:
        return out
    start, end = shift_bounds(emp, date_str)
    out["late_minutes"] = late_minutes(punch_in, start)
    if punch_out is not None:
        hours = half_up(Decimal(int((punch_out - punch_in).total_seconds())) / Decimal(3600), 2)   # R4
        out["work_hours"] = float(hours)
        out["overtime_minutes"] = overtime_minutes(punch_out, end)                                 # R3
        out["half_day"] = hours < Decimal("4.50")                                                  # R5 (rounded value)
    return out


MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"


def month_bounds(month):
    year, mon = int(month[:4]), int(month[5:7])
    return f"{month}-01", f"{month}-{calendar.monthrange(year, mon)[1]:02d}"


def working_days(month, joined_on):
    """R7: Mon-Fri days of the month, counted from joined_on for mid-month joiners."""
    first, last = month_bounds(month)
    start = max(first, joined_on)
    if start > last:
        return 0
    d0, d1 = Date.fromisoformat(start), Date.fromisoformat(last)
    return sum(1 for i in range((d1 - d0).days + 1) if (d0 + timedelta(days=i)).weekday() < 5)
# --- helpers:end ---


def unprocessable(loc, msg):
    """422 with the same body shape FastAPI uses for its own validation errors."""
    return HTTPException(422, detail=[{"loc": list(loc), "msg": msg, "type": "value_error"}])


def parse_date_param(value, name, where="query"):
    try:
        return valid_date(value).isoformat()
    except ValueError:
        raise unprocessable([where, name], f"{name} must be a valid date in YYYY-MM-DD format")


# --------------------------------------------------------------------------- #
# 3. Request models
# --------------------------------------------------------------------------- #
Epoch = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]   # epoch milliseconds only
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
HM_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
    shift_start: str = Field("09:30", pattern=HM_PATTERN)
    shift_end: str = Field("18:30", pattern=HM_PATTERN)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on_is_a_date(cls, v):
        return valid_date(v).isoformat()

    @model_validator(mode="after")
    def _shift_times_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[Epoch] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[Epoch] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[Epoch] = None
    punch_out: Optional[Epoch] = None
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=200)]
    regularized_by: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]

    @model_validator(mode="after")
    def _no_explicit_nulls(self):
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} must not be null; omit it to leave it unchanged")
        return self


# --------------------------------------------------------------------------- #
# Output shaping (MongoDB document -> API JSON)
# --------------------------------------------------------------------------- #
def employee_out(d):
    return {"emp_code": d["emp_code"], "name": d["name"], "email": d["email"], "department": d["department"],
            "shift_start": d["shift_start"], "shift_end": d["shift_end"], "joined_on": d["joined_on"],
            "created_at": to_ms(d.get("created_at"))}


def _history_value(key, value):
    return to_ms(value) if key in ("punch_in", "punch_out") and isinstance(value, datetime) else value


def record_out(d):
    """Missing legacy fields become defaults; datetimes become epoch milliseconds; _id never leaves."""
    history = []
    for h in d.get("history") or []:
        changes = {k: {"from": _history_value(k, v.get("from")), "to": _history_value(k, v.get("to"))}
                   for k, v in (h.get("changes") or {}).items()}
        history.append({"at": to_ms(h.get("at")), "by": h.get("by"), "reason": h.get("reason"), "changes": changes})
    return {"emp_code": d["emp_code"], "date": d["date"], "status": d["status"],
            "punch_in": to_ms(d.get("punch_in")), "punch_out": to_ms(d.get("punch_out")),
            "work_hours": d.get("work_hours"), "late_minutes": d.get("late_minutes") or 0,
            "overtime_minutes": d.get("overtime_minutes") or 0, "half_day": bool(d.get("half_day", False)),
            "history": history}


def get_employee_or_404(emp_code):
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 4. Health, employees, attendance
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "MongoDB is not available")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = from_ms(to_ms(datetime.now(UTC)))          # millisecond precision, like MongoDB stores it
    try:
        employees.insert_one(doc)                                  # unique index decides, no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None,
                   page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department else {}
    total = employees.count_documents(q)
    cursor = employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = instant_from_ms(body.punched_at) if body.punched_at is not None else now_seconds()
    date_str = attendance_date(emp, ts)
    start, _ = shift_bounds(emp, date_str)
    doc = {"emp_code": body.emp_code, "date": date_str, "status": body.status, "punch_in": ts, "punch_out": None,
           "work_hours": None, "late_minutes": late_minutes(ts, start), "overtime_minutes": 0,
           "half_day": False, "history": []}
    try:
        logs.insert_one(doc)           # unique (emp_code, date) index: exactly one concurrent request wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = instant_from_ms(body.punched_at) if body.punched_at is not None else now_seconds()
    # most recent record whose punch_in is at or before the punch-out (works across midnight)
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = aware(rec["punch_in"])
    if ts <= p_in:
        raise unprocessable(["body", "punched_at"], "punched_at must be after the punch-in")
    if ts - p_in > timedelta(hours=24):
        raise unprocessable(["body", "punched_at"], "punched_at is more than 24 hours after the punch-in")
    d = derive(emp, rec["status"], rec["date"], p_in, ts)
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},      # only succeeds while the record is still open
        {"$set": {"punch_out": ts, "work_hours": d["work_hours"],
                  "overtime_minutes": d["overtime_minutes"], "half_day": d["half_day"]}},
        return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


def build_attendance_query(emp_code, date_from, date_to, status):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    rng = {}
    if date_from:
        rng["$gte"] = date_from
    if date_to:
        rng["$lte"] = date_to
    if rng:
        q["date"] = rng
    if status:
        q["status"] = status
    return q


ATTENDANCE_SORT = {"date": -1, "emp_code": 1}


def check_attendance_filters(date_from, date_to):
    if date_from:
        date_from = parse_date_param(date_from, "date_from")
    if date_to:
        date_to = parse_date_param(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise unprocessable(["query", "date_from"], "date_from must not be after date_to")
    return date_from, date_to


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None,
                    date_to: Optional[str] = None, status: Optional[Status] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    date_from, date_to = check_attendance_filters(date_from, date_to)
    q = build_attendance_query(emp_code, date_from, date_to, status)
    total = logs.count_documents(q) if q else logs.estimated_document_count()
    cursor = (logs.find(q).sort(list(ATTENDANCE_SORT.items()))
              .skip((page - 1) * page_size).limit(page_size))
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    date = parse_date_param(date, "date", "path")
    emp = get_employee_or_404(emp_code)
    rec = logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    req_in = instant_from_ms(body.punch_in) if body.punch_in is not None else None
    req_out = instant_from_ms(body.punch_out) if body.punch_out is not None else None
    old_in, old_out = aware(rec.get("punch_in")), aware(rec.get("punch_out"))
    new_status = body.status or rec["status"]

    if new_status not in PRESENCE:                       # ABSENT / LEAVE clear both punch times
        if req_in is not None or req_out is not None:
            raise unprocessable(["body"], f"punch_in / punch_out cannot be set with status {new_status}")
        new_in = new_out = None
    else:
        new_in = req_in or old_in
        if new_in is None:
            raise unprocessable(["body", "punch_in"], f"status {new_status} requires a punch_in")
        new_out = req_out or old_out
        if req_in is not None and attendance_date(emp, new_in) != rec["date"]:
            raise unprocessable(["body", "punch_in"], f"punch_in must stay on the attendance date {rec['date']}")
        if new_out is not None and (new_out <= new_in or new_out - new_in > timedelta(hours=24)):
            raise unprocessable(["body", "punch_out"], "punch_out must be after punch_in and within 24 hours")

    if new_status == rec["status"] and new_in == old_in and new_out == old_out:
        raise unprocessable(["body"], "the request changes nothing")

    new_vals = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
                **derive(emp, new_status, rec["date"], new_in, new_out)}
    old_vals = {"status": rec["status"], "punch_in": old_in, "punch_out": old_out,
                "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
                "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False))}
    changes = {k: {"from": old_vals[k], "to": v} for k, v in new_vals.items() if old_vals[k] != v}
    entry = {"at": from_ms(to_ms(datetime.now(UTC))), "by": body.regularized_by,
             "reason": body.reason, "changes": changes}

    # Optimistic concurrency: apply only if the record is exactly as we read it (same history length,
    # status and punch times). A concurrent edit makes this match nothing -> 409, so no history is lost.
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "status": rec["status"], "punch_in": rec.get("punch_in"),
         "punch_out": rec.get("punch_out"),
         "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(rec.get("history") or [])]}},
        {"$set": new_vals, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "the record was modified concurrently; reload and retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# 5. Analytics (aggregation pipelines)
# --------------------------------------------------------------------------- #
# Reusable per-log expressions. They READ the stored derived fields (late_minutes, half_day, ...) and never
# recompute them. "p2" = present days x 2, so a half day (0.5) stays an exact integer (1) while a full day is 2.
_DOW = {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}   # 1=Sun ... 7=Sat
_IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}
_HALVES = {"$cond": [_IS_PRESENT, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 1, 2]}, 0]}
_LATE = {"$ifNull": ["$late_minutes", 0]}
_HAS_HOURS = {"$and": [_IS_PRESENT, {"$isNumber": "$work_hours"}]}
METRICS = {
    "p2": {"$cond": [{"$and": [{"$gte": [_DOW, 2]}, {"$lte": [_DOW, 6]}]}, _HALVES, 0]},   # R7: Mon-Fri only
    "late": _LATE,
    "late_flag": {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]},
    "overtime": {"$ifNull": ["$overtime_minutes", 0]},
    "leave": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]},
    "on_duty": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]},
    "wh_cents": {"$cond": [_HAS_HOURS, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]},
    "wh_n": {"$cond": [_HAS_HOURS, 1, 0]},
}


def num(x):
    return Decimal(str(x or 0))


def build_monthly_pipeline(emp_code, month):
    first, last = month_bounds(month)
    return [{"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
            {"$group": {"_id": None, **{k: {"$sum": v} for k, v in METRICS.items()}}}]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_RE)):
    emp = get_employee_or_404(emp_code)
    row = next(iter(logs.aggregate(build_monthly_pipeline(emp_code, month))), None) or {}
    wd = working_days(month, emp["joined_on"])
    present = num(row.get("p2")) / 2
    pct = half_up(present / wd * 100, 2) if wd else None
    return {"emp_code": emp_code, "month": month, "working_days": wd,
            "present_days": float(half_up(present, 2)), "leave_days": int(row.get("leave", 0)),
            "late_count": int(row.get("late_flag", 0)), "total_late_minutes": int(row.get("late", 0)),
            "total_overtime_minutes": int(row.get("overtime", 0)),
            "attendance_pct": float(pct) if pct is not None else None}


def build_summary_pipeline(month, department):
    first, last = month_bounds(month)
    match = {"joined_on": {"$lte": last}}                       # R9: headcount = joined on or before month end
    if department:
        match["department"] = department
    return [
        {"$match": match},
        # start from EMPLOYEES so people with zero logs still count; join their month of logs by emp_code
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": [{"$match": {"date": {"$gte": first, "$lte": last}}},
                                  {"$project": {"_id": 0, **METRICS}}],
                     "as": "logs"}},
        {"$project": {"_id": 0, "department": 1, **{k: {"$sum": f"$logs.{k}"} for k in METRICS}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, **{k: {"$sum": f"${k}"} for k in METRICS}}},
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_RE), department: Optional[str] = None):
    items = []
    for r in employees.aggregate(build_summary_pipeline(month, department)):
        n = int(r["wh_n"])
        avg = half_up(num(r["wh_cents"]) / (Decimal(n) * 100), 2) if n else None     # mean over RECORDS
        items.append({"department": r["_id"], "headcount": int(r["headcount"]),
                      "present_days": float(half_up(num(r["p2"]) / 2, 2)),
                      "avg_work_hours": float(avg) if avg is not None else None,
                      "late_count": int(r["late_flag"]), "total_late_minutes": int(r["late"]),
                      "leave_count": int(r["leave"]), "on_duty_count": int(r["on_duty"])})
    return {"month": month, "items": items}


def build_leaderboard_pipeline(month, limit, department):
    first, last = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {"_id": "$emp_code", "total": {"$sum": _LATE}, "late_count": {"$sum": METRICS["late_flag"]}}},
        {"$match": {"total": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},                                        # logs of unknown employees disappear here
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})   # rank inside the department only
    pipeline += [
        # standard competition ranking (1,2,2,4): $rank, NOT the array position
        {"$setWindowFields": {"sortBy": {"total": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},                        # limit applies AFTER ranking
        {"$sort": {"total": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
                      "total_late_minutes": "$total", "late_count": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=MONTH_RE), limit: int = Query(10, ge=1, le=50),
                     department: Optional[str] = None):
    items = list(logs.aggregate(build_leaderboard_pipeline(month, limit, department)))
    for r in items:
        r["rank"], r["total_late_minutes"], r["late_count"] = int(r["rank"]), int(r["total_late_minutes"]), int(r["late_count"])
    return {"month": month, "items": items}


def department_roster(department):
    rows = list(employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    return [r["emp_code"] for r in rows], sorted(r["joined_on"] for r in rows)


def check_trend_range(date_from, date_to):
    date_from, date_to = parse_date_param(date_from, "from"), parse_date_param(date_to, "to")
    d0, d1 = Date.fromisoformat(date_from), Date.fromisoformat(date_to)
    if d1 < d0:
        raise unprocessable(["query", "to"], "to must not be before from")
    if (d1 - d0).days + 1 > 92:
        raise unprocessable(["query", "to"], "the range must not exceed 92 days")
    return date_from, date_to


def build_trend_pipeline(codes, joined_list, date_from, date_to):
    d0, d1 = Date.fromisoformat(date_from), Date.fromisoformat(date_to)
    lo = datetime(d0.year, d0.month, d0.day, tzinfo=UTC)
    hi = datetime(d1.year, d1.month, d1.day, tzinfo=UTC) + timedelta(days=1)     # $densify upper bound is exclusive
    window = {"documents": [-6, 0]}                                              # current row + 6 before, inside the range
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": date_from, "$lte": date_to}}},
        {"$group": {"_id": "$date", "p2": {"$sum": _HALVES}, "late_count": {"$sum": METRICS["late_flag"]}}},
        {"$set": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        # gap filling: one row for EVERY calendar day, weekends and days without any record included
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},
        {"$set": {"p2": {"$ifNull": ["$p2", 0]}, "late_count": {"$ifNull": ["$late_count", 0]},
                  "ds": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}}, "dow": {"$dayOfWeek": "$d"}}},
        {"$set": {"working": {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]},
                  "headcount": {"$size": {"$filter": {"input": {"$literal": joined_list}, "as": "j",
                                                      "cond": {"$lte": ["$$j", "$ds"]}}}}}},
        # attendance_rate x 10000, rounded half-up with integer arithmetic: floor((p2*10000 + h) / (2h))
        {"$set": {"scaled": {"$cond": [{"$and": ["$working", {"$gt": ["$headcount", 0]}]},
                                       {"$trunc": [{"$divide": [{"$add": [{"$multiply": ["$p2", 10000]}, "$headcount"]},
                                                                {"$multiply": [2, "$headcount"]}]}, 0]},
                                       None]}}},
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "win_sum": {"$sum": "$scaled", "window": window},
            "win_n": {"$sum": {"$cond": [{"$ne": ["$scaled", None]}, 1, 0]}, "window": window}}}},
        {"$sort": {"d": 1}},
        {"$project": {
            "_id": 0, "date": "$ds", "is_working_day": "$working", "headcount": 1,
            "present_count": {"$divide": ["$p2", 2]}, "late_count": 1,
            "attendance_rate": {"$cond": [{"$eq": ["$scaled", None]}, None, {"$divide": ["$scaled", 10000]}]},
            "moving_avg_7d": {"$cond": [{"$eq": ["$win_n", 0]}, None, {"$divide": [
                {"$trunc": [{"$divide": [{"$add": [{"$multiply": [2, "$win_sum"]}, "$win_n"]},
                                         {"$multiply": [2, "$win_n"]}]}, 0]}, 10000]}]}}},
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    date_from, date_to = check_trend_range(from_, to)
    codes, joined_list = department_roster(department)
    if not codes:
        raise HTTPException(404, "unknown department")
    rows = list(logs.aggregate(build_trend_pipeline(codes, joined_list, date_from, date_to)))
    for r in rows:
        r["headcount"], r["late_count"] = int(r["headcount"]), int(r["late_count"])
    return {"department": department, "items": rows}


# --------------------------------------------------------------------------- #
# 6. Admin: explain
# --------------------------------------------------------------------------- #
def run_explain(command):
    result = db.command({"explain": command, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))


def need(**params):
    for name, value in params.items():
        if value in (None, ""):
            raise unprocessable(["query", name.rstrip("_")], f"{name.rstrip('_')} is required for this endpoint")


@app.get("/admin/explain/{endpoint}")
def explain(endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                              "late_leaderboard", "department_trend"],
            emp_code: Optional[str] = None, month: Optional[str] = Query(None, pattern=MONTH_RE),
            department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),
            date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[Status] = None,
            from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None,
            page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    # Every branch uses the SAME builder as the real endpoint, so the explained plan is the real plan.
    if endpoint == "attendance_list":
        date_from, date_to = check_attendance_filters(date_from, date_to)
        q = build_attendance_query(emp_code, date_from, date_to, status)
        collection = "attendance_logs"
        command = {"find": collection, "filter": q, "sort": ATTENDANCE_SORT,
                   "skip": (page - 1) * page_size, "limit": page_size}
    elif endpoint == "employee_monthly":
        need(emp_code=emp_code, month=month)
        collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": build_monthly_pipeline(emp_code, month), "cursor": {}}
    elif endpoint == "department_summary":
        need(month=month)
        collection = "employees"
        command = {"aggregate": collection, "pipeline": build_summary_pipeline(month, department), "cursor": {}}
    elif endpoint == "late_leaderboard":
        need(month=month)
        collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": build_leaderboard_pipeline(month, limit, department),
                   "cursor": {}}
    else:  # department_trend
        need(department=department, from_=from_, to=to)
        date_from, date_to = check_trend_range(from_, to)
        codes, joined_list = department_roster(department)
        if not codes:
            raise HTTPException(404, "unknown department")
        collection = "attendance_logs"
        command = {"aggregate": collection, "pipeline": build_trend_pipeline(codes, joined_list, date_from, date_to),
                   "cursor": {}}
    return {"endpoint": endpoint, "collection": collection, "explain": run_explain(command)}
