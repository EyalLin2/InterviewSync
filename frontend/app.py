"""
InterviewSync — Frontend BFF (Backend-for-Frontend)
Thin Flask layer: renders Hebrew RTL templates, proxies all data to the backend REST API.
No database access. JWT stored in Flask session.
"""

import os
from functools import wraps

import requests as http
from flask import (Flask, render_template, request, redirect, abort,
                   url_for, session, flash)

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
    @property
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
        return redirect(url_for("admin_hub") if session.get("role") == "admin" else url_for("index"))

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
                return redirect(url_for("admin_hub"))
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
            # type-specific fields
            "interests_hobbies":    request.form.get("interests_hobbies", "").strip(),
            "institution_name":     request.form.get("institution_name", "").strip(),
            "graduation_year":      request.form.get("graduation_year") or None,
            "current_job":          request.form.get("current_job", "").strip(),
            "years_experience":     request.form.get("years_experience") or None,
            "reason_for_guidance":  request.form.get("reason_for_guidance", "").strip(),
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
    from datetime import timedelta
    _today = _date.today()
    return render_template("index.html",
        nav_role="student",
        user=me(),
        active=data["active"],
        completed=data["completed"],
        total=data["total"],
        upcoming_meetings=data["upcoming_meetings"],
        today=_today.isoformat(),
        near_due=(_today + timedelta(days=3)).isoformat(),
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


@app.route("/settings", methods=["GET", "POST"])
@login_required
def student_settings():
    if session.get("role") == "admin":
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        action = request.form.get("action")
        if action == "update_profile":
            r = api_patch("/api/my/profile", json={
                "full_name":        request.form.get("full_name", "").strip(),
                "email":            request.form.get("email", "").strip(),
                "phone":            request.form.get("phone", "").strip(),
                "career_goals":     request.form.get("career_goals", "").strip(),
                "fears_weaknesses": request.form.get("fears_weaknesses", "").strip(),
            })
            _flash_from_response(r, "הפרופיל עודכן בהצלחה ✓")
        elif action == "change_password":
            r = api_post("/api/my/password", json={
                "current_password": request.form.get("current_password", ""),
                "new_password":     request.form.get("new_password", ""),
            })
            if r.status_code == 200:
                flash("הסיסמה שונתה בהצלחה ✓", "success")
            else:
                flash(r.json().get("error", "שגיאה בשינוי הסיסמה."), "danger")
        return redirect(url_for("student_settings"))

    r_me = api_get("/api/auth/me")
    data = r_me.json() if r_me.status_code == 200 else {}
    return render_template("student_settings.html",
        nav_role="student", current_page="settings",
        user=me(),
        profile=data.get("profile", {}),
    )


@app.route("/schedule")
@login_required
def student_schedule():
    if session.get("role") == "admin":
        return redirect(url_for("admin_dashboard"))
    r        = api_get("/api/my/meetings")
    r_me     = api_get("/api/auth/me")
    meetings = r.json() if r.status_code == 200 else []
    me_data  = r_me.json() if r_me.status_code == 200 else {}
    return render_template("student_schedule.html", nav_role="student", current_page="schedule",
                           user=me(), meetings=meetings,
                           profile=me_data.get("profile", {}))


@app.route("/api/tasks/<int:tid>/comments", methods=["GET"])
@login_required
def task_comments_get(tid):
    from flask import jsonify as _j
    r = api_get(f"/api/my/tasks/{tid}/comments")
    return _j(r.json() if r.status_code == 200 else []), r.status_code if r.status_code != 200 else 200


@app.route("/api/tasks/<int:tid>/comments", methods=["POST"])
@login_required
def task_comments_post(tid):
    from flask import jsonify as _j
    r = api_post(f"/api/my/tasks/{tid}/comments", json=request.get_json() or {})
    return _j(r.json()), r.status_code


@app.route("/api/progress")
@login_required
def student_progress():
    from flask import jsonify as _j
    r = api_get("/api/my/progress")
    return _j(r.json() if r.status_code == 200 else {}), r.status_code


@app.route("/api/student-chat", methods=["POST"])
@login_required
def student_ai_chat():
    from flask import jsonify as _j
    r = api_post("/api/my/ai-chat", json=request.get_json() or {})
    return _j(r.json()), r.status_code


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
# Routes — Admin Hub (switcher)
# ─────────────────────────────────────────────

@app.route("/admin")
@admin_required
def admin_hub():
    """Landing page — pick Private CRM or Business dashboard."""
    r_biz = api_get("/api/business/overview")
    biz   = r_biz.json() if r_biz.status_code == 200 else {}
    r_stu = api_get("/api/students")
    students_count = len(r_stu.json()) if r_stu.status_code == 200 else 0
    return render_template("admin_hub.html",
        nav_role="hub",
        user=me(),
        students_count=students_count,
        inquiries_new=biz.get("inquiries_new", 0),
        upcoming_meetings_count=0,
    )


# ─────────────────────────────────────────────
# Routes — Admin Private (CRM)
# ─────────────────────────────────────────────

@app.route("/admin/private")
@admin_required
def admin_dashboard():
    r_students = api_get("/api/students")
    r_tasks    = api_get("/api/tasks")
    r_focus    = api_get("/api/dashboard/focus")
    r_risk     = api_get("/api/dashboard/risk")
    students   = r_students.json() if r_students.status_code == 200 else []
    taskbank   = r_tasks.json()    if r_tasks.status_code    == 200 else []
    focus      = r_focus.json()    if r_focus.status_code    == 200 else {}
    risk_list  = r_risk.json()     if r_risk.status_code     == 200 else []
    categories = sorted({t["category"] for t in taskbank})

    progress = {s["id"]: s.get("progress", {"total": 0, "done": 0, "pct": 0})
                for s in students}
    risk_map = {r["id"]: r for r in risk_list}

    upcoming_count = sum(
        1 for s in students
        if s.get("progress", {}).get("total", 0) > s.get("progress", {}).get("done", 0)
    )

    new_submissions_count = focus.get("new_submissions_count", 0)

    return render_template("admin.html",
        nav_role="admin", current_page="dashboard",
        user=me(),
        students=students,
        taskbank=taskbank,
        categories=categories,
        progress=progress,
        upcoming_count=upcoming_count,
        focus=focus,
        new_submissions_count=new_submissions_count,
        risk_map=risk_map,
    )


@app.route("/admin/private/students", methods=["POST"])
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


@app.route("/admin/private/taskbank", methods=["POST"])
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


@app.route("/admin/private/student/<int:sid>", methods=["GET", "POST"])
@admin_required
def student_file(sid):
    if request.method == "POST":
        action = request.form.get("action")

        if action == "assign_tasks":
            task_ids = request.form.getlist("task_ids", type=int)
            due_dates = {}
            for tid in task_ids:
                dd = request.form.get(f"due_date_{tid}", "").strip()
                if dd:
                    due_dates[str(tid)] = dd
            r = api_post(f"/api/students/{sid}/assignments", json={"task_ids": task_ids, "due_dates": due_dates})
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
                "student_status":     request.form.get("student_status", "active"),
            })
            _flash_from_response(r, "הגדרות התהליך נשמרו.")

        elif action == "save_full_profile":
            r = api_patch(f"/api/students/{sid}/profile", json={
                "full_name":                   request.form.get("full_name", ""),
                "email":                       request.form.get("email", ""),
                "phone":                       request.form.get("phone", ""),
                "education_level":             request.form.get("education_level", ""),
                "current_occupation_or_grade": request.form.get("current_occupation_or_grade", ""),
                "career_goals":                request.form.get("career_goals", ""),
                "fears_weaknesses":            request.form.get("fears_weaknesses", ""),
            })
            _flash_from_response(r, "הפרופיל עודכן.")

        elif action == "regenerate_strategy":
            r = api_post(f"/api/ai/coaching-strategy/{sid}")
            if r.status_code == 503:
                flash("Gemini AI לא מוגדר — הוסף GROQ_API_KEY ל-docker-compose.yml. קבל מפתח חינמי: console.groq.com/apikey", "warning")
            else:
                _flash_from_response(r, "אסטרטגיית ההדרכה עודכנה ✓")

        elif action == "add_note":
            text = request.form.get("note_text", "").strip()
            if text:
                r = api_post(f"/api/students/{sid}/notes", json={"text": text})
                _flash_from_response(r, "ההערה נוספה.")

        elif action == "delete_note":
            nid = request.form.get("note_id", type=int)
            r   = api_delete(f"/api/notes/{nid}")
            _flash_from_response(r, "ההערה נמחקה.")

        return redirect(url_for("student_file", sid=sid))

    # GET
    r_student = api_get(f"/api/students/{sid}")
    r_tasks   = api_get("/api/tasks")
    r_notes   = api_get(f"/api/students/{sid}/notes")
    if r_student.status_code != 200:
        flash("סטודנט לא נמצא.", "danger")
        return redirect(url_for("admin_dashboard"))

    student    = r_student.json()
    taskbank   = r_tasks.json()  if r_tasks.status_code  == 200 else []
    notes      = r_notes.json()  if r_notes.status_code  == 200 else []
    categories = sorted({t["category"] for t in taskbank})
    assigned_due_dates = {
        at["task_id"]: at["due_date"]
        for at in student.get("active", []) + student.get("completed", [])
        if at.get("due_date")
    }

    r_billing = api_get(f"/api/students/{sid}/billing")
    r_svcs    = api_get("/api/services")
    billing_info = r_billing.json() if r_billing.status_code == 200 else {}
    services     = r_svcs.json()    if r_svcs.status_code    == 200 else []

    return render_template("student_file.html",
        nav_role="admin",
        user=me(),
        student=student,
        profile=student.get("profile", {}),
        taskbank=taskbank,
        assigned_ids=set(student.get("assigned_ids", [])),
        assigned_due_dates=assigned_due_dates,
        active=student.get("active", []),
        completed=student.get("completed", []),
        categories=categories,
        student_meetings=student.get("meetings", []),
        notes=notes,
        billing_info=billing_info,
        services=services,
    )


