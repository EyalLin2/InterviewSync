"""
InterviewSync — Frontend BFF (Backend-for-Frontend)
Thin Flask layer: renders Hebrew RTL templates, proxies all data to the backend REST API.
No database access. JWT stored in Flask session.
"""

import os
from functools import wraps

import requests as http
from flask import (Flask, render_template, request, redirect,
                   url_for, session, flash, send_from_directory, abort)

from datetime import date as _date, datetime as _datetime

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")


@app.context_processor
def inject_now():
    return {"now_date": _date.today()}


@app.template_filter("dt")
def fmt_dt(value, fmt="%d/%m/%Y"):
    """Format an ISO datetime/date string or object. {{ value | dt }} or {{ value | dt('%H:%M') }}"""
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = _datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    try:
        return value.strftime(fmt)
    except Exception:
        return str(value)


@app.template_filter("to_date")
def to_date(value):
    """Convert an ISO date string to a date object for arithmetic. {{ value | to_date }}"""
    if not value:
        return None
    if isinstance(value, _date):
        return value
    if isinstance(value, _datetime):
        return value.date()
    try:
        return _datetime.fromisoformat(str(value)).date()
    except Exception:
        return None


# ─────────────────────────────────────────────
# Backend API client
# ─────────────────────────────────────────────

def _api(method: str, path: str, **kwargs) -> http.Response:
    """Make an authenticated request to the backend."""
    token = session.get("token")
    headers = kwargs.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        return http.request(method, f"{BACKEND_URL}{path}",
                            headers=headers, timeout=15, **kwargs)
    except http.exceptions.ConnectionError:
        return _FakeResponse(502, {"error": "Backend unavailable"})
    except http.exceptions.Timeout:
        return _FakeResponse(504, {"error": "Backend timeout"})


class _FakeResponse:
    """Minimal response shim for connection failures."""
    def __init__(self, status_code: int, data: dict):
        self.status_code = status_code
        self._data = data
    def json(self): return self._data
    def ok(self):   return False


def api_get(path, **kw):   return _api("GET",    path, **kw)
def api_post(path, **kw):  return _api("POST",   path, **kw)
def api_patch(path, **kw): return _api("PATCH",  path, **kw)
def api_delete(path, **kw):return _api("DELETE", path, **kw)


def _flash_from_response(r, ok_msg: str = "", ok_level: str = "success"):
    """Flash a message based on API response."""
    if r.status_code < 300:
        if ok_msg:
            flash(ok_msg, ok_level)
    else:
        data = r.json() if callable(r.json) else r.json()
        msg  = data.get("error", data.get("message", "שגיאה בשרת."))
        flash(msg, "danger")


# ─────────────────────────────────────────────
# Auth guards
# ─────────────────────────────────────────────

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("token"):
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if session.get("role") != "admin":
            flash("נדרשת הרשאת מנהל.", "danger")
            return redirect(url_for("index"))
        return fn(*args, **kwargs)
    return wrapper


def me():
    return session.get("user", {})


# ─────────────────────────────────────────────
# Routes — Auth
# ─────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("token"):
        return redirect(url_for("admin_dashboard") if session.get("role") == "admin" else url_for("index"))

    if request.method == "POST":
        r = api_post("/api/auth/login", json={
            "username": request.form.get("username", "").strip(),
            "password": request.form.get("password", ""),
        })
        if r.status_code == 200:
            data = r.json()
            session["token"]   = data["token"]
            session["role"]    = data["role"]
            session["user_id"] = data["user_id"]
            session["user"]    = {"name": data["name"], "role": data["role"]}
            flash(f"ברוך הבא, {data['name']}!", "success")
            if data["role"] == "admin":
                return redirect(url_for("admin_dashboard"))
            if not data.get("profile_complete"):
                return redirect(url_for("onboarding"))
            return redirect(url_for("index"))
        flash("שם משתמש או סיסמה שגויים.", "danger")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ─────────────────────────────────────────────
# Routes — Student onboarding & dashboard
# ─────────────────────────────────────────────

