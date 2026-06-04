import os
import json
import hmac
import hashlib
import calendar as _cal
from datetime import datetime, date, timedelta
from collections import defaultdict
from functools import wraps

import jwt
from flask import Flask, request, jsonify, send_from_directory, abort
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from openai import OpenAI

from models import db, get_database_url, User, StudentProfile, TaskBank, AssignedTask, Meeting

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"]     = get_database_url()
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"]          = 5 * 1024 * 1024  # 5 MB

SECRET_KEY    = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "/app/uploads")
ALLOWED_EXT   = {"pdf", "doc", "docx", "png", "jpg", "jpeg", "txt"}

db.init_app(app)
CORS(app, supports_credentials=True)


# ─────────────────────────────────────────────
# JWT helpers
# ─────────────────────────────────────────────

def create_token(user_id: int, role: str) -> str:
    payload = {
        "sub":  user_id,
        "role": role,
        "exp":  datetime.utcnow() + timedelta(days=7),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None


def get_token_from_request() -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        payload = decode_token(get_token_from_request() or "")
        if not payload:
            return jsonify({"error": "Unauthorized"}), 401
        request.user_id = payload["sub"]
        request.role    = payload["role"]
        return fn(*args, **kwargs)
    return wrapper


def require_admin(fn):
    @wraps(fn)
    @require_auth
    def wrapper(*args, **kwargs):
        if request.role != "admin":
            return jsonify({"error": "Admin access required"}), 403
        return fn(*args, **kwargs)
    return wrapper


# ─────────────────────────────────────────────
# File upload helpers
# ─────────────────────────────────────────────

def _allowed(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def _save_file(file, subdir: str, prefix: str) -> str:
    if not file or not file.filename or not _allowed(file.filename):
        return ""
    dest = os.path.join(UPLOAD_FOLDER, subdir)
    os.makedirs(dest, exist_ok=True)
    safe = secure_filename(f"{prefix}_{file.filename}")
    file.save(os.path.join(dest, safe))
    return f"{subdir}/{safe}"


# ─────────────────────────────────────────────
# Meeting token (HMAC)
# ─────────────────────────────────────────────

def meeting_token(meeting_id: int) -> str:
    key = SECRET_KEY.encode() if isinstance(SECRET_KEY, str) else SECRET_KEY
    return hmac.new(key, str(meeting_id).encode(), hashlib.sha256).hexdigest()[:20]


# ─────────────────────────────────────────────
# Phone normalizer
# ─────────────────────────────────────────────

def normalize_phone(phone: str) -> str:
    import re
    p = re.sub(r"[\s\-\(\)]", "", phone).strip()
    if not p:
        return ""
    if p.startswith("+"):
        return p
    if p.startswith("972"):
        return "+" + p
    if re.match(r"^0[5-9]\d{8}$", p):
        return "+972" + p[1:]
    return "+" + p


# ─────────────────────────────────────────────
# AI helpers
# ─────────────────────────────────────────────

def _ai_client():
    key = os.environ.get("AI_API_KEY")
    return OpenAI(api_key=key) if key else None


def _parse_json(raw: str) -> list:
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        if len(parts) >= 2:
            raw = parts[1].lstrip("json").lstrip("\n")
    try:
        return json.loads(raw)
    except Exception:
        return []


def ai_coaching_strategy(profile: StudentProfile) -> str:
    client = _ai_client()
    if not client:
        return ""
    edu   = "תיכון" if profile.education_level == "highschool" else "מכללה/אוניברסיטה"
    prompt = (
        f"אתה יועץ קריירה מומחה. פרופיל הסטודנט:\n"
        f"שם: {profile.full_name} | רמה: {edu} | שלב: {profile.current_occupation_or_grade}\n"
        f"מטרות: {profile.career_goals} | חששות: {profile.fears_weaknesses}\n\n"
        f"כתוב אסטרטגיית הדרכה (4-5 נקודות) עבור המנטור בעברית מקצועית."
    )
    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=700, temperature=0.7,
        )
        return r.choices[0].message.content.strip()
    except Exception:
        return ""


def ai_generate_tasks(profile: StudentProfile, count: int = 5) -> list:
    client = _ai_client()
    if not client:
        return []
    edu    = "תיכון" if profile.education_level == "highschool" else "מכללה/אוניברסיטה"
    prompt = (
        f"יועץ קריירה. צור {count} משימות מותאמות:\n"
        f"רמה: {edu} | שלב: {profile.current_occupation_or_grade} "
        f"| מטרות: {profile.career_goals} | חששות: {profile.fears_weaknesses}\n\n"
        f'החזר JSON בלבד: [{{"title":"...","description":"...","category":"קורות חיים|LinkedIn|הכנה לראיון|שאלון|כללי","task_type":"task|reflection|exercise"}}]'
    )
    try:
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000, temperature=0.8,
        )
        return _parse_json(r.choices[0].message.content)[:count]
    except Exception:
        return []