@app.route("/admin/private/student/<int:sid>/delete", methods=["POST"])
@admin_required
def admin_delete_student(sid):
    r = api_delete(f"/api/students/{sid}")
    if r.status_code == 200:
        flash("הסטודנט נמחק.", "success")
    else:
        try:
            flash(r.json().get("error", "שגיאה."), "danger")
        except Exception:
            flash("שגיאה במחיקה.", "danger")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/private/submissions")
@admin_required
def admin_submissions():
    r = api_get("/api/submissions")
    submissions = r.json() if r.status_code == 200 else []
    return render_template("admin_submissions.html", nav_role="admin", current_page="submissions", user=me(), submissions=submissions)


@app.route("/admin/private/submissions/<int:at_id>/feedback", methods=["POST"])
@admin_required
def admin_submission_feedback(at_id):
    feedback = request.form.get("feedback", "").strip()
    r = api_patch(f"/api/admin/assignments/{at_id}/feedback", json={"feedback": feedback})
    if r.status_code == 200:
        flash("הפידבק נשמר ✓", "success")
    else:
        flash("שגיאה בשמירת הפידבק.", "danger")
    return redirect(url_for("admin_submissions"))


@app.route("/admin/private/students/bulk-status", methods=["POST"])
@admin_required
def admin_bulk_status():
    ids = [int(i) for i in request.form.getlist("student_ids") if i.isdigit()]
    status = request.form.get("status", "")
    if ids and status:
        r = api_post("/api/students/bulk-status", json={"ids": ids, "status": status})
        if r.status_code == 200:
            flash(f"עודכן סטטוס ל-{r.json().get('updated', 0)} תלמידים.", "success")
        else:
            flash("שגיאה בעדכון הסטטוס.", "danger")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/private/student/<int:sid>/report")
