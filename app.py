import os
import json
from datetime import datetime
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from openai import OpenAI

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///interviewsync.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)


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


class StudentProfile(db.Model):
    __tablename__ = "student_profiles"
    id                          = db.Column(db.Integer, primary_key=True)
    user_id                     = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    full_name                   = db.Column(db.String(120), default="")
    email                       = db.Column(db.String(120), default="")
    phone                       = db.Column(db.String(30), default="")   # WhatsApp e.g. +972501234567
    education_level             = db.Column(db.String(20), default="")   # "highschool" | "college"
    current_occupation_or_grade = db.Column(db.String(200), default="") # כיתה OR תואר+תחום
    career_goals                = db.Column(db.Text, default="")
    fears_weaknesses            = db.Column(db.Text, default="")
    ai_coaching_strategy        = db.Column(db.Text, default="")        # shown to admin only
    resume_content              = db.Column(db.Text, default="")        # admin pastes/edits CV
    created_at                  = db.Column(db.DateTime, default=datetime.utcnow)


class TaskBank(db.Model):
    __tablename__ = "task_bank"
    id          = db.Column(db.Integer, primary_key=True)
    title       = db.Column(db.String(512), nullable=False)
    description = db.Column(db.Text, default="")
    category    = db.Column(db.String(100), default="כללי")
    # categories: קורות חיים | LinkedIn | הכנה לראיון | שאלון | כללי
    task_type   = db.Column(db.String(30), default="task")
    # task_type: "task" | "reflection" | "exercise"
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    assignments = db.relationship("AssignedTask", backref="task", lazy=True)


class AssignedTask(db.Model):
    __tablename__ = "assigned_tasks"
    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    task_id      = db.Column(db.Integer, db.ForeignKey("task_bank.id"), nullable=False)
    status       = db.Column(db.String(20), default="pending")  # "pending" | "completed"
    assigned_at  = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)
    __table_args__ = (db.UniqueConstraint("user_id", "task_id", name="uq_assigned_task"),)


# ─────────────────────────────────────────────
# AI helpers
# ─────────────────────────────────────────────

def _get_ai_client():
    key = os.environ.get("AI_API_KEY")
    return OpenAI(api_key=key) if key else None


def _parse_ai_json(raw: str) -> list:
    """Strip markdown fences and parse JSON array. Returns [] on any failure."""
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
    """Hebrew coaching strategy for admin. Graceful fallback if no API key."""
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
    """Return a list of task dicts tailored to this student. Returns [] on failure."""
    client = _get_ai_client()
    if not client:
        return []

    edu = "תיכון" if profile.education_level == "highschool" else "מכללה/אוניברסיטה"
    prompt = f"""אתה יועץ קריירה. צור בדיוק {count} משימות הכנה מותאמות לסטודנט:
רמה: {edu} | שלב: {profile.current_occupation_or_grade} | מטרות: {profile.career_goals} | חולשות: {profile.fears_weaknesses}

בחר בתבונה את סוג כל משימה (task/reflection/exercise) בהתאם למה שהסטודנט צריך.

החזר JSON בלבד (ללא מרקדאון):
[{{"title":"...","description":"...","category":"קורות חיים|LinkedIn|הכנה לראיון|שאלון|כללי","task_type":"task|reflection|exercise"}}]"""

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000, temperature=0.8,
        )
        result = _parse_ai_json(resp.choices[0].message.content)
        return result[:count]
    except Exception as e:
        app.logger.error("ai_generate_tasks_for_student: %s", e)
        return []


# ─────────────────────────────────────────────
# WhatsApp (Twilio)
# ─────────────────────────────────────────────

def send_whatsapp(phone: str, message: str) -> bool:
    """
    Send a WhatsApp message via Twilio.
    Requires: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM
    Returns True on success, False if not configured or failed.
    """
    sid   = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_ = os.environ.get("TWILIO_WHATSAPP_FROM")  # e.g. "whatsapp:+14155238886"

    if not all([sid, token, from_, phone]):
        return False

    try:
        from twilio.rest import Client
        client = Client(sid, token)
        to = f"whatsapp:{phone}" if not phone.startswith("whatsapp:") else phone
        client.messages.create(body=message, from_=from_, to=to)
        return True
    except Exception as e:
        app.logger.error("WhatsApp send failed: %s", e)
        return False


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
        full_name   = request.form.get("full_name", "").strip()
        email       = request.form.get("email", "").strip()
        phone       = request.form.get("phone", "").strip()
        edu_level   = request.form.get("education_level", "").strip()
        occupation  = request.form.get("current_occupation_or_grade", "").strip()
        goals       = request.form.get("career_goals", "").strip()
        weaknesses  = request.form.get("fears_weaknesses", "").strip()

        if not (full_name and edu_level and goals):
            flash("יש למלא את כל השדות המסומנים כחובה.", "warning")
            return redirect(url_for("onboarding"))

        profile = user.profile or StudentProfile(user_id=user.id)
        profile.full_name                   = full_name
        profile.email                       = email
        profile.phone                       = phone
        profile.education_level             = edu_level
        profile.current_occupation_or_grade = occupation
        profile.career_goals                = goals
        profile.fears_weaknesses            = weaknesses
        profile.ai_coaching_strategy        = ai_generate_coaching_strategy(profile)

        if not user.profile:
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
        task_id = request.form.get("task_id", type=int)
        at = AssignedTask.query.filter_by(user_id=user.id, task_id=task_id).first_or_404()
        if at.status == "pending":
            at.status       = "completed"
            at.completed_at = datetime.utcnow()
            db.session.commit()
            flash("כל הכבוד! המשימה סומנה כהושלמה ✓", "success")
        return redirect(url_for("index"))

    active    = (AssignedTask.query
                 .filter_by(user_id=user.id, status="pending")
                 .order_by(AssignedTask.assigned_at.desc()).all())
    completed = (AssignedTask.query
                 .filter_by(user_id=user.id, status="completed")
                 .order_by(AssignedTask.completed_at.desc()).all())

    return render_template("index.html",
        user=user,
        profile=user.profile,
        active=active,
        completed=completed,
        total=len(active) + len(completed),
    )


