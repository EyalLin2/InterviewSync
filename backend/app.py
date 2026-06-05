import os
import json
import hmac
import hashlib
import calendar as _cal
from datetime import datetime, date, timedelta
from functools import wraps

import jwt
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from groq import Groq

from models import (db, get_database_url,
                    User, StudentProfile, TaskBank, AssignedTask, Meeting,
                    MentorNote,
                    Workshop, Inquiry, ActivityLog, TOPIC_CATEGORIES,
                    Service, StudentBilling, BillingRecord)

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
        "sub":  str(user_id),   # JWT spec requires string subject
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
        request.user_id = int(payload["sub"])   # convert back to int
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
    """Return configured Groq client or None if no key."""
    key = os.environ.get("GROQ_API_KEY") or os.environ.get("AI_API_KEY")
    if not key:
        return None
    return Groq(api_key=key)


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

    lvl = profile.education_level or "college"
    if lvl == "highschool":
        context = (
            f"תלמיד/ה תיכון, כיתה {profile.current_occupation_or_grade}. "
            f"תחומי עניין: {profile.interests_hobbies or 'לא צוין'}. "
            f"מטרות: {profile.career_goals}. חששות: {profile.fears_weaknesses or 'לא צוין'}."
        )
        focus = "הכנה ראשונה לשוק העבודה, בניית ניסיון ראשוני, LinkedIn, כישורים רכים"
    elif lvl == "college":
        context = (
            f"סטודנט/ית — {profile.current_occupation_or_grade} "
            f"ב-{profile.institution_name or 'מוסד לא צוין'}"
            f"{f', שנת סיום {profile.graduation_year}' if profile.graduation_year else ''}. "
            f"מטרות: {profile.career_goals}. חששות: {profile.fears_weaknesses or 'לא צוין'}."
        )
        focus = "קורות חיים, LinkedIn, הכנה לראיונות, הגעה לתפקיד ראשון בתחום"
    else:  # career
        context = (
            f"מחפש/ת הכוונה תעסוקתית. תפקיד נוכחי/קודם: {profile.current_job or 'לא צוין'}. "
            f"ניסיון: {profile.years_experience or '?'} שנים. "
            f"סיבה לפנייה: {profile.reason_for_guidance or 'לא צוין'}. "
            f"מטרות: {profile.career_goals}. חששות: {profile.fears_weaknesses or 'לא צוין'}."
        )
        focus = "מיתוג מקצועי, אסטרטגיית חיפוש עבודה, קורות חיים, LinkedIn, ניהול מעבר קריירה"

    prompt = (
        f"אתה יועץ קריירה מומחה. פרופיל:\n{context}\n\n"
        f"תחומי מיקוד רלוונטיים: {focus}\n\n"
        f"כתוב אסטרטגיית הדרכה (4-5 נקודות) מותאמת אישית עבור המנטור בעברית מקצועית."
    )
    try:
        r = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=700,
        )
        return r.choices[0].message.content.strip()
    except Exception as e:
        app.logger.error("Groq coaching strategy error: %s", e)
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
        f'החזר JSON בלבד (ללא מרקדאון): [{{"title":"...","description":"...","category":"קורות חיים|LinkedIn|הכנה לראיון|שאלון|כללי","task_type":"task|reflection|exercise"}}]'
    )
    try:
        r = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000,
        )
        return _parse_json(r.choices[0].message.content)[:count]
    except Exception as e:
        app.logger.error("Groq generate tasks error: %s", e)
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
        "mentor_notes":   p.mentor_notes,
        "resume_file":    p.resume_file or "",
        "student_status": p.student_status or "active",
        "ai_strategy_updated_at": p.ai_strategy_updated_at.isoformat() if p.ai_strategy_updated_at else None,
        # type-specific fields
        "interests_hobbies":    p.interests_hobbies or "",
        "institution_name":     p.institution_name or "",
        "graduation_year":      p.graduation_year,
        "current_job":          p.current_job or "",
        "years_experience":     p.years_experience,
        "reason_for_guidance":  p.reason_for_guidance or "",
    }