@admin_required
def student_report(sid):
    r = api_get(f"/api/students/{sid}/report")
    if r.status_code != 200:
        flash("סטודנט לא נמצא.", "danger")
        return redirect(url_for("admin_dashboard"))
    data = r.json()
    r_notes = api_get(f"/api/students/{sid}/notes")
    notes   = r_notes.json() if r_notes.status_code == 200 else []
    return render_template("student_report.html",
        user=me(),
        student=data["student"],
        profile=data["profile"],
        active=data["active"],
        completed=data["completed"],
        notes=notes,
        now_date=_date.today(),
    )


@app.route("/admin/private/billing")
@admin_required
def billing_dashboard():
    month = request.args.get("month", "")
    params = {"month": month} if month else {}
    r = api_get("/api/billing", params=params)
    data = r.json() if r.status_code == 200 else {}
    r_svcs = api_get("/api/services")
    services = r_svcs.json() if r_svcs.status_code == 200 else []
    return render_template("billing_dashboard.html",
        nav_role="admin", current_page="billing",
        user=me(), data=data, services=services)


@app.route("/admin/private/billing/generate", methods=["POST"])
@admin_required
def billing_generate():
    month = request.form.get("month", _datetime.utcnow().strftime("%Y-%m"))
    r = api_post(f"/api/billing/generate/{month}")
    if r.status_code == 200:
        flash(f"חיוב חושב ל-{month}: {r.json().get('processed',0)} סטודנטים.", "success")
    else:
        flash("שגיאה בחישוב חיוב.", "danger")
    return redirect(url_for("billing_dashboard") + f"?month={month}")


