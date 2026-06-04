import os
import json
import hmac
import hashlib
import calendar as _cal
from datetime import datetime, date, timedelta
from collections import defaultdict
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, flash, abort
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from openai import OpenAI

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///interviewsync.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB upload limit

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "static", "uploads")
ALLOWED_EXT   = {"pdf", "doc", "docx", "png", "jpg", "jpeg", "txt"}

db = SQLAlchemy(app)

# ── Rate limiting (in-memory, resets on restart) ──────────────────────────
_login_attempts: dict = defaultdict(list)

def _check_rate_limit(ip: str) -> bool:
    """Allow max 5 login attempts per 5 minutes per IP. Returns True if OK."""
    now    = datetime.utcnow()
    cutoff = now - timedelta(seconds=300)
    _login_attempts[ip] = [t for t in _login_attempts[ip] if t > cutoff]
    if len(_login_attempts[ip]) >= 5:
        return False
    _login_attempts[ip].append(now)
    return True


@app.context_processor
def inject_globals():
    """Make today's date and utcnow available in all templates."""
    return {"now_date": date.today(), "now_datetime": datetime.utcnow()}


# ─────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────

class User(db.Model):
    __tablename__ = "users"
    id       = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(256), nullable=False)
    role     = db.Column(db.String(10), nullable=False, default="student")  # "admin" | "student"
    profile  = db.relationship("StudentProfile", backref="user", uselist=False, lazy=True)
    tasks    = db.relationship("AssignedTask", backref="student", lazy=True)
    meetings = db.relationship("Meeting", foreign_keys="Meeting.student_id",
                               backref="student", lazy=True)


class StudentProfile(db.Model):
    __tablename__ = "student_profiles"
    id                          = db.Column(db.Integer, primary_key=True)
    user_id                     = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    full_name                   = db.Column(db.String(120), default="")
    email                       = db.Column(db.String(120), default="")
    phone                       = db.Column(db.String(30), default="")
    education_level             = db.Column(db.String(20), default="")   # "highschool" | "college"
    current_occupation_or_grade = db.Column(db.String(200), default="")
    career_goals                = db.Column(db.Text, default="")
    fears_weaknesses            = db.Column(db.Text, default="")
    ai_coaching_strategy        = db.Column(db.Text, default="")
    resume_content              = db.Column(db.Text, default="")
    # Process timeline (admin manages)
    process_start_date          = db.Column(db.Date, nullable=True)
    target_end_date             = db.Column(db.Date, nullable=True)
    # Private mentor scratch pad
    mentor_notes                = db.Column(db.Text, default="")
    created_at                  = db.Column(db.DateTime, default=datetime.utcnow)


class TaskBank(db.Model):
    __tablename__ = "task_bank"
    id            = db.Column(db.Integer, primary_key=True)
    title         = db.Column(db.String(512), nullable=False)
    description   = db.Column(db.Text, default="")
    category      = db.Column(db.String(100), default="כללי")
    task_type     = db.Column(db.String(30), default="task")  # task | reflection | exercise
    resource_file = db.Column(db.String(512), default="")     # admin-uploaded guide/template
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    assignments   = db.relationship("AssignedTask", backref="task", lazy=True)


class AssignedTask(db.Model):
    __tablename__ = "assigned_tasks"
    id              = db.Column(db.Integer, primary_key=True)
    user_id         = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    task_id         = db.Column(db.Integer, db.ForeignKey("task_bank.id"), nullable=False)
    status          = db.Column(db.String(20), default="pending")   # pending | completed
    assigned_at     = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at    = db.Column(db.DateTime, nullable=True)
    submission_note = db.Column(db.Text, default="")                # student's text answer
    submission_file = db.Column(db.String(512), default="")         # student-uploaded proof
    __table_args__  = (db.UniqueConstraint("user_id", "task_id", name="uq_assigned_task"),)


class Meeting(db.Model):
    __tablename__ = "meetings"
    id           = db.Column(db.Integer, primary_key=True)
    student_id   = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    scheduled_at = db.Column(db.DateTime, nullable=False)
    duration_min = db.Column(db.Integer, default=60)
    notes        = db.Column(db.Text, default="")
    status       = db.Column(db.String(20), default="pending")  # pending | confirmed | cancelled
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)


# ─────────────────────────────────────────────
# File upload helpers
# ─────────────────────────────────────────────