def _task_dict(t: TaskBank) -> dict:
    assigned  = len(t.assignments)
    completed = sum(1 for a in t.assignments if a.status == "completed")
    rate = round(completed / assigned * 100) if assigned else 0
    return {
        "id": t.id, "title": t.title, "description": t.description,
        "category": t.category, "task_type": t.task_type,
        "resource_file": t.resource_file,
        "assigned_count":  assigned,
        "completed_count": completed,
        "completion_rate": rate,
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
        "meeting_type": m.meeting_type or "progress_review",
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
                  "education_level", "current_occupation_or_grade",
                  "interests_hobbies", "institution_name",
                  "current_job", "reason_for_guidance"):
        if field in body:
            setattr(profile, field, body[field])
    for int_field in ("graduation_year", "years_experience"):
        if int_field in body and body[int_field] is not None:
            try:
                setattr(profile, int_field, int(body[int_field]))
            except (ValueError, TypeError):
                pass

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
    now = datetime.utcnow()
    result = []
    for s in students:
        total = AssignedTask.query.filter_by(user_id=s.id).count()
        done  = AssignedTask.query.filter_by(user_id=s.id, status="completed").count()

        # last activity = max of last task completion OR last confirmed meeting
        last_task = (AssignedTask.query.filter_by(user_id=s.id, status="completed")
                     .order_by(AssignedTask.completed_at.desc()).first())
        last_meeting = (Meeting.query.filter_by(student_id=s.id)
                        .filter(Meeting.status.in_(["confirmed", "completed"]))
                        .filter(Meeting.scheduled_at <= now)
                        .order_by(Meeting.scheduled_at.desc()).first())
        dates = [d for d in [
            last_task.completed_at    if last_task    else None,
            last_meeting.scheduled_at if last_meeting else None,
        ] if d]
        last_activity_dt = max(dates) if dates else None
        last_activity_days = (now - last_activity_dt).days if last_activity_dt else None

        result.append({
            "id": s.id, "username": s.username,
            "profile": _profile_dict(s.profile),
            "progress": {"total": total, "done": done,
                         "pct": round(done / total * 100) if total else 0},
            "last_activity_days": last_activity_days,
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
    p = student.profile
    if not p:
        # Create profile on first admin PATCH
        p = StudentProfile(user_id=sid)
        db.session.add(p)
    body = request.get_json() or {}
    # Date fields
    for field in ("process_start_date", "target_end_date"):
        if field in body:
            val = body[field]
            setattr(p, field, date.fromisoformat(val) if val else None)
    # Text fields editable by mentor
    for field in ("mentor_notes", "student_status",
                  "career_goals", "fears_weaknesses", "full_name",
                  "email", "phone", "education_level", "current_occupation_or_grade"):
        if field in body:
            setattr(p, field, body[field])
    db.session.commit()
    return jsonify({"ok": True, "profile": _profile_dict(p)})


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


@app.route("/api/students/<int:sid>/notes", methods=["GET"])
@require_admin
def list_notes(sid):
    User.query.filter_by(id=sid, role="student").first_or_404()
    notes = (MentorNote.query.filter_by(student_id=sid)
             .order_by(MentorNote.created_at.desc()).all())
    return jsonify([{
        "id": n.id, "text": n.text,
        "created_at": n.created_at.isoformat(),
    } for n in notes])


@app.route("/api/students/<int:sid>/notes", methods=["POST"])
@require_admin
def add_note(sid):
    User.query.filter_by(id=sid, role="student").first_or_404()
    text = (request.get_json() or {}).get("text", "").strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    n = MentorNote(student_id=sid, text=text)
    db.session.add(n)
    db.session.commit()
    return jsonify({"id": n.id, "text": n.text,
                    "created_at": n.created_at.isoformat()}), 201


@app.route("/api/notes/<int:nid>", methods=["DELETE"])
@require_admin
def delete_note(nid):
    n = MentorNote.query.get_or_404(nid)
    db.session.delete(n)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/students/<int:sid>/cv", methods=["POST"])
@require_admin
def upload_student_cv(sid):
    """Upload a CV file for the student."""
    student = User.query.filter_by(id=sid, role="student").first_or_404()
    p = student.profile
    if not p:
        return jsonify({"error": "No profile"}), 404
    file = request.files.get("cv_file")
    path = _save_file(file, str(sid), "cv")
    if not path:
        return jsonify({"error": "Invalid or missing file (PDF/DOC only)"}), 400
    p.resume_file = path
    db.session.commit()
    return jsonify({"ok": True, "path": path})


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
    if not _ai_client():
        return jsonify({"error": "no_api_key",
                        "message": "GROQ_API_KEY לא מוגדר — הוסף אותו ל-docker-compose.yml ומפתח חינמי: aistudio.google.com/apikey",
                        "strategy": p.ai_coaching_strategy or ""}), 503
    result = ai_coaching_strategy(p)
    if result:
        p.ai_coaching_strategy = result
        p.ai_strategy_updated_at = datetime.utcnow()
        db.session.commit()
    return jsonify({"strategy": p.ai_coaching_strategy or "",
                    "updated_at": p.ai_strategy_updated_at.isoformat() if p.ai_strategy_updated_at else None})


@app.route("/api/ai/tasks/<int:sid>/suggest", methods=["POST"])
@require_admin
def suggest_ai_tasks(sid):
    """Generate AI task suggestions WITHOUT saving — admin reviews first."""
    student = User.query.filter_by(id=sid, role="student").first_or_404()
    p = student.profile
    if not p:
        return jsonify({"error": "No profile"}), 404
    suggestions = ai_generate_tasks(p, count=5)
    if not suggestions:
        return jsonify({"error": "AI unavailable or no API key"}), 503
    return jsonify({"suggestions": suggestions})


@app.route("/api/ai/tasks/<int:sid>/confirm", methods=["POST"])
@require_admin
def confirm_ai_tasks(sid):
    """Save selected AI suggestions to task bank and assign to student."""
    student = User.query.filter_by(id=sid, role="student").first_or_404()
    p = student.profile
    body      = request.get_json() or {}
    tasks     = body.get("tasks", [])  # list of {title, description, category, task_type}
    assign    = body.get("assign", False)

    created = []
    for s in tasks:
        title = s.get("title", "").strip()
        if not title:
            continue
        task = TaskBank(title=title, description=s.get("description", ""),
                        category=s.get("category", "כללי"), task_type=s.get("task_type", "task"))
        db.session.add(task)
        db.session.flush()
        if assign:
            db.session.add(AssignedTask(user_id=sid, task_id=task.id))
        created.append(_task_dict(task))

    db.session.commit()

    if assign and p and p.phone and created:
        name = p.full_name or student.username
        send_whatsapp(p.phone, f"שלום {name}! המנטור הוסיף {len(created)} משימות חדשות. 📋")

    return jsonify({"created": len(created), "tasks": created, "assigned": assign})


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


@app.route("/api/admin/assignments/<int:at_id>/complete", methods=["PATCH"])
@require_admin
def admin_complete_task(at_id):
    """Admin marks a student's task as completed on their behalf."""
    at = AssignedTask.query.get_or_404(at_id)
    note = (request.get_json() or {}).get("note", "")
    at.status       = "completed"
    at.completed_at = datetime.utcnow()
    if note:
        at.submission_note = note
    db.session.commit()
    return jsonify({"ok": True, "completed_at": at.completed_at.isoformat()})


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
        edu = (p.education_level or "college") if p else "college"
        by_day.setdefault(m.scheduled_at.day, []).append({
            "id": m.id, "name": name,
            "time": m.scheduled_at.strftime("%H:%M"),
            "duration": m.duration_min,
            "status": m.status, "notes": m.notes or "",
            "meeting_type":     m.meeting_type or "progress_review",
            "education_level":  edu,  # for color coding
        })

    # Add business workshops to the calendar
    month_workshops = (Workshop.query
                       .filter(Workshop.scheduled_at >= from_dt,
                               Workshop.scheduled_at <  to_dt,
                               Workshop.status != "cancelled")
                       .order_by(Workshop.scheduled_at).all())
    for w in month_workshops:
        by_day.setdefault(w.scheduled_at.day, []).append({
            "id": w.id, "name": w.title,
            "time": w.scheduled_at.strftime("%H:%M"),
            "duration": w.duration_min if hasattr(w, "duration_min") else 60,
            "status": w.status, "notes": w.notes or "",
            "event_type": "workshop",         # distinguish from meeting
            "meeting_type": "workshop",
            "education_level": "workshop",    # special color
            "workshop_type":  w.workshop_type or "one_time",
            "topic":          w.topic_category or "",
        })

    # Upcoming workshops for sidebar
    upcoming_workshops = [
        {"id": w.id, "name": w.title,
         "scheduled_at": w.scheduled_at.isoformat(),
         "duration_min": 60,
         "status": w.status, "notes": w.notes or "",
         "event_type": "workshop",
         "topic": w.topic_category or ""}
        for w in Workshop.query
            .filter(Workshop.scheduled_at >= datetime.utcnow())
            .filter(Workshop.status != "cancelled")
            .order_by(Workshop.scheduled_at).limit(5).all()
    ]

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
        "upcoming": upcoming,
        "upcoming_workshops": upcoming_workshops,
        "students": students,
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
                duration_min=body.get("duration_min", 60),
                notes=body.get("notes", ""),
                meeting_type=body.get("meeting_type", "progress_review"))
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

    if action == "mark_completed":
        m.status = "completed"
        db.session.commit()
        return jsonify({"ok": True})

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
# Routes — Business (admin only)
# ─────────────────────────────────────────────

def _workshop_dict(w: Workshop) -> dict:
    return {
        "id": w.id, "title": w.title, "description": w.description,
        "topic_category": w.topic_category, "workshop_type": w.workshop_type,
        "status": w.status,
        "scheduled_at": w.scheduled_at.isoformat() if w.scheduled_at else None,
        "location": w.location, "max_participants": w.max_participants,
        "notes": w.notes,
        "inquiries_count": len(w.inquiries),
        "created_at": w.created_at.isoformat(),
    }


def _inquiry_dict(i: Inquiry) -> dict:
    return {
        "id": i.id, "full_name": i.full_name, "phone": i.phone, "email": i.email,
        "topic": i.topic, "source": i.source, "notes": i.notes, "status": i.status,
        "workshop_id": i.workshop_id,
        "workshop_title": i.workshop.title if i.workshop else None,
        "created_at": i.created_at.isoformat(),
    }


def _activity_dict(a: ActivityLog) -> dict:
    return {
        "id": a.id, "title": a.title, "activity_type": a.activity_type,
        "topic_category": a.topic_category,
        "activity_date": a.activity_date.isoformat(),
        "duration_min": a.duration_min,
        "participants_count": a.participants_count,
        "description": a.description, "workshop_id": a.workshop_id,
        "workshop_title": a.workshop.title if a.workshop else None,
        "created_at": a.created_at.isoformat(),
    }


@app.route("/api/business/overview")
@require_admin
def business_overview():
    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    workshops_total  = Workshop.query.count()
    workshops_month  = Workshop.query.filter(Workshop.created_at >= month_start).count()
    inquiries_new    = Inquiry.query.filter_by(status="new").count()
    inquiries_total  = Inquiry.query.count()
    activities_month = ActivityLog.query.filter(ActivityLog.activity_date >= month_start.date()).count()

    upcoming = (Workshop.query
                .filter(Workshop.scheduled_at >= now, Workshop.status != "cancelled")
                .order_by(Workshop.scheduled_at).limit(3).all())
    recent_inquiries = Inquiry.query.order_by(Inquiry.created_at.desc()).limit(5).all()

    return jsonify({
        "workshops_total":    workshops_total,
        "workshops_this_month": workshops_month,
        "inquiries_new":      inquiries_new,
        "inquiries_total":    inquiries_total,
        "activities_this_month": activities_month,
        "upcoming_workshops": [_workshop_dict(w) for w in upcoming],
        "recent_inquiries":   [_inquiry_dict(i) for i in recent_inquiries],
        "topic_categories":   TOPIC_CATEGORIES,
    })


# ── Workshops ──────────────────────────────────────────────────────────────

@app.route("/api/workshops")
@require_admin
def list_workshops():
    ws = Workshop.query.order_by(Workshop.created_at.desc()).all()
    return jsonify([_workshop_dict(w) for w in ws])


@app.route("/api/workshops", methods=["POST"])
@require_admin
def create_workshop():
    body = request.get_json() or {}
    title = body.get("title", "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400

    sched = None
    if body.get("scheduled_at"):
        try:
            sched = datetime.fromisoformat(body["scheduled_at"])
        except ValueError:
            return jsonify({"error": "Invalid scheduled_at format"}), 400

    w = Workshop(
        title=title,
        description=body.get("description", ""),
        topic_category=body.get("topic_category", "כללי"),
        workshop_type=body.get("workshop_type", "one_time"),
        status=body.get("status", "planned"),
        scheduled_at=sched,
        location=body.get("location", ""),
        max_participants=body.get("max_participants"),
        notes=body.get("notes", ""),
    )
    db.session.add(w)
    db.session.commit()
    return jsonify(_workshop_dict(w)), 201


@app.route("/api/workshops/<int:wid>", methods=["PATCH"])
@require_admin
def update_workshop(wid):
    w    = Workshop.query.get_or_404(wid)
    body = request.get_json() or {}
    for field in ("title", "description", "topic_category", "workshop_type",
                  "status", "location", "notes"):
        if field in body:
            setattr(w, field, body[field])
    if "max_participants" in body:
        w.max_participants = body["max_participants"]
    if "scheduled_at" in body:
        val = body["scheduled_at"]
        w.scheduled_at = datetime.fromisoformat(val) if val else None
    db.session.commit()
    return jsonify(_workshop_dict(w))


@app.route("/api/workshops/<int:wid>", methods=["DELETE"])
@require_admin
def delete_workshop(wid):
    w = Workshop.query.get_or_404(wid)
    # Unlink inquiries and activities before deleting
    Inquiry.query.filter_by(workshop_id=wid).update({"workshop_id": None})
    ActivityLog.query.filter_by(workshop_id=wid).update({"workshop_id": None})
    db.session.delete(w)
    db.session.commit()
    return jsonify({"ok": True})


# ── Inquiries ──────────────────────────────────────────────────────────────

@app.route("/api/inquiries")
@require_admin
def list_inquiries():
    status_filter = request.args.get("status")
    q = Inquiry.query.order_by(Inquiry.created_at.desc())
    if status_filter:
        q = q.filter_by(status=status_filter)
    return jsonify([_inquiry_dict(i) for i in q.all()])


@app.route("/api/inquiries", methods=["POST"])
@require_admin
def create_inquiry():
    body = request.get_json() or {}
    name = body.get("full_name", "").strip()
    if not name:
        return jsonify({"error": "full_name required"}), 400
    i = Inquiry(
        full_name=name,
        phone=body.get("phone", ""),
        email=body.get("email", ""),
        topic=body.get("topic", ""),
        source=body.get("source", ""),
        notes=body.get("notes", ""),
    )
    db.session.add(i)
    db.session.commit()
    return jsonify(_inquiry_dict(i)), 201


@app.route("/api/inquiries/<int:iid>", methods=["PATCH"])
@require_admin
def update_inquiry(iid):
    i    = Inquiry.query.get_or_404(iid)
    body = request.get_json() or {}
    for field in ("status", "notes", "topic", "phone", "email", "source"):
        if field in body:
            setattr(i, field, body[field])
    if "workshop_id" in body:
        i.workshop_id = body["workshop_id"]
        if body["workshop_id"] and i.status == "new":
            i.status = "assigned"
    db.session.commit()
    return jsonify(_inquiry_dict(i))


@app.route("/api/inquiries/<int:iid>", methods=["DELETE"])
@require_admin
def delete_inquiry(iid):
    i = Inquiry.query.get_or_404(iid)
    db.session.delete(i)
    db.session.commit()
    return jsonify({"ok": True})


# ── Activity Log ───────────────────────────────────────────────────────────

@app.route("/api/activities")
@require_admin
def list_activities():
    acts = ActivityLog.query.order_by(ActivityLog.activity_date.desc()).all()
    return jsonify([_activity_dict(a) for a in acts])


@app.route("/api/activities", methods=["POST"])
@require_admin
def create_activity():
    body = request.get_json() or {}
    title = body.get("title", "").strip()
    if not title:
        return jsonify({"error": "title required"}), 400
    dt_raw = body.get("activity_date", "")
    if not dt_raw:
        return jsonify({"error": "activity_date required"}), 400
    try:
        act_date = date.fromisoformat(dt_raw)
    except ValueError:
        return jsonify({"error": "Invalid date format"}), 400

    a = ActivityLog(
        title=title,
        activity_type=body.get("activity_type", "other"),
        topic_category=body.get("topic_category", ""),
        activity_date=act_date,
        duration_min=body.get("duration_min"),
        participants_count=body.get("participants_count"),
        description=body.get("description", ""),
        workshop_id=body.get("workshop_id"),
    )
    db.session.add(a)
    db.session.commit()
    return jsonify(_activity_dict(a)), 201


@app.route("/api/activities/<int:aid>", methods=["PATCH"])
@require_admin
def update_activity(aid):
    a    = ActivityLog.query.get_or_404(aid)
    body = request.get_json() or {}
    for field in ("title", "activity_type", "topic_category", "description"):
        if field in body:
            setattr(a, field, body[field])
    for field in ("duration_min", "participants_count", "workshop_id"):
        if field in body:
            setattr(a, field, body[field])
    if "activity_date" in body:
        try:
            a.activity_date = date.fromisoformat(body["activity_date"])
        except ValueError:
            pass
    db.session.commit()
    return jsonify(_activity_dict(a))


@app.route("/api/activities/<int:aid>", methods=["DELETE"])
@require_admin
def delete_activity(aid):
    a = ActivityLog.query.get_or_404(aid)
    db.session.delete(a)
    db.session.commit()
    return jsonify({"ok": True})


# ─────────────────────────────────────────────
# Dashboard Focus
# ─────────────────────────────────────────────

@app.route("/api/dashboard/focus")
@require_admin
def dashboard_focus():
    """At-a-glance: students needing attention, pending submissions, pending meetings."""
    now = datetime.utcnow()
    threshold = int(os.environ.get("INACTIVITY_DAYS", 7))
    cutoff    = now - timedelta(days=threshold)

    students = User.query.filter_by(role="student").all()
    needs_attention = []
    for s in students:
        p = s.profile
        if not p or p.student_status == "completed":
            continue
        last_task = (AssignedTask.query.filter_by(user_id=s.id, status="completed")
                     .order_by(AssignedTask.completed_at.desc()).first())
        last_meeting = (Meeting.query.filter_by(student_id=s.id)
                        .filter(Meeting.status.in_(["confirmed", "completed"]))
                        .filter(Meeting.scheduled_at <= now)
                        .order_by(Meeting.scheduled_at.desc()).first())
        dates = [d for d in [
            last_task.completed_at    if last_task    else None,
            last_meeting.scheduled_at if last_meeting else None,
        ] if d]
        last_activity = max(dates) if dates else None
        if not last_activity or last_activity < cutoff:
            pending_count = AssignedTask.query.filter_by(user_id=s.id, status="pending").count()
            if pending_count:
                days = (now - last_activity).days if last_activity else None
                needs_attention.append({
                    "id": s.id,
                    "name": (p.full_name or s.username),
                    "days_inactive": days,
                    "pending_tasks": pending_count,
                })

    # Recent submissions waiting review (completed in last 7 days with note/file)
    week_ago = now - timedelta(days=7)
    recent = (AssignedTask.query
              .filter(AssignedTask.status == "completed")
              .filter(AssignedTask.completed_at >= week_ago)
              .filter(
                  (AssignedTask.submission_note != "") |
                  (AssignedTask.submission_file != "")
              ).all())
    pending_submissions = []
    for at in recent:
        s = db.session.get(User, at.user_id)
        p = s.profile if s else None
        name = (p.full_name if p and p.full_name else (s.username if s else "?"))
        pending_submissions.append({
            "student_id":   at.user_id,
            "student_name": name,
            "task_title":   at.task.title if at.task else "?",
            "completed_at": at.completed_at.isoformat() if at.completed_at else None,
        })

    # Meetings awaiting student confirmation
    pending_confirmations = (Meeting.query
                             .filter_by(status="pending")
                             .filter(Meeting.scheduled_at >= now)
                             .order_by(Meeting.scheduled_at).all())
    pending_meetings = []
    for m in pending_confirmations:
        s = db.session.get(User, m.student_id)
        p = s.profile if s else None
        name = (p.full_name if p and p.full_name else (s.username if s else "?"))
        pending_meetings.append({
            "meeting_id":   m.id,
            "student_id":   m.student_id,
            "student_name": name,
            "scheduled_at": m.scheduled_at.isoformat(),
        })

    return jsonify({
        "needs_attention":  sorted(needs_attention, key=lambda x: -(x["days_inactive"] or 999)),
        "pending_submissions": pending_submissions[:5],
        "pending_meetings": pending_meetings[:5],
    })


# ─────────────────────────────────────────────
# AI Chat
# ─────────────────────────────────────────────

def _build_student_context(sid: int) -> str:
    """Build a rich context string about a student for the AI chat."""
    student = User.query.filter_by(id=sid, role="student").first()
    if not student:
        return "סטודנט לא נמצא."

    p = student.profile
    if not p:
        return f"סטודנט {student.username} — אין פרופיל עדיין."

    edu_map = {"highschool": "תלמיד תיכון", "college": "סטודנט", "career": "הכוונה תעסוקתית"}
    lines = [
        f"שם: {p.full_name or student.username}",
        f"סוג: {edu_map.get(p.education_level, 'לא צוין')}",
        f"שלב: {p.current_occupation_or_grade or 'לא צוין'}",
        f"מטרות: {p.career_goals or 'לא צוינו'}",
        f"חששות: {p.fears_weaknesses or 'לא צוינו'}",
        f"סטטוס: {p.student_status or 'active'}",
    ]
    if p.education_level == "highschool" and p.interests_hobbies:
        lines.append(f"תחומי עניין: {p.interests_hobbies}")
    if p.education_level == "college" and p.institution_name:
        lines.append(f"מוסד: {p.institution_name}")
    if p.education_level == "career" and p.current_job:
        lines.append(f"תפקיד: {p.current_job} | ניסיון: {p.years_experience or '?'} שנים")

    # Tasks
    active    = AssignedTask.query.filter_by(user_id=sid, status="pending").all()
    completed = (AssignedTask.query.filter_by(user_id=sid, status="completed")
                 .order_by(AssignedTask.completed_at.desc()).limit(5).all())

    if active:
        lines.append("\nמשימות פעילות:")
        for a in active:
            t = a.task
            age = (datetime.utcnow() - a.assigned_at).days if a.assigned_at else 0
            lines.append(f"  • {t.title} [{t.category}] — מחכה {age} ימים")

    if completed:
        lines.append("\nמשימות שהושלמו לאחרונה:")
        for a in completed:
            note = f" | הגשה: {a.submission_note[:80]}" if a.submission_note else ""
            lines.append(f"  ✓ {a.task.title}{note}")

    # Recent notes
    notes = (MentorNote.query.filter_by(student_id=sid)
             .order_by(MentorNote.created_at.desc()).limit(4).all())
    if notes:
        lines.append("\nהערות מנטור אחרונות:")
        for n in notes:
            lines.append(f"  [{n.created_at.strftime('%d/%m')}] {n.text[:100]}")

    # Meetings
    upcoming = (Meeting.query.filter_by(student_id=sid)
                .filter(Meeting.scheduled_at >= datetime.utcnow())
                .filter(Meeting.status != "cancelled")
                .order_by(Meeting.scheduled_at).limit(2).all())
    if upcoming:
        lines.append("\nפגישות קרובות:")
        for m in upcoming:
            lines.append(f"  📅 {m.scheduled_at.strftime('%d/%m %H:%M')} ({m.duration_min} דק׳)")

    # Coaching strategy
    if p.ai_coaching_strategy and len(p.ai_coaching_strategy) > 20:
        lines.append(f"\nאסטרטגיית הדרכה:\n{p.ai_coaching_strategy[:500]}")

    return "\n".join(lines)


@app.route("/api/students/<int:sid>/chat", methods=["POST"])
@require_admin
def student_chat(sid):
    """AI chat with full student context."""
    client = _ai_client()
    if not client:
        return jsonify({"error": "no_api_key",
                        "reply": "GROQ_API_KEY לא מוגדר — הוסף ל-docker-compose.yml"}), 503

    body     = request.get_json() or {}
    history  = body.get("messages", [])   # [{"role":"user/assistant","content":"..."}]
    new_msg  = body.get("message", "").strip()
    if not new_msg:
        return jsonify({"error": "message required"}), 400

    context = _build_student_context(sid)
    system_prompt = (
        "אתה עוזר AI חכם למנטור קריירה. יש לך גישה לכל המידע על הסטודנט הבא:\n\n"
        f"{context}\n\n"
        "ענה בעברית קצרה וממוקדת. תן המלצות מעשיות. "
        "אם שואלים על התקדמות — נתח לפי הנתונים. "
        "אם שואלים על משימות — הסתמך על הרשימה. "
        "היה ישיר ואל תחזור על מה שכבר ידוע."
    )

    messages = [{"role": "system", "content": system_prompt}]
    # Add history (last 10 turns to stay within context)
    for msg in history[-10:]:
        if msg.get("role") in ("user", "assistant"):
            messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": new_msg})

    try:
        r = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            max_tokens=600,
            temperature=0.7,
        )
        reply = r.choices[0].message.content.strip()
        return jsonify({"reply": reply})
    except Exception as e:
        app.logger.error("Chat error: %s", e)
        return jsonify({"reply": f"שגיאה: {str(e)[:100]}"}), 500


# ─────────────────────────────────────────────
# Billing
# ─────────────────────────────────────────────

def _service_dict(s: Service) -> dict:
    return {
        "id": s.id, "name": s.name, "description": s.description,
        "unit": s.unit, "is_active": s.is_active,
        "price_highschool": s.price_highschool,
        "price_college":    s.price_college,
        "price_career":     s.price_career,
    }


def _price_for_student(service: Service, profile: StudentProfile | None,
                       custom: float | None = None) -> float:
    """Return the applicable price for a student."""
    if custom is not None:
        return custom
    if not profile:
        return service.price_college
    lvl = profile.education_level or "college"
    if lvl == "highschool":
        return service.price_highschool
    if lvl == "career":
        return service.price_career
    return service.price_college


# ── Service Catalog ────────────────────────────────────────────────────────

@app.route("/api/services")
@require_admin
def list_services():
    svcs = Service.query.order_by(Service.name).all()
    return jsonify([_service_dict(s) for s in svcs])


@app.route("/api/services", methods=["POST"])
@require_admin
def create_service():
    body = request.get_json() or {}
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    s = Service(
        name=name,
        description=body.get("description", ""),
        unit=body.get("unit", "per_session"),
        price_highschool=float(body.get("price_highschool", 0)),
        price_college=float(body.get("price_college", 0)),
        price_career=float(body.get("price_career", 0)),
    )
    db.session.add(s)
    db.session.commit()
    return jsonify(_service_dict(s)), 201


@app.route("/api/services/<int:sid>", methods=["PATCH"])
@require_admin
def update_service(sid):
    s    = Service.query.get_or_404(sid)
    body = request.get_json() or {}
    for field in ("name", "description", "unit"):
        if field in body:
            setattr(s, field, body[field])
    for field in ("price_highschool", "price_college", "price_career"):
        if field in body:
            setattr(s, field, float(body[field]))
    if "is_active" in body:
        s.is_active = bool(body["is_active"])
    db.session.commit()
    return jsonify(_service_dict(s))


@app.route("/api/services/<int:sid>", methods=["DELETE"])
@require_admin
def delete_service(sid):
    s = Service.query.get_or_404(sid)
    db.session.delete(s)
    db.session.commit()
    return jsonify({"ok": True})


# ── Student Billing Assignment ─────────────────────────────────────────────

@app.route("/api/students/<int:student_id>/billing")
@require_admin
def get_student_billing(student_id):
    User.query.filter_by(id=student_id, role="student").first_or_404()
    sb = StudentBilling.query.filter_by(student_id=student_id).first()
    if not sb:
        return jsonify({"assigned": False})
    s = sb.service
    student = db.session.get(User, student_id)
    price = _price_for_student(s, student.profile, sb.custom_price) if s else 0
    return jsonify({
        "assigned": True,
        "service_id":   sb.service_id,
        "service_name": s.name if s else None,
        "service_unit": s.unit if s else None,
        "custom_price": sb.custom_price,
        "effective_price": price,
        "is_active": sb.is_active,
    })


@app.route("/api/students/<int:student_id>/billing", methods=["POST"])
@require_admin
def set_student_billing(student_id):
    User.query.filter_by(id=student_id, role="student").first_or_404()
    body = request.get_json() or {}
    sb   = StudentBilling.query.filter_by(student_id=student_id).first()
    if not sb:
        sb = StudentBilling(student_id=student_id)
        db.session.add(sb)
    sb.service_id   = body.get("service_id")
    sb.custom_price = float(body["custom_price"]) if body.get("custom_price") else None
    sb.is_active    = body.get("is_active", True)
    db.session.commit()

    s     = db.session.get(Service, sb.service_id) if sb.service_id else None
    price = _price_for_student(s, db.session.get(User, student_id).profile,
                               sb.custom_price) if s else 0
    return jsonify({"ok": True, "effective_price": price})


# ── Billing Records & Monthly Report ──────────────────────────────────────

@app.route("/api/billing")
@require_admin
def billing_dashboard():
    """Monthly billing overview."""
    month = request.args.get("month", datetime.utcnow().strftime("%Y-%m"))

    # Fetch or auto-generate records for this month
    records = (BillingRecord.query.filter_by(month=month)
               .order_by(BillingRecord.student_id).all())

    MONTHS_HE = {1:"ינואר",2:"פברואר",3:"מרץ",4:"אפריל",5:"מאי",6:"יוני",
                 7:"יולי",8:"אוגוסט",9:"ספטמבר",10:"אוקטובר",11:"נובמבר",12:"דצמבר"}
    y, m = int(month[:4]), int(month[5:])
    month_name = f"{MONTHS_HE[m]} {y}"

    prev_m = f"{y}-{m-1:02d}" if m > 1 else f"{y-1}-12"
    next_m = f"{y}-{m+1:02d}" if m < 12 else f"{y+1}-01"

    result = []
    for rec in records:
        s = db.session.get(User, rec.student_id)
        p = s.profile if s else None
        svc = db.session.get(Service, rec.service_id) if rec.service_id else None
        result.append({
            "id":            rec.id,
            "student_id":    rec.student_id,
            "student_name":  (p.full_name if p and p.full_name else (s.username if s else "?")),
            "service_name":  svc.name if svc else "—",
            "service_unit":  svc.unit if svc else "per_session",
            "month":         rec.month,
            "meetings_count":rec.meetings_count,
            "amount_due":    rec.amount_due,
            "paid_at":       rec.paid_at.isoformat() if rec.paid_at else None,
            "payment_note":  rec.payment_note,
        })

    total_due  = sum(r["amount_due"] for r in result)
    total_paid = sum(r["amount_due"] for r in result if r["paid_at"])

    return jsonify({
        "month":      month,
        "month_name": month_name,
        "prev_month": prev_m,
        "next_month": next_m,
        "records":    result,
        "total_due":  total_due,
        "total_paid": total_paid,
        "total_pending": total_due - total_paid,
    })


@app.route("/api/billing/generate/<month>", methods=["POST"])
@require_admin
def generate_billing(month):
    """Calculate billing for all active students for the given month (YYYY-MM)."""
    try:
        y, m = int(month[:4]), int(month[5:])
    except Exception:
        return jsonify({"error": "Invalid month format (YYYY-MM)"}), 400

    from_dt = datetime(y, m, 1)
    to_dt   = datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1)

    students = User.query.filter_by(role="student").all()
    created = 0

    for student in students:
        sb = StudentBilling.query.filter_by(student_id=student.id, is_active=True).first()
        if not sb or not sb.service_id:
            continue
        svc = db.session.get(Service, sb.service_id)
        if not svc:
            continue

        # Count confirmed/completed meetings this month
        meeting_count = (Meeting.query
                         .filter_by(student_id=student.id)
                         .filter(Meeting.scheduled_at >= from_dt,
                                 Meeting.scheduled_at <  to_dt)
                         .filter(Meeting.status.in_(["confirmed", "completed"]))
                         .count())

        price = _price_for_student(svc, student.profile, sb.custom_price)

        if svc.unit == "per_session":
            amount = meeting_count * price
        else:
            amount = price  # monthly / fixed

        # Upsert
        rec = BillingRecord.query.filter_by(
            student_id=student.id, month=month).first()
        if not rec:
            rec = BillingRecord(student_id=student.id, month=month)
            db.session.add(rec)
        rec.service_id     = sb.service_id
        rec.meetings_count = meeting_count
        rec.amount_due     = amount
        created += 1

    db.session.commit()
    return jsonify({"ok": True, "processed": created, "month": month})