@app.route("/admin/private/billing/<int:rec_id>/pay", methods=["POST"])
@admin_required
def billing_mark_paid(rec_id):
    paid = request.form.get("paid", "1") == "1"
    note = request.form.get("note", "").strip()
    r = api_patch(f"/api/billing/{rec_id}/pay",
                  json={"paid": paid, "note": note})
    if r.status_code < 300:
        flash("עודכן.", "success")
    else:
        try:
            flash(r.json().get("error", "שגיאה."), "danger")
        except Exception:
            flash("שגיאה בעדכון.", "danger")
    month = request.form.get("month", "")
    return redirect(url_for("billing_dashboard") + (f"?month={month}" if month else ""))


@app.route("/admin/private/billing/invoice/<int:student_id>/<month>")
@admin_required
def billing_invoice(student_id, month):
    r_hist = api_get(f"/api/students/{student_id}/billing/history",
                     params={"year": month[:4]})
    r_stu  = api_get(f"/api/students/{student_id}")
    if r_stu.status_code != 200:
        flash("סטודנט לא נמצא.", "danger")
        return redirect(url_for("billing_dashboard"))
    history = r_hist.json() if r_hist.status_code == 200 else {}
    student = r_stu.json()
    # Find this month record
    rec = next((r for r in history.get("records",[]) if r["month"] == month), None)
    return render_template("billing_invoice.html",
        user=me(), student=student, profile=student.get("profile",{}),
        record=rec, month=month, history=history)


@app.route("/admin/private/services")
@admin_required
def services_settings():
    r = api_get("/api/services")
    services = r.json() if r.status_code == 200 else []
    return render_template("services_settings.html", nav_role="admin", current_page="services", user=me(), services=services)


@app.route("/admin/private/services", methods=["POST"])
@admin_required
def services_settings_post():
    action = request.form.get("action")
    if action == "add":
        r = api_post("/api/services", json={
            "name":             request.form.get("name","").strip(),
            "description":      request.form.get("description","").strip(),
            "unit":             request.form.get("unit","per_session"),
            "price_highschool": float(request.form.get("price_highschool",0) or 0),
            "price_college":    float(request.form.get("price_college",0) or 0),
            "price_career":     float(request.form.get("price_career",0) or 0),
        })
        _flash_from_response(r, "השירות נוסף.", "success")
    elif action == "edit":
        sid = request.form.get("service_id", type=int)
        r = api_patch(f"/api/services/{sid}", json={
            "name":             request.form.get("name","").strip(),
            "description":      request.form.get("description","").strip(),
            "unit":             request.form.get("unit","per_session"),
            "price_highschool": float(request.form.get("price_highschool",0) or 0),
            "price_college":    float(request.form.get("price_college",0) or 0),
            "price_career":     float(request.form.get("price_career",0) or 0),
        })
        _flash_from_response(r, "עודכן.")
    elif action == "delete":
        sid = request.form.get("service_id", type=int)
        r = api_delete(f"/api/services/{sid}")
        _flash_from_response(r, "נמחק.")
    return redirect(url_for("services_settings"))


@app.route("/admin/private/student/<int:sid>/billing-settings", methods=["POST"])
@admin_required
def student_billing_set(sid):
    _cp = request.form.get("custom_price", "").strip()
    r = api_post(f"/api/students/{sid}/billing", json={
        "service_id":   request.form.get("service_id", type=int),
        "custom_price": float(_cp) if _cp else None,
    })
    _flash_from_response(r, "הגדרות חיוב עודכנו.")
    return redirect(url_for("student_file", sid=sid))