# ─────────────────────────────────────────────
# WhatsApp (Twilio)
# ─────────────────────────────────────────────

def send_whatsapp(phone: str, message: str) -> tuple[bool, str]:
    sid   = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_ = os.environ.get("TWILIO_WHATSAPP_FROM")
    if not all([sid, token, from_, phone]):
        return False, "no_config"
    try:
        from twilio.rest import Client
        to = f"whatsapp:{phone}" if not phone.startswith("whatsapp:") else phone
        Client(sid, token).messages.create(body=message, from_=from_, to=to)
        return True, ""
    except Exception as e:
        return False, str(e)


# ─────────────────────────────────────────────
# Serializers
# ─────────────────────────────────────────────

def _profile_dict(p: StudentProfile | None) -> dict:
    if not p:
        return {}
    return {
        "full_name": p.full_name, "email": p.email, "phone": p.phone,
        "education_level": p.education_level,
        "current_occupation_or_grade": p.current_occupation_or_grade,
        "career_goals": p.career_goals, "fears_weaknesses": p.fears_weaknesses,
        "ai_coaching_strategy": p.ai_coaching_strategy,
        "resume_content": p.resume_content,
        "process_start_date": p.process_start_date.isoformat() if p.process_start_date else None,
        "target_end_date":    p.target_end_date.isoformat()    if p.target_end_date    else None,
        "mentor_notes": p.mentor_notes,
    }


def _task_dict(t: TaskBank) -> dict:
    return {
        "id": t.id, "title": t.title, "description": t.description,
        "category": t.category, "task_type": t.task_type,
        "resource_file": t.resource_file,
    }


def _assignment_dict(at: AssignedTask) -> dict:
    return {
        "id": at.id, "task_id": at.task_id, "task": _task_dict(at.task),
        "status": at.status,
        "assigned_at":  at.assigned_at.isoformat()  if at.assigned_at  else None,
        "completed_at": at.completed_at.isoformat() if at.completed_at else None,
        "submission_note": at.submission_note, "submission_file": at.submission_file,
    }


def _meeting_dict(m: Meeting, include_token: bool = False) -> dict:
    s = db.session.get(User, m.student_id)
    p = s.profile if s else None
    d = {
        "id": m.id, "student_id": m.student_id,
        "student_name": (p.full_name if p and p.full_name else (s.username if s else "?")),
        "scheduled_at": m.scheduled_at.isoformat(),
        "duration_min": m.duration_min,
        "notes": m.notes, "status": m.status,
    }
    if include_token:
        d["token"] = meeting_token(m.id)
    return d


# ─────────────────────────────────────────────
# Routes — Health
# ─────────────────────────────────────────────

@app.route("/health")
def health():
    try:
        db.session.execute(db.text("SELECT 1"))
        db_status = "ok"
    except Exception:
        db_status = "error"
    return jsonify({"status": "ok", "db": db_status})


# ─────────────────────────────────────────────
# Routes — Auth
# ─────────────────────────────────────────────

@app.route("/api/auth/login", methods=["POST"])
def login():
    body     = request.get_json() or {}
    username = body.get("username", "").strip()
    password = body.get("password", "")
    user     = User.query.filter_by(username=username).first()
    if not user or not check_password_hash(user.password, password):
        return jsonify({"error": "שם משתמש או סיסמה שגויים"}), 401
    p    = user.profile
    name = (p.full_name if p and p.full_name else user.username)
    return jsonify({
        "token":   create_token(user.id, user.role),
        "user_id": user.id,
        "role":    user.role,
        "name":    name,
        "profile_complete": bool(p and p.education_level and p.career_goals and p.full_name),
    })