@app.route("/api/billing/<int:rec_id>/pay", methods=["PATCH"])
@require_admin
def mark_billing_paid(rec_id):
    rec  = BillingRecord.query.get_or_404(rec_id)
    body = request.get_json() or {}
    if body.get("paid"):
        rec.paid_at      = datetime.utcnow()
        rec.payment_note = body.get("note", "")
    else:
        rec.paid_at      = None
        rec.payment_note = ""
    db.session.commit()
    return jsonify({"ok": True,
                    "paid_at": rec.paid_at.isoformat() if rec.paid_at else None})


@app.route("/api/students/<int:student_id>/billing/history")
@require_admin
def student_billing_history(student_id):
    """Per-student monthly billing history."""
    User.query.filter_by(id=student_id, role="student").first_or_404()
    year = request.args.get("year", type=int, default=datetime.utcnow().year)

    records = (BillingRecord.query
               .filter_by(student_id=student_id)
               .filter(BillingRecord.month.like(f"{year}-%"))
               .order_by(BillingRecord.month.desc()).all())

    result = []
    for rec in records:
        svc = db.session.get(Service, rec.service_id) if rec.service_id else None
        result.append({
            "id":            rec.id,
            "month":         rec.month,
            "service_name":  svc.name if svc else "—",
            "service_unit":  svc.unit if svc else "per_session",
            "meetings_count":rec.meetings_count,
            "amount_due":    rec.amount_due,
            "paid_at":       rec.paid_at.isoformat() if rec.paid_at else None,
            "payment_note":  rec.payment_note,
        })

    total_year = sum(r["amount_due"] for r in result)
    meetings_year = sum(r["meetings_count"] for r in result)

    return jsonify({
        "year":           year,
        "records":        result,
        "total_year":     total_year,
        "meetings_year":  meetings_year,
    })