@app.route("/admin/private/reports/meetings")
@admin_required
def meetings_report():
    year  = request.args.get("year",  type=int)
    month = request.args.get("month", type=int)
    params = {}
    if year:  params["year"]  = year
    if month: params["month"] = month
    r    = api_get("/api/reports/meetings", params=params)
    data = r.json() if r.status_code == 200 else {}
    return render_template("meetings_report.html",
        user=me(), report=data)


@app.route("/admin/private/student/<int:sid>/chat", methods=["POST"])
@admin_required
def student_chat_proxy(sid):
    from flask import jsonify as _jsonify
    body = request.get_json() or {}
    r = api_post(f"/api/students/{sid}/chat", json=body)
    try:
        return _jsonify(r.json()), r.status_code
    except Exception:
        return _jsonify({"reply": "שגיאת תקשורת עם השרת."}), 500


@app.route("/admin/private/student/<int:sid>/ai-suggest", methods=["POST"])
@admin_required
def student_ai_suggest(sid):
    from flask import jsonify as _j
    r = api_post(f"/api/ai/tasks/{sid}/suggest")
    try:
        return _j(r.json()), r.status_code
    except Exception:
        return _j({"error": "שגיאה"}), 500


@app.route("/admin/private/student/<int:sid>/ai-confirm", methods=["POST"])
@admin_required
def student_ai_confirm(sid):
    from flask import jsonify as _j
    body = request.get_json() or {}
    r = api_post(f"/api/ai/tasks/{sid}/confirm", json=body)
    try:
        return _j(r.json()), r.status_code
    except Exception:
        return _j({"error": "שגיאה"}), 500


@app.route("/admin/private/new-intake")
@admin_required
def admin_new_intake():
    """Start a new student intake — creates account then goes to questionnaire."""
    return render_template("admin_new_intake.html", user=me())


@app.route("/admin/private/new-intake", methods=["POST"])
@admin_required
def admin_new_intake_post():
    username = request.form.get("username","").strip()
    password = request.form.get("password","").strip() or "change123"
    r = api_post("/api/students", json={"username": username, "password": password})
    if r.status_code == 201:
        sid = r.json()["id"]
        return redirect(url_for("admin_intake", sid=sid))
    flash(r.json().get("error","שגיאה ביצירת סטודנט."), "danger")
    return redirect(url_for("admin_new_intake"))


@app.route("/admin/private/student/<int:sid>/intake", methods=["GET", "POST"])
@admin_required
def admin_intake(sid):
    """Admin fills the onboarding questionnaire for a student during a meeting."""
    r_student = api_get(f"/api/students/{sid}")
    if r_student.status_code != 200:
        flash("סטודנט לא נמצא.", "danger")
        return redirect(url_for("admin_dashboard"))
    student = r_student.json()

    if request.method == "POST":
        r = api_patch(f"/api/students/{sid}/profile", json={
            "full_name":                   request.form.get("full_name","").strip(),
            "email":                       request.form.get("email","").strip(),
            "phone":                       request.form.get("phone","").strip(),
            "education_level":             request.form.get("education_level","").strip(),
            "current_occupation_or_grade": request.form.get("current_occupation_or_grade","").strip(),
            "career_goals":                request.form.get("career_goals","").strip(),
            "fears_weaknesses":            request.form.get("fears_weaknesses","").strip(),
            "interests_hobbies":           request.form.get("interests_hobbies","").strip(),
            "institution_name":            request.form.get("institution_name","").strip(),
            "graduation_year":             request.form.get("graduation_year") or None,
            "current_job":                 request.form.get("current_job","").strip(),
            "years_experience":            request.form.get("years_experience") or None,
            "reason_for_guidance":         request.form.get("reason_for_guidance","").strip(),
        })
        if r.status_code == 200:
            intake_note = request.form.get("intake_notes","").strip()
            if intake_note:
                api_post(f"/api/students/{sid}/notes", json={"text": f"[שאלון קבלה] {intake_note}"})
            api_post(f"/api/ai/coaching-strategy/{sid}")
            flash("השאלון נשמר ואסטרטגיית הדרכה עודכנה. ✓", "success")
        else:
            flash(r.json().get("error","שגיאה."), "danger")
        return redirect(url_for("student_file", sid=sid))

    return render_template("admin_intake.html",
        user=me(), student=student,
        profile=student.get("profile", {}))