@app.route("/onboarding", methods=["GET", "POST"])
@login_required
def onboarding():
    if session.get("role") == "admin":
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        r = api_post("/api/auth/onboarding", json={
            "full_name":                   request.form.get("full_name", "").strip(),
            "email":                       request.form.get("email", "").strip(),
            "phone":                       request.form.get("phone", "").strip(),
            "education_level":             request.form.get("education_level", "").strip(),
            "current_occupation_or_grade": request.form.get("current_occupation_or_grade", "").strip(),
            "career_goals":                request.form.get("career_goals", "").strip(),
            "fears_weaknesses":            request.form.get("fears_weaknesses", "").strip(),
        })
        if r.status_code == 200:
            flash("הפרופיל נשמר! המנטור שלך יצור עבורך תוכנית.", "success")
            return redirect(url_for("index"))
        flash(r.json().get("error", "שגיאה בשמירת הפרופיל."), "warning")

    return render_template("onboarding.html", user=me())


@app.route("/", methods=["GET"])
@login_required
def index():
    if session.get("role") == "admin":
        return redirect(url_for("admin_dashboard"))

    r    = api_get("/api/my/tasks")
    data = r.json() if r.status_code == 200 else {"active": [], "completed": [], "total": 0, "upcoming_meetings": []}
    return render_template("index.html",
        user=me(),
        active=data["active"],
        completed=data["completed"],
        total=data["total"],
        upcoming_meetings=data["upcoming_meetings"],
    )


@app.route("/complete/<int:tid>", methods=["POST"])
@login_required
def complete_task(tid):
    files = {}
    upload = request.files.get("submission_file")
    if upload and upload.filename:
        files["submission_file"] = (upload.filename, upload.stream, upload.content_type)

    r = _api("POST", f"/api/my/tasks/{tid}/submit",
             data={"submission_note": request.form.get("submission_note", "")},
             files=files if files else None)
    _flash_from_response(r, "המשימה הושלמה! כל הכבוד ✓")
    return redirect(url_for("index"))


@app.route("/schedule")
@login_required
def student_schedule():
    if session.get("role") == "admin":
        return redirect(url_for("admin_dashboard"))
    r        = api_get("/api/my/meetings")
    meetings = r.json() if r.status_code == 200 else []
    return render_template("student_schedule.html", user=me(), meetings=meetings)


@app.route("/meeting/<int:mid>/confirm")
def meeting_confirm(mid):
    token = request.args.get("token", "")
    r     = api_get(f"/api/meetings/{mid}/confirm", params={"token": token})
    if r.status_code == 403:
        abort(403)
    if r.status_code == 404:
        abort(404)
    try:
        data = r.json()
    except Exception:
        abort(500)
    return render_template("meeting_confirm.html",
        meeting=data.get("meeting", {}),
        already_confirmed=data.get("already_confirmed", False))


# ─────────────────────────────────────────────
# Routes — Admin
# ─────────────────────────────────────────────

@app.route("/admin")
@admin_required
def admin_dashboard():
    r_students = api_get("/api/students")
    r_tasks    = api_get("/api/tasks")
    students   = r_students.json() if r_students.status_code == 200 else []
    taskbank   = r_tasks.json()    if r_tasks.status_code    == 200 else []
    categories = sorted({t["category"] for t in taskbank})

    # Build progress dict keyed by student id for template compatibility
    progress = {s["id"]: s.get("progress", {"total": 0, "done": 0, "pct": 0})
                for s in students}

    upcoming_count = sum(
        1 for s in students
        if s.get("progress", {}).get("total", 0) > s.get("progress", {}).get("done", 0)
    )

    return render_template("admin.html",
        user=me(),
        students=students,
        taskbank=taskbank,
        categories=categories,
        progress=progress,
        upcoming_count=upcoming_count,
    )