# ─────────────────────────────────────────────
# Reports
# ─────────────────────────────────────────────

@app.route("/api/students/<int:sid>/report")
@require_admin
def student_report_data(sid):
    """Full student data for PDF report."""
    student = User.query.filter_by(id=sid, role="student").first_or_404()
    notes   = (MentorNote.query.filter_by(student_id=sid)
               .order_by(MentorNote.created_at.desc()).all())
    active    = AssignedTask.query.filter_by(user_id=sid, status="pending").all()
    completed = (AssignedTask.query.filter_by(user_id=sid, status="completed")
                 .order_by(AssignedTask.completed_at.desc()).all())
    return jsonify({
        "student":   {"id": student.id, "username": student.username},
        "profile":   _profile_dict(student.profile),
        "active":    [_assignment_dict(a) for a in active],
        "completed": [_assignment_dict(a) for a in completed],
        "notes":     [{"text": n.text, "created_at": n.created_at.isoformat()} for n in notes],
    })


@app.route("/api/reports/meetings")
@require_admin
def meetings_report():
    """Monthly meetings summary."""
    year  = request.args.get("year",  type=int, default=date.today().year)
    month = request.args.get("month", type=int, default=date.today().month)
    from_dt = datetime(year, month, 1)
    to_dt   = datetime(year + 1 if month == 12 else year,
                       1 if month == 12 else month + 1, 1)

    meetings = (Meeting.query
                .filter(Meeting.scheduled_at >= from_dt,
                        Meeting.scheduled_at <  to_dt)
                .filter(Meeting.status != "cancelled")
                .order_by(Meeting.scheduled_at).all())

    # Aggregate per student
    per_student: dict = {}
    for m in meetings:
        s = db.session.get(User, m.student_id)
        p = s.profile if s else None
        name = (p.full_name if p and p.full_name else (s.username if s else "?"))
        if m.student_id not in per_student:
            per_student[m.student_id] = {"name": name, "count": 0,
                                          "total_min": 0, "meetings": []}
        per_student[m.student_id]["count"]     += 1
        per_student[m.student_id]["total_min"] += m.duration_min or 0
        per_student[m.student_id]["meetings"].append({
            "date":     m.scheduled_at.strftime("%d/%m/%Y"),
            "time":     m.scheduled_at.strftime("%H:%M"),
            "duration": m.duration_min,
            "status":   m.status,
            "notes":    m.notes or "",
        })

    MONTHS_HE = {1:"ינואר",2:"פברואר",3:"מרץ",4:"אפריל",5:"מאי",6:"יוני",
                 7:"יולי",8:"אוגוסט",9:"ספטמבר",10:"אוקטובר",11:"נובמבר",12:"דצמבר"}
    return jsonify({
        "year": year, "month": month, "month_name": MONTHS_HE[month],
        "total_meetings":  len(meetings),
        "total_students":  len(per_student),
        "total_hours":     round(sum(m.duration_min or 0 for m in meetings) / 60, 1),
        "per_student":     list(per_student.values()),
    })