def _normalize_phone(phone: str) -> str:
    """Normalize Israeli phone numbers to E.164 (+972...). Accepts 05X, 972X, +972X formats."""
    import re
    p = re.sub(r"[\s\-\(\)]", "", phone).strip()
    if not p:
        return ""
    if p.startswith("+"):
        return p                          # already E.164
    if p.startswith("972"):
        return "+" + p                    # 9720501234567 → +9720501234567
    if re.match(r"^0[5-9]\d{8}$", p):
        return "+972" + p[1:]             # 0501234567 → +972501234567
    return "+" + p if p else p            # best-effort: prepend +


def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def _save_student_submission(file, user_id: int, task_id: int) -> str:
    """Save student submission file. Returns relative path (from static/) or ''."""
    if not file or not file.filename or not _allowed_file(file.filename):
        return ""
    user_dir = os.path.join(UPLOAD_FOLDER, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    safe = secure_filename(f"task_{task_id}_{file.filename}")
    file.save(os.path.join(user_dir, safe))
    return f"uploads/{user_id}/{safe}"


def _save_task_resource(file, task_id: int) -> str:
    """Save task resource file (admin uploads). Returns relative path or ''."""
    if not file or not file.filename or not _allowed_file(file.filename):
        return ""
    res_dir = os.path.join(UPLOAD_FOLDER, "tasks")
    os.makedirs(res_dir, exist_ok=True)
    safe = secure_filename(f"resource_{task_id}_{file.filename}")
    file.save(os.path.join(res_dir, safe))
    return f"uploads/tasks/{safe}"


# ─────────────────────────────────────────────
# Meeting token (HMAC-based, no DB column needed)
# ─────────────────────────────────────────────

def _meeting_token(meeting_id: int) -> str:
    key = app.secret_key.encode() if isinstance(app.secret_key, str) else app.secret_key
    return hmac.new(key, str(meeting_id).encode(), hashlib.sha256).hexdigest()[:20]


# ─────────────────────────────────────────────
# AI helpers
# ─────────────────────────────────────────────

def _get_ai_client():
    key = os.environ.get("AI_API_KEY")
    return OpenAI(api_key=key) if key else None


def _parse_ai_json(raw: str) -> list:
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        if len(parts) >= 2:
            raw = parts[1].lstrip("json").lstrip("\n")
    try:
        return json.loads(raw)
    except Exception:
        return []


def ai_generate_coaching_strategy(profile: "StudentProfile") -> str:
    client = _get_ai_client()
    if not client:
        return "ניתוח AI אינו זמין — הגדר AI_API_KEY כדי להפעיל."

    edu = "תיכון" if profile.education_level == "highschool" else "מכללה/אוניברסיטה"
    prompt = f"""אתה יועץ קריירה מומחה. קיבלת את הפרופיל הבא של תלמיד/סטודנט:
שם: {profile.full_name}
רמת לימוד: {edu}
כיתה/תואר: {profile.current_occupation_or_grade or 'לא צוין'}
מטרות קריירה: {profile.career_goals or 'לא צוינו'}
חששות ונקודות חולשה: {profile.fears_weaknesses or 'לא צוינו'}

כתוב אסטרטגיית הדרכה מקצועית (4-5 נקודות) עבור המנטור, הכוללת:
• נושאי מיקוד מרכזיים לפי הרקע
• המלצות ממוקדות לטיפול בחולשות
• שלבי פעולה מומלצים לטווח הקרוב
• גישת אימון מותאמת אישית לסטודנט זה

כתוב בעברית מקצועית, פונה ישירות למנטור."""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=700, temperature=0.7,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"ניתוח AI נכשל: {e}"


def ai_generate_tasks_for_student(profile: "StudentProfile", count: int = 5) -> list:
    client = _get_ai_client()
    if not client:
        return []

    edu = "תיכון" if profile.education_level == "highschool" else "מכללה/אוניברסיטה"
    prompt = f"""אתה יועץ קריירה. צור בדיוק {count} משימות הכנה מותאמות לסטודנט:
רמה: {edu} | שלב: {profile.current_occupation_or_grade} | מטרות: {profile.career_goals} | חולשות: {profile.fears_weaknesses}

החזר JSON בלבד (ללא מרקדאון):
[{{"title":"...","description":"...","category":"קורות חיים|LinkedIn|הכנה לראיון|שאלון|כללי","task_type":"task|reflection|exercise"}}]"""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000, temperature=0.8,
        )
        return _parse_ai_json(resp.choices[0].message.content)[:count]
    except Exception as e:
        app.logger.error("ai_generate_tasks_for_student: %s", e)
        return []