@app.route("/admin/students", methods=["POST"])
@admin_required
def admin_create_student():
    r = api_post("/api/students", json={
        "username":  request.form.get("username", "").strip(),
        "password":  request.form.get("password", "").strip(),
        "full_name": request.form.get("full_name", "").strip(),
        "phone":     request.form.get("phone", "").strip(),
    })
    if r.status_code == 201:
        flash(f"סטודנט '{request.form.get('username')}' נוצר בהצלחה!", "success")
    else:
        flash(r.json().get("error", "שגיאה ביצירת הסטודנט."), "danger")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/taskbank", methods=["POST"])
@admin_required
def admin_taskbank():
    action = request.form.get("action")

    if action == "add":
        r = api_post("/api/tasks", json={
            "title":       request.form.get("title", "").strip(),
            "description": request.form.get("description", "").strip(),
            "category":    request.form.get("category", "כללי"),
            "task_type":   request.form.get("task_type", "task"),
        })
        if r.status_code == 201:
            tid   = r.json()["id"]
            rfile = request.files.get("resource_file")
            if rfile and rfile.filename:
                _api("POST", f"/api/tasks/{tid}/resource",
                     files={"file": (rfile.filename, rfile.stream, rfile.content_type)})
            flash("המשימה נוספה לבנק.", "success")
        else:
            flash(r.json().get("error", "שגיאה."), "danger")

    elif action == "edit":
        tid  = request.form.get("task_id", type=int)
        body = {k: request.form.get(k) for k in ("title", "description", "category", "task_type")
                if request.form.get(k)}
        if request.form.get("clear_resource"):
            body["clear_resource"] = True
        r = api_patch(f"/api/tasks/{tid}", json=body)
        if r.status_code == 200:
            rfile = request.files.get("resource_file")
            if rfile and rfile.filename:
                _api("POST", f"/api/tasks/{tid}/resource",
                     files={"file": (rfile.filename, rfile.stream, rfile.content_type)})
            flash("המשימה עודכנה.", "success")
        else:
            flash("שגיאה בעדכון.", "danger")

    elif action == "delete":
        tid = request.form.get("task_id", type=int)
        r   = api_delete(f"/api/tasks/{tid}")
        _flash_from_response(r, "המשימה נמחקה.")

    return redirect(url_for("admin_dashboard") + "#tab-taskbank")


@app.route("/admin/student/<int:sid>", methods=["GET", "POST"])
@admin_required
def student_file(sid):
    if request.method == "POST":
        action = request.form.get("action")

        if action == "assign_tasks":
            task_ids = request.form.getlist("task_ids", type=int)
            r = api_post(f"/api/students/{sid}/assignments", json={"task_ids": task_ids})
            if r.status_code == 200:
                d = r.json()
                msg = f"שויכו {d['assigned']} משימות"
                msg += " ונשלחה הודעת WhatsApp." if d["notified"] else ". WhatsApp לא מוגדר."
                flash(msg, "success" if d["notified"] else "info")
            else:
                flash("שגיאה בשיוך.", "danger")

        elif action == "save_resume":
            r = api_patch(f"/api/students/{sid}/resume",
                          json={"resume_content": request.form.get("resume_content", "")})
            _flash_from_response(r, "קורות החיים נשמרו.")

        elif action == "save_profile_settings":
            r = api_patch(f"/api/students/{sid}/profile", json={
                "process_start_date": request.form.get("process_start_date") or None,
                "target_end_date":    request.form.get("target_end_date")    or None,
                "mentor_notes":       request.form.get("mentor_notes", ""),
            })
            _flash_from_response(r, "הגדרות התהליך נשמרו.")

        elif action == "regenerate_strategy":
            r = api_post(f"/api/ai/coaching-strategy/{sid}")
            _flash_from_response(r, "אסטרטגיית ההדרכה עודכנה.")

        return redirect(url_for("student_file", sid=sid))

    # GET
    r_student = api_get(f"/api/students/{sid}")
    r_tasks   = api_get("/api/tasks")
    if r_student.status_code != 200:
        flash("סטודנט לא נמצא.", "danger")
        return redirect(url_for("admin_dashboard"))

    student    = r_student.json()
    taskbank   = r_tasks.json() if r_tasks.status_code == 200 else []
    categories = sorted({t["category"] for t in taskbank})

    return render_template("student_file.html",
        user=me(),
        student=student,
        profile=student.get("profile", {}),
        taskbank=taskbank,
        assigned_ids=set(student.get("assigned_ids", [])),
        active=student.get("active", []),
        completed=student.get("completed", []),
        categories=categories,
        student_meetings=student.get("meetings", []),
    )