# ─────────────────────────────────────────────
# Inactivity reminder (APScheduler daily job)
# ─────────────────────────────────────────────

# In-memory set to avoid duplicate sends on the same calendar day
_notified_today: set = set()


def check_inactive_students():
    """Daily: send WhatsApp to students who haven't been active in X days."""
    threshold = int(os.environ.get("INACTIVITY_DAYS", 7))
    cutoff    = datetime.utcnow() - timedelta(days=threshold)
    today_key = datetime.utcnow().strftime("%Y-%m-%d")

    with app.app_context():
        students = User.query.filter_by(role="student").all()
        for s in students:
            if f"{today_key}:{s.id}" in _notified_today:
                continue  # already notified today

            last_task = (AssignedTask.query
                         .filter_by(user_id=s.id, status="completed")
                         .order_by(AssignedTask.completed_at.desc()).first())
            last_meeting = (Meeting.query
                            .filter_by(student_id=s.id)
                            .filter(Meeting.scheduled_at <= datetime.utcnow())
                            .filter(Meeting.status != "cancelled")
                            .order_by(Meeting.scheduled_at.desc()).first())

            dates = [d for d in [
                last_task.completed_at    if last_task    else None,
                last_meeting.scheduled_at if last_meeting else None,
            ] if d]
            last_activity = max(dates) if dates else None

            if last_activity and last_activity > cutoff:
                continue  # active

            p = s.profile
            if not p or not p.phone:
                continue

            pending = AssignedTask.query.filter_by(user_id=s.id, status="pending").count()
            if pending == 0:
                continue

            name = p.full_name or s.username
            ok, _ = send_whatsapp(p.phone,
                f"שלום {name}! לא ראינו אותך פעיל/ה בתוכנית לאחרונה. "
                f"יש לך {pending} משימות ממתינות — היכנס/י ותמשיך/י 💪")
            if ok:
                _notified_today.add(f"{today_key}:{s.id}")
                app.logger.info("Inactivity reminder sent to student %s", s.username)