# ─────────────────────────────────────────────
# WhatsApp (Twilio)
# ─────────────────────────────────────────────

def send_whatsapp(phone: str, message: str) -> tuple[bool, str]:
    """
    Send WhatsApp via Twilio. Returns (success, reason_string).
    reason is empty string on success, or a description of why it failed.
    """
    sid   = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_ = os.environ.get("TWILIO_WHATSAPP_FROM")

    if not sid or not token or not from_:
        return False, "no_config"
    if not phone:
        return False, "no_phone"
    try:
        from twilio.rest import Client
        client = Client(sid, token)
        to = f"whatsapp:{phone}" if not phone.startswith("whatsapp:") else phone
        client.messages.create(body=message, from_=from_, to=to)
        return True, ""
    except Exception as e:
        app.logger.error("WhatsApp send failed: %s", e)
        return False, str(e)


# ─────────────────────────────────────────────
# Auth helpers
# ─────────────────────────────────────────────

def current_user():
    uid = session.get("user_id")
    return db.session.get(User, uid) if uid else None


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        u = current_user()
        if not u or u.role != "admin":
            flash("נדרשת הרשאת מנהל.", "danger")
            return redirect(url_for("index"))
        return fn(*args, **kwargs)
    return wrapper


def _profile_complete(user: User) -> bool:
    p = user.profile
    return bool(p and p.education_level and p.career_goals and p.full_name)


# ─────────────────────────────────────────────
# Routes — Auth
# ─────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    u = current_user()
    if u:
        return redirect(url_for("admin_dashboard") if u.role == "admin" else url_for("index"))

    if request.method == "POST":
        ip = request.remote_addr or "unknown"
        if not _check_rate_limit(ip):
            flash("יותר מדי ניסיונות כניסה. נסה שוב בעוד 5 דקות.", "danger")
            return render_template("login.html")

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            session["user_id"] = user.id
            if user.role == "admin":
                flash(f"ברוך הבא, {user.username}!", "success")
                return redirect(url_for("admin_dashboard"))
            if not _profile_complete(user):
                return redirect(url_for("onboarding"))
            name = user.profile.full_name if user.profile else user.username
            flash(f"ברוך הבא, {name}!", "success")
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
    user = current_user()
    if user.role == "admin":
        return redirect(url_for("admin_dashboard"))
    if _profile_complete(user):
        return redirect(url_for("index"))

    if request.method == "POST":
        full_name  = request.form.get("full_name", "").strip()
        email      = request.form.get("email", "").strip()
        phone      = request.form.get("phone", "").strip()
        edu_level  = request.form.get("education_level", "").strip()
        occupation = request.form.get("current_occupation_or_grade", "").strip()
        goals      = request.form.get("career_goals", "").strip()
        weaknesses = request.form.get("fears_weaknesses", "").strip()

        if not (full_name and edu_level and goals):
            flash("יש למלא את כל השדות המסומנים כחובה.", "warning")
            return redirect(url_for("onboarding"))

        is_new     = user.profile is None
        profile    = user.profile or StudentProfile(user_id=user.id)
        profile.full_name                   = full_name
        profile.email                       = email
        profile.phone                       = _normalize_phone(phone)
        profile.education_level             = edu_level
        profile.current_occupation_or_grade = occupation
        profile.career_goals                = goals
        profile.fears_weaknesses            = weaknesses
        profile.ai_coaching_strategy        = ai_generate_coaching_strategy(profile)

        # Auto-set process start date on first submission
        if is_new or not profile.process_start_date:
            profile.process_start_date = date.today()

        if is_new:
            db.session.add(profile)
        db.session.commit()
        flash("הפרופיל נשמר! המנטור שלך יוצר עבורך תוכנית.", "success")
        return redirect(url_for("index"))

    return render_template("onboarding.html", user=user)