@app.route("/admin/private/student/<int:sid>/billing-history")
@admin_required
def student_billing_history_proxy(sid):
    from flask import jsonify as _j
    year = request.args.get("year", "")
    params = {"year": year} if year else {}
    r = api_get(f"/api/students/{sid}/billing/history", params=params)
    try:
        return _j(r.json()), r.status_code
    except Exception:
        return _j({"records": []}), 500


@app.route("/admin/private/assignment/<int:at_id>/complete", methods=["POST"])
@admin_required
def admin_complete_task(at_id):
    note = request.form.get("note","").strip()
    r = api_patch(f"/api/admin/assignments/{at_id}/complete",
                  json={"note": note})
    if r.status_code == 200:
        flash("המשימה סומנה כהושלמה ✓", "success")
    else:
        flash("שגיאה.", "danger")
    sid = request.form.get("student_id","")
    return redirect(url_for("student_file", sid=sid) if sid else url_for("admin_dashboard"))


@app.route("/admin/private/assignment/<int:at_id>/feedback", methods=["POST"])
@admin_required
def admin_task_feedback(at_id):
    feedback = request.form.get("feedback", "").strip()
    r = api_patch(f"/api/admin/assignments/{at_id}/feedback",
                  json={"feedback": feedback})
    if r.status_code == 200:
        flash("הפידבק נשמר ✓", "success")
    else:
        flash("שגיאה בשמירת הפידבק.", "danger")
    sid = request.form.get("student_id", "")
    return redirect(url_for("student_file", sid=sid) if sid else url_for("admin_dashboard"))


@app.route("/my/tasks/<int:tid>/feedback-seen", methods=["POST"])
@login_required
def task_feedback_seen(tid):
    api_post(f"/api/my/tasks/{tid}/feedback-seen")
    return ("", 204)


@app.route("/admin/private/student/<int:sid>/upload-cv", methods=["POST"])
@admin_required
def admin_upload_cv(sid):
    cv_file = request.files.get("cv_file")
    if cv_file and cv_file.filename:
        r = _api("POST", f"/api/students/{sid}/cv",
                 files={"cv_file": (cv_file.filename, cv_file.stream, cv_file.content_type)})
        if r.status_code == 200:
            flash("קובץ קורות החיים הועלה בהצלחה.", "success")
        else:
            flash(r.json().get("error", "שגיאה בהעלאה."), "danger")
    else:
        flash("לא נבחר קובץ.", "warning")
    return redirect(url_for("student_file", sid=sid))


@app.route("/admin/private/ai-tasks/<int:sid>", methods=["POST"])
@admin_required
def ai_tasks_for_student(sid):
    r = api_post(f"/api/ai/tasks/{sid}")
    if r.status_code == 200:
        flash(f"נוצרו ושויכו {r.json()['created']} משימות AI.", "success")
    else:
        flash(r.json().get("error", "שגיאת AI."), "warning")
    return redirect(url_for("student_file", sid=sid))


@app.route("/admin/private/schedule", methods=["GET", "POST"])
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
                "meeting_type": request.form.get("meeting_type", "progress_review"),
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

        elif action == "mark_completed":
            mid = request.form.get("meeting_id", type=int)
            r   = api_patch(f"/api/meetings/{mid}", json={
                "action":        "mark_completed",
                "outcome_notes": request.form.get("outcome_notes", "").strip(),
                "action_items":  request.form.get("action_items", "").strip(),
            })
            _flash_from_response(r, "הפגישה סומנה כהתקיימה ✓")

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
    year    = request.args.get("year",    type=int)
    month   = request.args.get("month",   type=int)
    prefill = request.args.get("prefill", type=int)  # student_id to pre-select
    params  = {}
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
        prefill_student=prefill,
        today=data.get("today", ""),
        upcoming_workshops=data.get("upcoming_workshops", []),
    )


# ─────────────────────────────────────────────
# Routes — Admin Business
# ─────────────────────────────────────────────

@app.route("/admin/business")
@admin_required
def admin_business():
    r = api_get("/api/business/overview")
    data = r.json() if r.status_code == 200 else {}
    r_ws  = api_get("/api/workshops")
    r_iq  = api_get("/api/inquiries")
    r_act = api_get("/api/activities")
    workshops  = r_ws.json()  if r_ws.status_code  == 200 else []
    inquiries  = r_iq.json()  if r_iq.status_code  == 200 else []
    activities = r_act.json() if r_act.status_code == 200 else []
    return render_template("admin_business.html",
        user=me(),
        overview=data,
        workshops=workshops,
        inquiries=inquiries,
        activities=activities,
        topic_categories=data.get("topic_categories", []),
    )