# Start scheduler (only in production/gunicorn, not in pytest)
if os.environ.get("TESTING") != "true":
    _scheduler = BackgroundScheduler(timezone="UTC", daemon=True)
    _scheduler.add_job(check_inactive_students, "cron", hour=9, minute=0)
    _scheduler.start()


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

    # Default service catalog
    if Service.query.count() == 0:
        default_services = [
            Service(name="שיעור",           description="פגישה אישית — לפי שעה",
                    unit="per_session",
                    price_highschool=150, price_college=200, price_career=250),
            Service(name="כתיבת ראיון",     description="כתיבת קורות חיים ומכתב כיסוי",
                    unit="fixed",
                    price_highschool=400, price_college=500, price_career=700),
            Service(name="ליווי תעסוקי",    description="חבילת ליווי חודשית",
                    unit="monthly",
                    price_highschool=600, price_college=800, price_career=1200),
            Service(name="חיפוש כיוון",     description="סדרת פגישות לבחירת כיוון",
                    unit="monthly",
                    price_highschool=500, price_college=700, price_career=1000),
            Service(name="תוכנית מלאה",     description="ליווי מלא — הכל כלול",
                    unit="monthly",
                    price_highschool=900, price_college=1200, price_career=1800),
        ]
        db.session.add_all(default_services)

    db.session.commit()