@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    user = current_user()
    if user.role == "admin":
        return redirect(url_for("admin_dashboard"))
    if not _profile_complete(user):
        return redirect(url_for("onboarding"))

    if request.method == "POST":
        task_id         = request.form.get("task_id", type=int)
        submission_note = request.form.get("submission_note", "").strip()
        upload_file     = request.files.get("submission_file")

        at = AssignedTask.query.filter_by(user_id=user.id, task_id=task_id).first_or_404()
        if at.status == "pending":
            at.status           = "completed"
            at.completed_at     = datetime.utcnow()
            at.submission_note  = submission_note
            if upload_file and upload_file.filename:
                path = _save_student_submission(upload_file, user.id, task_id)
                if path:
                    at.submission_file = path
                elif upload_file.filename:
                    flash("סוג הקובץ אינו נתמך (PDF, DOC, תמונה, TXT בלבד).", "warning")
            db.session.commit()
            flash("כל הכבוד! המשימה סומנה כהושלמה ✓", "success")
        return redirect(url_for("index"))

    active    = (AssignedTask.query
                 .filter_by(user_id=user.id, status="pending")
                 .order_by(AssignedTask.assigned_at.desc()).all())
    completed = (AssignedTask.query
                 .filter_by(user_id=user.id, status="completed")
                 .order_by(AssignedTask.completed_at.desc()).all())

    # Upcoming meetings for this student
    upcoming_meetings = (Meeting.query
                         .filter_by(student_id=user.id)
                         .filter(Meeting.scheduled_at >= datetime.utcnow())
                         .filter(Meeting.status != "cancelled")
                         .order_by(Meeting.scheduled_at).limit(3).all())

    return render_template("index.html",
        user=user,
        profile=user.profile,
        active=active,
        completed=completed,
        total=len(active) + len(completed),
        upcoming_meetings=upcoming_meetings,
    )


@app.route("/schedule")
@login_required
def student_schedule():
    user = current_user()
    if user.role == "admin":
        return redirect(url_for("admin_dashboard"))

    upcoming = (Meeting.query
                .filter_by(student_id=user.id)
                .filter(Meeting.status != "cancelled")
                .order_by(Meeting.scheduled_at).all())
    meeting_tokens = {m.id: _meeting_token(m.id) for m in upcoming}
    return render_template("student_schedule.html",
        user=user, profile=user.profile,
        meetings=upcoming, meeting_tokens=meeting_tokens)


# ─────────────────────────────────────────────
# Routes — Admin
# ─────────────────────────────────────────────

@app.route("/admin")
@login_required
@admin_required
def admin_dashboard():
    students  = User.query.filter_by(role="student").order_by(User.username).all()
    taskbank  = TaskBank.query.order_by(TaskBank.category, TaskBank.title).all()

    progress = {}
    for s in students:
        total = AssignedTask.query.filter_by(user_id=s.id).count()
        done  = AssignedTask.query.filter_by(user_id=s.id, status="completed").count()
        progress[s.id] = {"total": total, "done": done,
                          "pct": round(done / total * 100) if total else 0}

    categories = sorted({t.category for t in taskbank})

    # Upcoming meetings count for navbar badge
    upcoming_count = (Meeting.query
                      .filter(Meeting.scheduled_at >= datetime.utcnow())
                      .filter(Meeting.status != "cancelled").count())

    return render_template("admin.html",
        user=current_user(),
        students=students,
        taskbank=taskbank,
        progress=progress,
        categories=categories,
        upcoming_count=upcoming_count,
    )


@app.route("/admin/taskbank", methods=["POST"])
@login_required
@admin_required
def admin_taskbank():
    action = request.form.get("action")

    if action == "add":
        title    = request.form.get("title", "").strip()
        desc     = request.form.get("description", "").strip()
        category = request.form.get("category", "כללי").strip()
        ttype    = request.form.get("task_type", "task")
        if title:
            task = TaskBank(title=title, description=desc, category=category, task_type=ttype)
            db.session.add(task)
            db.session.flush()  # get task.id for file naming
            rfile = request.files.get("resource_file")
            if rfile and rfile.filename:
                path = _save_task_resource(rfile, task.id)
                if path:
                    task.resource_file = path
            db.session.commit()
            flash("המשימה נוספה לבנק.", "success")
        else:
            flash("כותרת המשימה לא יכולה להיות ריקה.", "warning")

    elif action == "edit":
        task_id  = request.form.get("task_id", type=int)
        task     = TaskBank.query.get_or_404(task_id)
        task.title       = request.form.get("title", task.title).strip() or task.title
        task.description = request.form.get("description", "").strip()
        task.category    = request.form.get("category", task.category).strip()
        task.task_type   = request.form.get("task_type", task.task_type)
        rfile = request.files.get("resource_file")
        if rfile and rfile.filename:
            path = _save_task_resource(rfile, task.id)
            if path:
                task.resource_file = path
        elif request.form.get("clear_resource"):
            task.resource_file = ""
        db.session.commit()
        flash("המשימה עודכנה.", "success")

    elif action == "delete":
        task_id = request.form.get("task_id", type=int)
        task    = TaskBank.query.get_or_404(task_id)
        AssignedTask.query.filter_by(task_id=task_id).delete()
        db.session.delete(task)
        db.session.commit()
        flash("המשימה נמחקה מהבנק.", "success")

    return redirect(url_for("admin_dashboard") + "#tab-taskbank")