@app.route("/api/auth/me")
@require_auth
def me():
    user = db.session.get(User, request.user_id)
    if not user:
        return jsonify({"error": "Not found"}), 404
    return jsonify({
        "user_id":  user.id, "username": user.username, "role": user.role,
        "profile":  _profile_dict(user.profile),
        "profile_complete": bool(user.profile and user.profile.education_level
                                 and user.profile.career_goals and user.profile.full_name),
    })


@app.route("/api/auth/onboarding", methods=["POST"])
@require_auth
def save_onboarding():
    user = db.session.get(User, request.user_id)
    body = request.get_json() or {}

    is_new  = user.profile is None
    profile = user.profile or StudentProfile(user_id=user.id)

    for field in ("full_name", "email", "career_goals", "fears_weaknesses",
                  "education_level", "current_occupation_or_grade"):
        if field in body:
            setattr(profile, field, body[field])

    if "phone" in body:
        profile.phone = normalize_phone(body["phone"])

    if not (profile.full_name and profile.education_level and profile.career_goals):
        return jsonify({"error": "Missing required fields"}), 400

    if is_new or not profile.process_start_date:
        profile.process_start_date = date.today()

    profile.ai_coaching_strategy = ai_coaching_strategy(profile)

    if is_new:
        db.session.add(profile)
    db.session.commit()
    return jsonify({"ok": True, "profile": _profile_dict(profile)})


# ─────────────────────────────────────────────
# Routes — Students (admin)
# ─────────────────────────────────────────────

@app.route("/api/students")
@require_admin
def list_students():
    students = User.query.filter_by(role="student").order_by(User.username).all()
    result = []
    for s in students:
        total = AssignedTask.query.filter_by(user_id=s.id).count()
        done  = AssignedTask.query.filter_by(user_id=s.id, status="completed").count()
        result.append({
            "id": s.id, "username": s.username,
            "profile": _profile_dict(s.profile),
            "progress": {"total": total, "done": done,
                         "pct": round(done / total * 100) if total else 0},
        })
    return jsonify(result)


