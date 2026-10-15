# Employee Attendance & Analytics API

FastAPI + MongoDB service for employee punch-in/punch-out, manual corrections with an audit trail, and analytics built on MongoDB aggregation pipelines. All application code is in `app/main.py`.

## Requirements
- Python 3.11 or newer
- MongoDB 6.0 or newer (local, Docker, or Atlas). `$densify` and `$setWindowFields` need 6.0+.

## Run

Windows (PowerShell):
```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
$env:MONGO_URI="mongodb://localhost:27017"
$env:MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```

macOS / Linux:
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```

`MONGO_URI` and `MONGO_DB` are read from environment variables (a local `.env` file also works, but real environment variables take priority). All indexes are created at startup, so no manual database setup is needed.

Check it works: open http://localhost:8000/health (returns `{"status":"ok"}`) and http://localhost:8000/docs (interactive API docs).

## Notes
- Every endpoint in `openapi.yaml` is implemented, including `GET /admin/explain/{endpoint}`.
- Business rules R1-R10 are implemented in the pure helper functions at the top of `app/main.py`.
- Analytics read the stored derived fields (`late_minutes`, `half_day`, etc.) and never recompute them from the punch times.
- The 7-day moving average in the trend endpoint averages the reported (4-decimal rounded) daily rates.
- Nothing from the contract is missing.