@app.route("/admin/student/<int:student_id>", methods=["GET", "POST"])
@login_required
@admin_required
def student_file(student_id):
    student  = User.query.filter_by(id=student_id, role="student").first_or_404()
    profile  = student.profile
    taskbank = TaskBank.query.order_by(TaskBank.category, TaskBank.title).all()

    if request.method == "POST":
        action = request.form.get("action")

        if action == "assign_tasks":
            checked_ids = request.form.getlist("task_ids", type=int)
            existing    = {at.task_id: at for at in AssignedTask.query.filter_by(user_id=student_id).all()}
            for tid, at in existing.items():
                if tid not in checked_ids and at.status == "pending":
                    db.session.delete(at)
            new_ids = [tid for tid in checked_ids if tid not in existing]
            for tid in new_ids:
                db.session.add(AssignedTask(user_id=student_id, task_id=tid))
            db.session.commit()
            if new_ids and profile and profile.phone:
                name = profile.full_name or student.username
                msg  = f"שלום {name}! המנטור שלך הוסיף לך {len(new_ids)} משימות חדשות. היכנס/י למערכת לצפייה 📋"
                sent, reason = send_whatsapp(profile.phone, msg)
                flash(f"שויכו {len(new_ids)} משימות" + (" ונשלחה הודעת WhatsApp." if sent else f". WhatsApp: {reason or "לא מוגדר"}."), "success" if sent else "warning")
            else:
                flash("המשימות עודכנו בהצלחה.", "success")

        elif action == "save_resume":
            if profile:
                profile.resume_content = request.form.get("resume_content", "")
                db.session.commit()
                flash("קורות החיים נשמרו.", "success")
            else:
                flash("אין פרופיל לסטודנט זה עדיין.", "warning")

        elif action == "save_profile_settings":
            if profile:
                start_raw = request.form.get("process_start_date", "").strip()
                end_raw   = request.form.get("target_end_date", "").strip()
                try:
                    profile.process_start_date = date.fromisoformat(start_raw) if start_raw else profile.process_start_date
                except ValueError:
                    pass
                try:
                    profile.target_end_date = date.fromisoformat(end_raw) if end_raw else None
                except ValueError:
                    pass
                profile.mentor_notes = request.form.get("mentor_notes", "").strip()
                db.session.commit()
                flash("הגדרות התהליך נשמרו.", "success")
            else:
                flash("אין פרופיל לסטודנט זה עדיין.", "warning")

        elif action == "regenerate_strategy":
            if profile:
                profile.ai_coaching_strategy = ai_generate_coaching_strategy(profile)
                db.session.commit()
                flash("אסטרטגיית ההדרכה עודכנה.", "success")
            else:
                flash("אין פרופיל — לא ניתן לייצר אסטרטגיה.", "warning")

        return redirect(url_for("student_file", student_id=student_id))

    assigned_ids = {at.task_id for at in AssignedTask.query.filter_by(user_id=student_id).all()}
    active    = (AssignedTask.query.filter_by(user_id=student_id, status="pending")
                 .order_by(AssignedTask.assigned_at).all())
    completed = (AssignedTask.query.filter_by(user_id=student_id, status="completed")
                 .order_by(AssignedTask.completed_at.desc()).all())
    categories = sorted({t.category for t in taskbank})

    # Upcoming meetings for this student
    student_meetings = (Meeting.query
                        .filter_by(student_id=student_id)
                        .filter(Meeting.scheduled_at >= datetime.utcnow())
                        .filter(Meeting.status != "cancelled")
                        .order_by(Meeting.scheduled_at).all())

    return render_template("student_file.html",
        user=current_user(),
        student=student,
        profile=profile,
        taskbank=taskbank,
        assigned_ids=assigned_ids,
        active=active,
        completed=completed,
        categories=categories,
        student_meetings=student_meetings,
    )