@app.route("/api/students", methods=["POST"])
@require_admin
def create_student():
    body      = request.get_json() or {}
    username  = body.get("username", "").strip()
    password  = body.get("password", "").strip()
    full_name = body.get("full_name", "").strip()
    phone     = body.get("phone", "").strip()

    if not username or not password:
        return jsonify({"error": "username and password required"}), 400
    if len(password) < 6:
        return jsonify({"error": "password must be at least 6 characters"}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({"error": f"Username '{username}' already taken"}), 409

    user = User(username=username, password=generate_password_hash(password), role="student")
    db.session.add(user)
    db.session.flush()
    if full_name or phone:
        db.session.add(StudentProfile(
            user_id=user.id, full_name=full_name,
            phone=normalize_phone(phone) if phone else "",
        ))
    db.session.commit()
    return jsonify({"id": user.id, "username": user.username}), 201


@app.route("/api/students/<int:sid>")
@require_admin
def get_student(sid):
    student = User.query.filter_by(id=sid, role="student").first_or_404()
    active    = [_assignment_dict(a) for a in
                 AssignedTask.query.filter_by(user_id=sid, status="pending").all()]
    completed = [_assignment_dict(a) for a in
                 AssignedTask.query.filter_by(user_id=sid, status="completed").all()]
    meetings  = [_meeting_dict(m) for m in
                 Meeting.query.filter_by(student_id=sid)
                 .filter(Meeting.scheduled_at >= datetime.utcnow())
                 .filter(Meeting.status != "cancelled")
                 .order_by(Meeting.scheduled_at).all()]
    assigned_ids = [a.task_id for a in AssignedTask.query.filter_by(user_id=sid).all()]
    return jsonify({
        "id": student.id, "username": student.username,
        "profile": _profile_dict(student.profile),
        "active": active, "completed": completed,
        "meetings": meetings, "assigned_ids": assigned_ids,
    })


@app.route("/api/students/<int:sid>/profile", methods=["PATCH"])
@require_admin
def update_student_profile(sid):
    student = User.query.filter_by(id=sid, role="student").first_or_404()
    p       = student.profile
    if not p:
        return jsonify({"error": "No profile yet"}), 404
    body = request.get_json() or {}
    for field in ("process_start_date", "target_end_date"):
        if field in body:
            val = body[field]
            setattr(p, field, date.fromisoformat(val) if val else None)
    if "mentor_notes" in body:
        p.mentor_notes = body["mentor_notes"]
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/students/<int:sid>/resume", methods=["PATCH"])
@require_admin
def update_resume(sid):
    student = User.query.filter_by(id=sid, role="student").first_or_404()
    p = student.profile
    if not p:
        return jsonify({"error": "No profile"}), 404
    p.resume_content = (request.get_json() or {}).get("resume_content", "")
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/students/<int:sid>/assignments", methods=["POST"])
@require_admin
def assign_tasks(sid):
    student  = User.query.filter_by(id=sid, role="student").first_or_404()
    body     = request.get_json() or {}
    task_ids = body.get("task_ids", [])
    existing = {at.task_id: at for at in AssignedTask.query.filter_by(user_id=sid).all()}

    for tid, at in existing.items():
        if tid not in task_ids and at.status == "pending":
            db.session.delete(at)

    new_ids = [tid for tid in task_ids if tid not in existing]
    for tid in new_ids:
        db.session.add(AssignedTask(user_id=sid, task_id=tid))
    db.session.commit()

    notified = False
    p = student.profile
    if new_ids and p and p.phone:
        name = p.full_name or student.username
        ok, _ = send_whatsapp(p.phone,
            f"שלום {name}! המנטור שלך הוסיף {len(new_ids)} משימות חדשות. היכנס/י למערכת. 📋")
        notified = ok

    return jsonify({"assigned": len(new_ids), "notified": notified})


# ─────────────────────────────────────────────
# Routes — AI
# ─────────────────────────────────────────────

@app.route("/api/ai/coaching-strategy/<int:sid>", methods=["POST"])
@require_admin
def regen_strategy(sid):
    student = User.query.filter_by(id=sid, role="student").first_or_404()
    p = student.profile
    if not p:
        return jsonify({"error": "No profile"}), 404
    p.ai_coaching_strategy = ai_coaching_strategy(p)
    db.session.commit()
    return jsonify({"strategy": p.ai_coaching_strategy})


@app.route("/api/ai/tasks/<int:sid>", methods=["POST"])
@require_admin
def gen_ai_tasks(sid):
    student = User.query.filter_by(id=sid, role="student").first_or_404()
    p = student.profile
    if not p:
        return jsonify({"error": "No profile"}), 404
    suggestions = ai_generate_tasks(p, count=5)
    if not suggestions:
        return jsonify({"error": "AI unavailable or no API key"}), 503

    created = []
    for s in suggestions:
        title = s.get("title", "").strip()
        if not title:
            continue
        task = TaskBank(title=title, description=s.get("description", ""),
                        category=s.get("category", "כללי"), task_type=s.get("task_type", "task"))
        db.session.add(task)
        db.session.flush()
        db.session.add(AssignedTask(user_id=sid, task_id=task.id))
        created.append(_task_dict(task))

    db.session.commit()

    p2 = student.profile
    if p2 and p2.phone and created:
        name = p2.full_name or student.username
        send_whatsapp(p2.phone,
            f"שלום {name}! המנטור יצר {len(created)} משימות AI חדשות. 🤖")

    return jsonify({"created": len(created), "tasks": created})


# ─────────────────────────────────────────────
# Routes — Task Bank
# ─────────────────────────────────────────────

@app.route("/api/tasks")
@require_auth
def list_tasks():
    tasks = TaskBank.query.order_by(TaskBank.category, TaskBank.title).all()
    return jsonify([_task_dict(t) for t in tasks])


@app.route("/api/tasks", methods=["POST"])
@require_admin
def create_task():
    body = request.get_json() or {}
    title = body.get("title", "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    t = TaskBank(title=title, description=body.get("description", ""),
                 category=body.get("category", "כללי"), task_type=body.get("task_type", "task"))
    db.session.add(t)
    db.session.commit()
    return jsonify(_task_dict(t)), 201


@app.route("/api/tasks/<int:tid>", methods=["PATCH"])
@require_admin
def update_task(tid):
    t    = TaskBank.query.get_or_404(tid)
    body = request.get_json() or {}
    for field in ("title", "description", "category", "task_type"):
        if field in body:
            setattr(t, field, body[field])
    if body.get("clear_resource"):
        t.resource_file = ""
    db.session.commit()
    return jsonify(_task_dict(t))


@app.route("/api/tasks/<int:tid>", methods=["DELETE"])
@require_admin
def delete_task(tid):
    t = TaskBank.query.get_or_404(tid)
    AssignedTask.query.filter_by(task_id=tid).delete()
    db.session.delete(t)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/tasks/<int:tid>/resource", methods=["POST"])
@require_admin
def upload_task_resource(tid):
    t    = TaskBank.query.get_or_404(tid)
    file = request.files.get("file")
    path = _save_file(file, "tasks", f"resource_{tid}")
    if path:
        t.resource_file = path
        db.session.commit()
        return jsonify({"path": path})
    return jsonify({"error": "Invalid or missing file"}), 400


# ─────────────────────────────────────────────
# Routes — Student (own tasks)
# ─────────────────────────────────────────────

@app.route("/api/my/tasks")
@require_auth
def my_tasks():
    uid = request.user_id
    active    = [_assignment_dict(a) for a in
                 AssignedTask.query.filter_by(user_id=uid, status="pending").all()]
    completed = [_assignment_dict(a) for a in
                 AssignedTask.query.filter_by(user_id=uid, status="completed")
                 .order_by(AssignedTask.completed_at.desc()).all()]
    upcoming  = [_meeting_dict(m) for m in
                 Meeting.query.filter_by(student_id=uid)
                 .filter(Meeting.scheduled_at >= datetime.utcnow())
                 .filter(Meeting.status != "cancelled")
                 .order_by(Meeting.scheduled_at).limit(3).all()]
    return jsonify({
        "active": active, "completed": completed,
        "total": len(active) + len(completed),
        "upcoming_meetings": upcoming,
    })


@app.route("/api/my/tasks/<int:tid>/submit", methods=["POST"])
@require_auth
def submit_task(tid):
    at = AssignedTask.query.filter_by(
        user_id=request.user_id, task_id=tid).first_or_404()
    if at.status == "pending":
        at.status           = "completed"
        at.completed_at     = datetime.utcnow()
        at.submission_note  = request.form.get("submission_note", "")
        file = request.files.get("submission_file")
        if file and file.filename:
            path = _save_file(file, str(request.user_id), f"task_{tid}")
            if path:
                at.submission_file = path
        db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/my/meetings")
@require_auth
def my_meetings():
    meetings = (Meeting.query.filter_by(student_id=request.user_id)
                .filter(Meeting.status != "cancelled")
                .order_by(Meeting.scheduled_at).all())
    return jsonify([_meeting_dict(m, include_token=True) for m in meetings])


# ─────────────────────────────────────────────
# Routes — Meetings (admin)
# ─────────────────────────────────────────────

@app.route("/api/meetings")
@require_admin
def list_meetings():
    year  = request.args.get("year",  type=int, default=date.today().year)
    month = request.args.get("month", type=int, default=date.today().month)
    year  = max(2020, min(2035, year))
    month = max(1,    min(12,   month))

    prev_year  = year - 1 if month == 1 else year
    prev_month = 12        if month == 1 else month - 1
    next_year  = year + 1  if month == 12 else year
    next_month = 1         if month == 12 else month + 1

    from_dt = datetime(year, month, 1)
    to_dt   = datetime(next_year, next_month, 1)

    month_meetings = (Meeting.query
                      .filter(Meeting.scheduled_at >= from_dt,
                              Meeting.scheduled_at <  to_dt,
                              Meeting.status != "cancelled")
                      .order_by(Meeting.scheduled_at).all())

    by_day: dict = {}
    for m in month_meetings:
        s = db.session.get(User, m.student_id)
        p = s.profile if s else None
        name = (p.full_name if p and p.full_name else (s.username if s else "?"))
        by_day.setdefault(m.scheduled_at.day, []).append({
            "id": m.id, "name": name,
            "time": m.scheduled_at.strftime("%H:%M"),
            "duration": m.duration_min,
            "status": m.status, "notes": m.notes or "",
        })

    cal_weeks = _cal.Calendar(firstweekday=6).monthdayscalendar(year, month)

    MONTHS_HE = {1:"ינואר",2:"פברואר",3:"מרץ",4:"אפריל",5:"מאי",6:"יוני",
                 7:"יולי",8:"אוגוסט",9:"ספטמבר",10:"אוקטובר",11:"נובמבר",12:"דצמבר"}

    upcoming = [_meeting_dict(m) for m in
                Meeting.query
                .filter(Meeting.scheduled_at >= datetime.utcnow())
                .filter(Meeting.status != "cancelled")
                .order_by(Meeting.scheduled_at).limit(20).all()]

    students = [{"id": s.id,
                 "name": (s.profile.full_name if s.profile and s.profile.full_name else s.username)}
                for s in User.query.filter_by(role="student").order_by(User.username).all()]

    return jsonify({
        "cal_weeks": cal_weeks, "meetings_by_day": by_day,
        "year": year, "month": month, "month_name": MONTHS_HE[month],
        "prev_year": prev_year, "prev_month": prev_month,
        "next_year": next_year, "next_month": next_month,
        "upcoming": upcoming, "students": students,
        "today": date.today().isoformat(),
    })


@app.route("/api/meetings", methods=["POST"])
@require_admin
def create_meeting():
    body = request.get_json() or {}
    sid  = body.get("student_id")
    sched_str = body.get("scheduled_at", "")
    if not sid or not sched_str:
        return jsonify({"error": "student_id and scheduled_at required"}), 400
    try:
        scheduled_at = datetime.fromisoformat(sched_str)
    except ValueError:
        return jsonify({"error": "Invalid datetime format"}), 400

    student = User.query.filter_by(id=sid, role="student").first_or_404()
    m = Meeting(student_id=sid, scheduled_at=scheduled_at,
                duration_min=body.get("duration_min", 60), notes=body.get("notes", ""))
    db.session.add(m)
    db.session.commit()

    notified = False
    p = student.profile
    if p and p.phone:
        token = meeting_token(m.id)
        # In a real deployment, construct URL from env var FRONTEND_URL
        frontend_url = os.environ.get("FRONTEND_URL", "http://localhost:5000")
        confirm_url  = f"{frontend_url}/meeting/{m.id}/confirm?token={token}"
        dt_str = scheduled_at.strftime("%d/%m/%Y בשעה %H:%M")
        name   = p.full_name or student.username
        ok, _ = send_whatsapp(p.phone,
            f"שלום {name}! נקבעה פגישה ביום {dt_str}.\nלאישור: {confirm_url}")
        notified = ok

    return jsonify({"id": m.id, "notified": notified}), 201


@app.route("/api/meetings/<int:mid>", methods=["PATCH"])
@require_admin
def update_meeting(mid):
    m      = Meeting.query.get_or_404(mid)
    body   = request.get_json() or {}
    action = body.get("action")

    if action == "cancel":
        m.status = "cancelled"
        db.session.commit()
        student = db.session.get(User, m.student_id)
        p = student.profile if student else None
        if p and p.phone:
            dt_str = m.scheduled_at.strftime("%d/%m/%Y %H:%M")
            name   = p.full_name or student.username
            send_whatsapp(p.phone, f"שלום {name}! הפגישה ב-{dt_str} בוטלה. נדבר בקרוב.")
        return jsonify({"ok": True})

    if action == "send_reminder":
        student = db.session.get(User, m.student_id)
        p = student.profile if student else None
        if not p or not p.phone:
            return jsonify({"error": "no_phone", "message": "אין מספר טלפון"}), 400
        dt_str = m.scheduled_at.strftime("%d/%m/%Y בשעה %H:%M")
        name   = p.full_name or student.username
        ok, reason = send_whatsapp(p.phone,
            f"תזכורת 📅 שלום {name}! פגישה ב-{dt_str}. להתראות!")
        if ok:
            return jsonify({"ok": True})
        return jsonify({"error": reason}), 400 if reason == "no_config" else 500

    return jsonify({"error": "Unknown action"}), 400


# ─────────────────────────────────────────────
# Routes — Meeting Confirmation (public)
# ─────────────────────────────────────────────

@app.route("/api/meetings/<int:mid>/confirm")
def confirm_meeting(mid):
    m     = Meeting.query.get_or_404(mid)
    token = request.args.get("token", "")
    if token != meeting_token(mid):
        return jsonify({"error": "Invalid token"}), 403

    already = (m.status == "confirmed")
    if not already and m.status == "pending":
        m.status = "confirmed"
        db.session.commit()
        student = db.session.get(User, m.student_id)
        p = student.profile if student else None
        if p and p.phone:
            dt_str = m.scheduled_at.strftime("%d/%m/%Y בשעה %H:%M")
            name   = p.full_name or student.username
            send_whatsapp(p.phone, f"✅ {name}, הפגישה ב-{dt_str} אושרה!")

    return jsonify({
        "already_confirmed": already or m.status == "confirmed",
        "meeting": {
            "scheduled_at": m.scheduled_at.isoformat(),
            "duration_min": m.duration_min, "notes": m.notes,
        },
    })


# ─────────────────────────────────────────────
# Routes — File Serving
# ─────────────────────────────────────────────

@app.route("/api/files/<path:filepath>")
@require_auth
def serve_file(filepath):
    directory = UPLOAD_FOLDER
    return send_from_directory(directory, filepath)


# ─────────────────────────────────────────────
# Seed & Init
# ─────────────────────────────────────────────

def seed_db():
    if not User.query.filter_by(username="admin").first():
        db.session.add(User(username="admin",
                            password=generate_password_hash("admin123"), role="admin"))
    if not User.query.filter_by(username="student1").first():
        db.session.add(User(username="student1",
                            password=generate_password_hash("student123"), role="student"))
    if TaskBank.query.count() == 0:
        starters = [
            TaskBank(title="עדכון קורות חיים", description="עיון ועדכון קורות החיים לפי הנחיות המנטור.", category="קורות חיים", task_type="task"),
            TaskBank(title="כתיבת סיכום מקצועי", description="כתיבת Professional Summary בראש קורות החיים.", category="קורות חיים", task_type="exercise"),
            TaskBank(title="הכנת פרופיל LinkedIn", description="יצירה ומילוי מלא של פרופיל LinkedIn.", category="LinkedIn", task_type="task"),
            TaskBank(title='כתיבת קטע "About" ב-LinkedIn', description="כתיבת קטע About ייחודי ומושך.", category="LinkedIn", task_type="exercise"),
            TaskBank(title="הכנה לשאלות HR נפוצות", description="חקור ותרגל תשובות ל-10 שאלות HR.", category="הכנה לראיון", task_type="task"),
            TaskBank(title='תרגול הצגה עצמית — "ספר/י על עצמך"', description="תרגול הצגה עצמית ממוקדת של דקה.", category="הכנה לראיון", task_type="exercise"),
            TaskBank(title="שאלות לשאול בסיום ראיון", description="הכן/י 5 שאלות חכמות לשאול המראיין.", category="הכנה לראיון", task_type="task"),
            TaskBank(title="הגדרת מטרות לחודש הקרוב", description="מה אני רוצה להשיג בחיפוש העבודה?", category="שאלון", task_type="reflection"),
            TaskBank(title="הכישורים הייחודיים שלי", description="כתוב/י 5 כישורים עם דוגמה לכל אחד.", category="שאלון", task_type="reflection"),
            TaskBank(title="מחקר חברות יעד", description="זהה 5 חברות ומצא/י פרטים על כל אחת.", category="כללי", task_type="task"),
        ]
        db.session.add_all(starters)
    db.session.commit()


with app.app_context():
    db.create_all()
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(os.path.join(UPLOAD_FOLDER, "tasks"), exist_ok=True)
    seed_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000,
            debug=os.environ.get("FLASK_DEBUG", "false").lower() == "true")