@app.route("/admin/business/workshops", methods=["POST"])
@admin_required
def admin_workshops():
    action = request.form.get("action")
    if action == "add":
        r = api_post("/api/workshops", json={
            "title":            request.form.get("title", "").strip(),
            "description":      request.form.get("description", "").strip(),
            "topic_category":   request.form.get("topic_category", "כללי"),
            "workshop_type":    request.form.get("workshop_type", "one_time"),
            "scheduled_at":     request.form.get("scheduled_at") or None,
            "location":         request.form.get("location", "").strip(),
            "max_participants": request.form.get("max_participants", type=int),
            "notes":            request.form.get("notes", "").strip(),
        })
        _flash_from_response(r, "הסדנה נוספה בהצלחה.", "success")
    elif action == "edit":
        wid  = request.form.get("workshop_id", type=int)
        body = {f: request.form.get(f) for f in
                ("title", "description", "topic_category", "workshop_type", "status", "location", "notes")
                if request.form.get(f) is not None}
        body["scheduled_at"]     = request.form.get("scheduled_at") or None
        body["max_participants"] = request.form.get("max_participants", type=int)
        r = api_patch(f"/api/workshops/{wid}", json=body)
        _flash_from_response(r, "הסדנה עודכנה.")
    elif action == "delete":
        wid = request.form.get("workshop_id", type=int)
        r   = api_delete(f"/api/workshops/{wid}")
        _flash_from_response(r, "הסדנה נמחקה.")
    return redirect(url_for("admin_business") + "#tab-workshops")


@app.route("/admin/business/inquiries", methods=["POST"])
@admin_required
def admin_inquiries():
    action = request.form.get("action")
    if action == "add":
        r = api_post("/api/inquiries", json={
            "full_name": request.form.get("full_name", "").strip(),
            "phone":     request.form.get("phone", "").strip(),
            "email":     request.form.get("email", "").strip(),
            "topic":     request.form.get("topic", "").strip(),
            "source":    request.form.get("source", ""),
            "notes":     request.form.get("notes", "").strip(),
        })
        _flash_from_response(r, "הפנייה נוספה.", "success")
    elif action == "update_status":
        iid  = request.form.get("inquiry_id", type=int)
        body = {"status": request.form.get("status", "")}
        wid  = request.form.get("workshop_id", type=int)
        if wid:
            body["workshop_id"] = wid
        r = api_patch(f"/api/inquiries/{iid}", json=body)
        _flash_from_response(r, "הפנייה עודכנה.")
    elif action == "delete":
        iid = request.form.get("inquiry_id", type=int)
        r   = api_delete(f"/api/inquiries/{iid}")
        _flash_from_response(r, "הפנייה נמחקה.")
    return redirect(url_for("admin_business") + "#tab-inquiries")


@app.route("/admin/business/activities", methods=["POST"])
@admin_required
def admin_activities():
    action = request.form.get("action")
    if action == "add":
        r = api_post("/api/activities", json={
            "title":              request.form.get("title", "").strip(),
            "activity_type":      request.form.get("activity_type", "other"),
            "topic_category":     request.form.get("topic_category", ""),
            "activity_date":      request.form.get("activity_date", ""),
            "duration_min":       request.form.get("duration_min", type=int),
            "participants_count": request.form.get("participants_count", type=int),
            "description":        request.form.get("description", "").strip(),
            "workshop_id":        request.form.get("workshop_id", type=int),
        })
        _flash_from_response(r, "הפעילות תועדה.", "success")
    elif action == "delete":
        aid = request.form.get("activity_id", type=int)
        r   = api_delete(f"/api/activities/{aid}")
        _flash_from_response(r, "הפעילות נמחקה.")
    return redirect(url_for("admin_business") + "#tab-activities")


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


@app.errorhandler(404)
def page_not_found(e):
    return render_template("404.html", user=me()), 404


@app.errorhandler(500)
def internal_error(e):
    return render_template("500.html", user=me()), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000,
            debug=os.environ.get("FLASK_DEBUG", "false").lower() == "true")