@app.route("/admin/ai-tasks/<int:student_id>", methods=["POST"])
@login_required
@admin_required
def ai_tasks_for_student(student_id):
    student = User.query.filter_by(id=student_id, role="student").first_or_404()
    profile = student.profile
    if not profile:
        flash("הסטודנט לא מילא פרופיל עדיין.", "warning")
        return redirect(url_for("student_file", student_id=student_id))

    suggestions = ai_generate_tasks_for_student(profile, count=5)
    if not suggestions:
        flash("יצירת משימות AI נכשלה — ודא שמפתח AI_API_KEY מוגדר.", "warning")
        return redirect(url_for("student_file", student_id=student_id))

    created = 0
    for s in suggestions:
        title = s.get("title", "").strip()
        if not title:
            continue
        task = TaskBank(title=title, description=s.get("description", ""),
                        category=s.get("category", "כללי"), task_type=s.get("task_type", "task"))
        db.session.add(task)
        db.session.flush()
        db.session.add(AssignedTask(user_id=student_id, task_id=task.id))
        created += 1

    db.session.commit()

    if profile.phone and created:
        name = profile.full_name or student.username
        send_whatsapp(profile.phone,
            f"שלום {name}! המנטור שלך יצר עבורך {created} משימות חדשות בעזרת AI 🤖 היכנס/י לצפייה.")
        # fire-and-forget — result not needed here

    flash(f"נוצרו ושויכו {created} משימות AI לסטודנט.", "success")
    return redirect(url_for("student_file", student_id=student_id))


# ─────────────────────────────────────────────
# Routes — Meetings / Schedule
# ─────────────────────────────────────────────