@app.route("/admin/ai-tasks/<int:sid>", methods=["POST"])
@admin_required
def ai_tasks_for_student(sid):
    r = api_post(f"/api/ai/tasks/{sid}")
    if r.status_code == 200:
        flash(f"נוצרו ושויכו {r.json()['created']} משימות AI.", "success")
    else:
        flash(r.json().get("error", "שגיאת AI."), "warning")
    return redirect(url_for("student_file", sid=sid))


@app.route("/admin/schedule", methods=["GET", "POST"])
@admin_required
def admin_schedule():
    if request.method == "POST":
        action = request.form.get("action")

        if action == "create_meeting":
            r = api_post("/api/meetings", json={
                "student_id":   request.form.get("student_id", type=int),
                "scheduled_at": request.form.get("scheduled_at", ""),
                "duration_min": request.form.get("duration_min", 60, type=int),
                "notes":        request.form.get("notes", ""),
            })
            if r.status_code == 201:
                d   = r.json()
                msg = "הפגישה נקבעה"
                msg += " ונשלחה הודעת WhatsApp." if d["notified"] else ". WhatsApp לא מוגדר — עדכן/י ידנית."
                flash(msg, "success" if d["notified"] else "info")
            else:
                flash(r.json().get("error", "שגיאה."), "danger")

        elif action == "cancel_meeting":
            mid = request.form.get("meeting_id", type=int)
            r   = api_patch(f"/api/meetings/{mid}", json={"action": "cancel"})
            _flash_from_response(r, "הפגישה בוטלה.")

        elif action == "send_reminder":
            mid = request.form.get("meeting_id", type=int)
            r   = api_patch(f"/api/meetings/{mid}", json={"action": "send_reminder"})
            if r.status_code == 200:
                flash("תזכורת WhatsApp נשלחה.", "success")
            else:
                err = r.json().get("error", "")
                if err == "no_config":
                    flash("Twilio לא מוגדר — הוסף TWILIO_* env vars.", "warning")
                elif err == "no_phone":
                    flash("לסטודנט אין מספר טלפון.", "warning")
                else:
                    flash(f"שגיאת WhatsApp: {err}", "danger")

        return redirect(url_for("admin_schedule"))

    # GET: fetch calendar data from backend
    year  = request.args.get("year",  type=int)
    month = request.args.get("month", type=int)
    params = {}
    if year:  params["year"]  = year
    if month: params["month"] = month

    r    = api_get("/api/meetings", params=params)
    data = r.json() if r.status_code == 200 else {}

    return render_template("admin_schedule.html",
        user=me(),
        students=data.get("students", []),
        upcoming=data.get("upcoming", []),
        cal_weeks=data.get("cal_weeks", []),
        meetings_by_day=data.get("meetings_by_day", {}),
        year=data.get("year"),
        month=data.get("month"),
        month_name=data.get("month_name", ""),
        prev_year=data.get("prev_year"),  prev_month=data.get("prev_month"),
        next_year=data.get("next_year"),  next_month=data.get("next_month"),
        today=data.get("today", ""),
    )


# ─────────────────────────────────────────────
# File proxy (serves uploaded files via backend)
# ─────────────────────────────────────────────

@app.route("/files/<path:filepath>")
@login_required
def serve_file(filepath):
    r = api_get(f"/api/files/{filepath}", stream=True)
    if r.status_code != 200:
        abort(404)
    from flask import Response
    return Response(r.iter_content(chunk_size=8192),
                    content_type=r.headers.get("Content-Type", "application/octet-stream"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000,
            debug=os.environ.get("FLASK_DEBUG", "false").lower() == "true")