# ─────────────────────────────────────────────
# Routes — Admin
# ─────────────────────────────────────────────

@app.route("/admin")
@login_required
@admin_required
def admin_dashboard():
    students  = User.query.filter_by(role="student").order_by(User.username).all()
    taskbank  = TaskBank.query.order_by(TaskBank.category, TaskBank.title).all()

    # Build per-student progress summary
    progress = {}
    for s in students:
        total     = AssignedTask.query.filter_by(user_id=s.id).count()
        done      = AssignedTask.query.filter_by(user_id=s.id, status="completed").count()
        progress[s.id] = {"total": total, "done": done,
                           "pct": round(done / total * 100) if total else 0}

    categories = sorted({t.category for t in taskbank})
    return render_template("admin.html",
        user=current_user(),
        students=students,
        taskbank=taskbank,
        progress=progress,
        categories=categories,
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
            db.session.add(TaskBank(title=title, description=desc,
                                    category=category, task_type=ttype))
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
            # Add new assignments (preserve existing completed ones)
            existing = {at.task_id: at for at in AssignedTask.query.filter_by(user_id=student_id).all()}
            # Remove unchecked that are still pending
            for tid, at in existing.items():
                if tid not in checked_ids and at.status == "pending":
                    db.session.delete(at)
            # Add newly checked that don't exist yet
            new_ids = [tid for tid in checked_ids if tid not in existing]
            for tid in new_ids:
                db.session.add(AssignedTask(user_id=student_id, task_id=tid))
            db.session.commit()

            # WhatsApp notification for newly assigned tasks
            if new_ids and profile and profile.phone:
                name = profile.full_name or student.username
                msg  = f"שלום {name}! המנטור שלך הוסיף לך {len(new_ids)} משימות חדשות. היכנס/י למערכת לצפייה 📋"
                sent = send_whatsapp(profile.phone, msg)
                if sent:
                    flash(f"שויכו {len(new_ids)} משימות ונשלחה הודעת WhatsApp.", "success")
                else:
                    flash(f"שויכו {len(new_ids)} משימות. WhatsApp לא מוגדר — ההודעה לא נשלחה.", "warning")
            else:
                flash("המשימות עודכנו בהצלחה.", "success")

        elif action == "save_resume":
            content = request.form.get("resume_content", "")
            if profile:
                profile.resume_content = content
                db.session.commit()
                flash("קורות החיים נשמרו.", "success")
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
    active    = (AssignedTask.query
                 .filter_by(user_id=student_id, status="pending")
                 .order_by(AssignedTask.assigned_at).all())
    completed = (AssignedTask.query
                 .filter_by(user_id=student_id, status="completed")
                 .order_by(AssignedTask.completed_at.desc()).all())
    categories = sorted({t.category for t in taskbank})

    return render_template("student_file.html",
        user=current_user(),
        student=student,
        profile=profile,
        taskbank=taskbank,
        assigned_ids=assigned_ids,
        active=active,
        completed=completed,
        categories=categories,
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
        task = TaskBank(
            title=title,
            description=s.get("description", ""),
            category=s.get("category", "כללי"),
            task_type=s.get("task_type", "task"),
        )
        db.session.add(task)
        db.session.flush()  # get task.id
        # Auto-assign to this student
        db.session.add(AssignedTask(user_id=student_id, task_id=task.id))
        created += 1

    db.session.commit()

    if profile.phone and created:
        name = profile.full_name or student.username
        msg  = f"שלום {name}! המנטור שלך יצר עבורך {created} משימות חדשות בעזרת AI 🤖 היכנס/י לצפייה."
        send_whatsapp(profile.phone, msg)

    flash(f"נוצרו ושויכו {created} משימות AI לסטודנט.", "success")
    return redirect(url_for("student_file", student_id=student_id))


# ─────────────────────────────────────────────
# Seed & Init
# ─────────────────────────────────────────────

def seed_db():
    if not User.query.filter_by(username="admin").first():
        db.session.add(User(
            username="admin",
            password=generate_password_hash("admin123"),
            role="admin",
        ))
    if not User.query.filter_by(username="student1").first():
        db.session.add(User(
            username="student1",
            password=generate_password_hash("student123"),
            role="student",
        ))

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
    seed_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000,
            debug=os.environ.get("FLASK_DEBUG", "false").lower() == "true")