@app.route("/admin/schedule", methods=["GET", "POST"])
@login_required
@admin_required
def admin_schedule():
    students = User.query.filter_by(role="student").order_by(User.username).all()

    if request.method == "POST":
        action = request.form.get("action")

        if action == "create_meeting":
            student_id   = request.form.get("student_id", type=int)
            scheduled_str = request.form.get("scheduled_at", "").strip()
            duration     = request.form.get("duration_min", 60, type=int)
            notes        = request.form.get("notes", "").strip()

            if not (student_id and scheduled_str):
                flash("יש לבחור תלמיד ותאריך/שעה.", "warning")
                return redirect(url_for("admin_schedule"))

            try:
                scheduled_at = datetime.fromisoformat(scheduled_str)
            except ValueError:
                flash("פורמט תאריך לא תקין.", "warning")
                return redirect(url_for("admin_schedule"))

            student = User.query.get_or_404(student_id)
            meeting = Meeting(student_id=student_id, scheduled_at=scheduled_at,
                              duration_min=duration, notes=notes)
            db.session.add(meeting)
            db.session.commit()

            # Send WhatsApp confirmation request
            profile = student.profile
            if profile and profile.phone:
                token        = _meeting_token(meeting.id)
                confirm_url  = url_for("meeting_confirm", meeting_id=meeting.id, token=token, _external=True)
                dt_str       = scheduled_at.strftime("%d/%m/%Y בשעה %H:%M")
                name         = profile.full_name or student.username
                msg = (f"שלום {name}! נקבעה פגישה ביום {dt_str} (משך: {duration} דקות).\n"
                       f"לאישור הפגישה לחץ/י כאן: {confirm_url}")
                sent, reason = send_whatsapp(profile.phone, msg)
                if sent:
                    flash("הפגישה נקבעה ונשלחה הודעת WhatsApp לאישור.", "success")
                elif reason == "no_config":
                    flash("הפגישה נקבעה. Twilio לא מוגדר (TWILIO_ACCOUNT_SID/AUTH_TOKEN/WHATSAPP_FROM חסרים) — שלח/י הודעה ידנית.", "warning")
                else:
                    flash(f"הפגישה נקבעה. שגיאת WhatsApp: {reason}", "warning")
            else:
                flash("הפגישה נקבעה. לתלמיד אין מספר טלפון — עדכן/י בפרופיל.", "warning")

        elif action == "cancel_meeting":
            meeting_id = request.form.get("meeting_id", type=int)
            meeting    = Meeting.query.get_or_404(meeting_id)
            meeting.status = "cancelled"
            db.session.commit()
            student = db.session.get(User, meeting.student_id)
            profile = student.profile if student else None
            if profile and profile.phone:
                dt_str = meeting.scheduled_at.strftime("%d/%m/%Y %H:%M")
                name   = profile.full_name or student.username
                send_whatsapp(profile.phone, f"שלום {name}! הפגישה בתאריך {dt_str} בוטלה. נדבר בקרוב.")
            flash("הפגישה בוטלה.", "success")

        elif action == "send_reminder":
            meeting_id = request.form.get("meeting_id", type=int)
            meeting    = Meeting.query.get_or_404(meeting_id)
            student    = db.session.get(User, meeting.student_id)
            profile    = student.profile if student else None
            if not profile or not profile.phone:
                flash("אין מספר טלפון לתלמיד זה — עדכן/י בפרופיל.", "warning")
            else:
                dt_str = meeting.scheduled_at.strftime("%d/%m/%Y בשעה %H:%M")
                name   = profile.full_name or student.username
                sent, reason = send_whatsapp(profile.phone,
                    f"תזכורת &#128197; שלום {name}! יש לנו פגישה ב-{dt_str}. להתראות!")
                if sent:
                    flash("תזכורת WhatsApp נשלחה בהצלחה.", "success")
                elif reason == "no_config":
                    flash("Twilio לא מוגדר — הוסף TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM כ-env vars.", "danger")
                else:
                    flash(f"שגיאת WhatsApp: {reason}", "danger")

        return redirect(url_for("admin_schedule"))

    # ── Calendar data ──────────────────────────────────────────────────
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

    meetings_by_day: dict = {}
    for m in month_meetings:
        s = db.session.get(User, m.student_id)
        p = s.profile if s else None
        name = p.full_name if p and p.full_name else (s.username if s else "?")
        meetings_by_day.setdefault(m.scheduled_at.day, []).append({
            "id":       m.id,
            "name":     name,
            "time":     m.scheduled_at.strftime("%H:%M"),
            "duration": m.duration_min,
            "status":   m.status,
            "notes":    m.notes or "",
        })

    # Sunday-first weeks (Israeli calendar)
    cal_weeks = _cal.Calendar(firstweekday=6).monthdayscalendar(year, month)

    MONTHS_HE = {1:"ינואר",2:"פברואר",3:"מרץ",4:"אפריל",5:"מאי",6:"יוני",
                 7:"יולי",8:"אוגוסט",9:"ספטמבר",10:"אוקטובר",11:"נובמבר",12:"דצמבר"}

    upcoming = (Meeting.query
                .filter(Meeting.scheduled_at >= datetime.utcnow())
                .filter(Meeting.status != "cancelled")
                .order_by(Meeting.scheduled_at).limit(20).all())

    return render_template("admin_schedule.html",
        user=current_user(),
        students=students,
        upcoming=upcoming,
        cal_weeks=cal_weeks,
        meetings_by_day=meetings_by_day,
        year=year, month=month,
        month_name=MONTHS_HE[month],
        prev_year=prev_year, prev_month=prev_month,
        next_year=next_year, next_month=next_month,
    )


@app.route("/admin/students", methods=["POST"])
@login_required
@admin_required
def admin_create_student():
    username  = request.form.get("username", "").strip()
    password  = request.form.get("password", "").strip()
    full_name = request.form.get("full_name", "").strip()
    phone     = request.form.get("phone", "").strip()

    if not username or not password:
        flash("שם משתמש וסיסמה הם שדות חובה.", "warning")
        return redirect(url_for("admin_dashboard"))
    if len(password) < 6:
        flash("הסיסמה חייבת להכיל לפחות 6 תווים.", "warning")
        return redirect(url_for("admin_dashboard"))
    if User.query.filter_by(username=username).first():
        flash(f"שם המשתמש '{username}' כבר תפוס.", "warning")
        return redirect(url_for("admin_dashboard"))

    user = User(username=username,
                password=generate_password_hash(password),
                role="student")
    db.session.add(user)
    db.session.flush()

    if full_name or phone:
        profile = StudentProfile(
            user_id=user.id,
            full_name=full_name,
            phone=_normalize_phone(phone) if phone else "",
        )
        db.session.add(profile)

    db.session.commit()
    flash(f"✓ סטודנט '{username}' נוצר בהצלחה! ישלח/תשלח לו/ה את פרטי הכניסה.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/meeting/<int:meeting_id>/confirm")