with app.app_context():
    db.create_all()
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(os.path.join(UPLOAD_FOLDER, "tasks"), exist_ok=True)

    # Safe migrations (idempotent — fails silently if column already exists)
    with db.engine.connect() as _conn:
        for _stmt in [
            "ALTER TABLE student_profiles ADD COLUMN student_status TEXT DEFAULT 'active'",
            "ALTER TABLE student_profiles ADD COLUMN interests_hobbies TEXT DEFAULT ''",
            "ALTER TABLE student_profiles ADD COLUMN institution_name TEXT DEFAULT ''",
            "ALTER TABLE student_profiles ADD COLUMN graduation_year INTEGER",
            "ALTER TABLE student_profiles ADD COLUMN current_job TEXT DEFAULT ''",
            "ALTER TABLE student_profiles ADD COLUMN years_experience INTEGER",
            "ALTER TABLE student_profiles ADD COLUMN reason_for_guidance TEXT DEFAULT ''",
            "ALTER TABLE meetings ADD COLUMN meeting_type TEXT DEFAULT 'progress_review'",
            "ALTER TABLE student_profiles ADD COLUMN resume_file TEXT DEFAULT ''",
            "ALTER TABLE student_profiles ADD COLUMN ai_strategy_updated_at TIMESTAMP",
        ]:
            try:
                _conn.execute(db.text(_stmt))
                _conn.commit()
            except Exception:
                _conn.rollback()

    seed_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000,
            debug=os.environ.get("FLASK_DEBUG", "false").lower() == "true")