def meeting_confirm(meeting_id):
    meeting = Meeting.query.get_or_404(meeting_id)
    token   = request.args.get("token", "")

    if token != _meeting_token(meeting_id):
        abort(403)

    if meeting.status == "pending":
        meeting.status = "confirmed"
        db.session.commit()
        # Notify student
        student = db.session.get(User, meeting.student_id)
        if student and student.profile and student.profile.phone:
            dt_str = meeting.scheduled_at.strftime("%d/%m/%Y בשעה %H:%M")
            name   = student.profile.full_name or student.username
            send_whatsapp(student.profile.phone,
                f"✅ {name}, הפגישה ב-{dt_str} אושרה! נתראה אז.")

    return render_template("meeting_confirm.html",
        meeting=meeting,
        already_confirmed=(meeting.status == "confirmed"),
    )


# ─────────────────────────────────────────────
# Seed & Init
# ─────────────────────────────────────────────

def seed_db():
    if not User.query.filter_by(username="admin").first():
        db.session.add(User(username="admin",
                            password=generate_password_hash("admin123"),
                            role="admin"))
    if not User.query.filter_by(username="student1").first():
        db.session.add(User(username="student1",
                            password=generate_password_hash("student123"),
                            role="student"))

    if TaskBank.query.count() == 0:
        starters = [
            TaskBank(title="עדכון קורות חיים", description="עיון ועדכון קורות החיים הקיימים לפי הנחיות המנטור.", category="קורות חיים", task_type="task"),
            TaskBank(title="כתיבת סיכום מקצועי", description="כתיבת פסקת סיכום מקצועי (Professional Summary) בראש קורות החיים.", category="קורות חיים", task_type="exercise"),
            TaskBank(title="הכנת פרופיל LinkedIn", description="יצירה ומילוי מלא של פרופיל LinkedIn כולל תמונה, כותרת ותיאור.", category="LinkedIn", task_type="task"),
            TaskBank(title='כתיבת קטע "About" ב-LinkedIn', description="כתיבת קטע About ייחודי ומושך שמשקף את הזהות המקצועית שלך.", category="LinkedIn", task_type="exercise"),
            TaskBank(title="הכנה לשאלות HR נפוצות", description="חקור ותרגל תשובות ל-10 שאלות HR הנפוצות ביותר בראיונות.", category="הכנה לראיון", task_type="task"),
            TaskBank(title='תרגול הצגה עצמית — "ספר/י על עצמך"', description="כתוב/י ותרגל/י הצגה עצמית ממוקדת של דקה אחת.", category="הכנה לראיון", task_type="exercise"),
            TaskBank(title="שאלות לשאול בסיום ראיון", description="הכן/י רשימה של 5 שאלות חכמות לשאול המראיין בסיום הראיון.", category="הכנה לראיון", task_type="task"),
            TaskBank(title="הגדרת מטרות לחודש הקרוב", description="שאלון עצמי: מה אני רוצה להשיג בחיפוש העבודה בחודש הקרוב?", category="שאלון", task_type="reflection"),
            TaskBank(title="הכישורים הייחודיים שלי", description="כתוב/י 5 כישורים ייחודיים שאתה/את מביא/ה לתפקיד הבא, עם דוגמה לכל כישור.", category="שאלון", task_type="reflection"),
            TaskBank(title="מחקר חברות יעד", description="זהה 5 חברות שמעניינות אותך ומצא/י פרטים על התרבות הארגונית ומשרות פתוחות.", category="כללי", task_type="task"),
        ]
        db.session.add_all(starters)
    db.session.commit()


with app.app_context():
    db.create_all()
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(os.path.join(UPLOAD_FOLDER, "tasks"), exist_ok=True)

    # Safe migrations for new columns on existing databases
    with db.engine.connect() as conn:
        for stmt in [
            "ALTER TABLE student_profiles ADD COLUMN process_start_date DATE",
            "ALTER TABLE student_profiles ADD COLUMN target_end_date DATE",
            "ALTER TABLE student_profiles ADD COLUMN mentor_notes TEXT DEFAULT ''",
            "ALTER TABLE assigned_tasks ADD COLUMN submission_note TEXT DEFAULT ''",
            "ALTER TABLE assigned_tasks ADD COLUMN submission_file TEXT DEFAULT ''",
            "ALTER TABLE task_bank ADD COLUMN resource_file TEXT DEFAULT ''",
        ]:
            try:
                conn.execute(db.text(stmt))
                conn.commit()
            except Exception:
                conn.rollback()

    seed_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000,
            debug=os.environ.get("FLASK_DEBUG", "false").lower() == "true")
