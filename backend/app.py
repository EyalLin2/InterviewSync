import os
import json
import hmac
import hashlib
import calendar as _cal
import logging
from contextlib import asynccontextmanager
from datetime import datetime, date, timedelta
from typing import Optional

import jwt
from fastapi import FastAPI, Depends, HTTPException, Form, File, UploadFile, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import text
from sqlalchemy.orm import Session
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from groq import Groq

from models import (
    Base,
    User, StudentProfile, TaskBank, AssignedTask, Meeting,
    MentorNote, TaskComment,
    Workshop, Inquiry, ActivityLog, TOPIC_CATEGORIES,
    Service, StudentBilling, BillingRecord,
    SessionLocal, engine,
)

logger = logging.getLogger("interviewsync")

SECRET_KEY    = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "/app/uploads")
ALLOWED_EXT   = {"pdf", "doc", "docx", "png", "jpg", "jpeg", "txt"}


# ─────────────────────────────────────────────
# DB dependency
# ─────────────────────────────────────────────

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ─────────────────────────────────────────────
# JWT helpers
# ─────────────────────────────────────────────

def create_token(user_id: int, role: str) -> str:
    payload = {
        "sub":  str(user_id),
        "role": role,
        "exp":  datetime.utcnow() + timedelta(days=7),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None


security = HTTPBearer(auto_error=False)


def get_current_user_id(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> int:
    if not credentials:
        raise HTTPException(status_code=401, detail="Unauthorized")
    payload = decode_token(credentials.credentials)
    if not payload:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return int(payload["sub"])


def require_admin_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> int:
    if not credentials:
        raise HTTPException(status_code=401, detail="Unauthorized")
    payload = decode_token(credentials.credentials)
    if not payload:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return int(payload["sub"])


def _or_404(obj, detail="not found"):
    if not obj:
        raise HTTPException(status_code=404, detail=detail)
    return obj


# ─────────────────────────────────────────────
# File upload helpers
# ─────────────────────────────────────────────

def _allowed(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def _save_upload(file: Optional[UploadFile], subdir: str, prefix: str) -> str:
    if not file or not file.filename or not _allowed(file.filename):
        return ""
    dest = os.path.join(UPLOAD_FOLDER, subdir)
    os.makedirs(dest, exist_ok=True)
    safe = secure_filename(f"{prefix}_{file.filename}")
    content = file.file.read()
    with open(os.path.join(dest, safe), "wb") as f:
        f.write(content)
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
    else:
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
        logger.error("Groq coaching strategy error: %s", e)
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
        logger.error("Groq generate tasks error: %s", e)
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
    task = at.task
    comments = at.comments if at.comments is not None else []
    has_admin_reply = any(c.author_role == "admin" and not c.is_read for c in comments)
    return {
        "id": at.id, "task_id": at.task_id,
        "task": _task_dict(task) if task else {
            "id": at.task_id, "title": "משימה מחוקה", "description": "",
            "category": "כללי", "task_type": "task", "resource_file": "",
            "assigned_count": 0, "completed_count": 0, "completion_rate": 0,
        },
        "status": at.status,
        "assigned_at":  at.assigned_at.isoformat()  if at.assigned_at  else None,
        "completed_at": at.completed_at.isoformat() if at.completed_at else None,
        "submission_note": at.submission_note, "submission_file": at.submission_file,
        "feedback": at.feedback or "",
        "feedback_at": at.feedback_at.isoformat() if at.feedback_at else None,
        "feedback_seen": bool(at.feedback_seen),
        "due_date": at.due_date.isoformat() if at.due_date else None,
        "comment_count": len(comments),
        "has_admin_reply": has_admin_reply,
    }


def _meeting_dict(m: Meeting, db: Session, include_token: bool = False) -> dict:
    s = db.get(User, m.student_id)
    p = s.profile if s else None
    d = {
        "id": m.id, "student_id": m.student_id,
        "student_name": (p.full_name if p and p.full_name else (s.username if s else "?")),
        "scheduled_at": m.scheduled_at.isoformat(),
        "duration_min": m.duration_min,
        "notes": m.notes, "status": m.status,
        "meeting_type": m.meeting_type or "progress_review",
        "outcome_notes": m.outcome_notes or "",
        "action_items":  m.action_items  or "",
    }
    if include_token:
        d["token"] = meeting_token(m.id)
    return d


# ─────────────────────────────────────────────
# Scheduler
# ─────────────────────────────────────────────

def check_inactive_students():
    threshold = int(os.environ.get("INACTIVITY_DAYS", 7))
    cutoff    = datetime.utcnow() - timedelta(days=threshold)
    today     = date.today()

    with SessionLocal() as db:
        students = db.query(User).filter_by(role="student").all()
        for s in students:
            p = s.profile
            if not p or not p.phone:
                continue
            if p.last_reminder_sent == today:
                continue

            last_task = (db.query(AssignedTask)
                         .filter_by(user_id=s.id, status="completed")
                         .order_by(AssignedTask.completed_at.desc()).first())
            last_meeting = (db.query(Meeting)
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
                continue

            pending = db.query(AssignedTask).filter_by(user_id=s.id, status="pending").count()
            if pending == 0:
                continue

            name = p.full_name or s.username
            oldest = (db.query(AssignedTask)
                      .filter_by(user_id=s.id, status="pending")
                      .join(TaskBank).order_by(AssignedTask.id).first())
            task_hint = (f"המשימה '{oldest.task.title}' מחכה לך"
                         if oldest and oldest.task
                         else f"יש לך {pending} משימות ממתינות")
            try:
                ok, _ = send_whatsapp(p.phone,
                    f"שלום {name}! לא ראינו אותך פעיל/ה בתוכנית לאחרונה. "
                    f"{task_hint} — היכנס/י ותמשיך/י 💪")
                if ok:
                    p.last_reminder_sent = today
                    db.commit()
                    logger.info("Inactivity reminder sent to student %s", s.username)
            except Exception as e:
                logger.error("Inactivity reminder failed for %s: %s", s.username, e)


def check_overdue_tasks():
    today = date.today()
    with SessionLocal() as db:
        overdue = (db.query(AssignedTask)
                   .filter_by(status="pending")
                   .filter(AssignedTask.due_date.isnot(None))
                   .filter(AssignedTask.due_date < today)
                   .all())
        notified = set()
        for at in overdue:
            if at.user_id in notified:
                continue
            s = db.get(User, at.user_id)
            p = s.profile if s else None
            if not p or not p.phone:
                continue
            name = p.full_name or s.username
            title = at.task.title if at.task else ""
            try:
                ok, _ = send_whatsapp(p.phone,
                    f"שלום {name}! המשימה '{title}' עברה את הדדליין — "
                    f"נסה/י לסיים אותה היום 💪")
                if ok:
                    notified.add(at.user_id)
                    logger.info("Overdue alert sent to student %s", s.username)
            except Exception as e:
                logger.error("Overdue alert failed for %s: %s", s.username, e)


def _notify_category_milestone(uid: int, category: str, db: Session):
    all_in_cat = (db.query(AssignedTask)
                  .join(TaskBank)
                  .filter(AssignedTask.user_id == uid)
                  .filter(TaskBank.category == category)
                  .all())
    if not all_in_cat or not all(t.status == "completed" for t in all_in_cat):
        return
    s = db.get(User, uid)
    p = s.profile if s else None
    if not p or not p.phone:
        return
    name = p.full_name or s.username
    try:
        send_whatsapp(p.phone,
            f"כל הכבוד {name}! סיימת את כל משימות ה{category} 🎉 "
            f"המנטור שלך יצור קשר בקרוב לגבי הצעדים הבאים.")
        logger.info("Category milestone sent to student %s for %s", s.username, category)
    except Exception as e:
        logger.error("Category milestone failed for %s: %s", s.username, e)


# ─────────────────────────────────────────────
# Migrations
# ─────────────────────────────────────────────

def _run_migrations():
    with engine.connect() as conn:
        for stmt in [
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
            "ALTER TABLE student_profiles ADD COLUMN last_reminder_sent DATE",
            "ALTER TABLE assigned_tasks ADD COLUMN feedback TEXT DEFAULT ''",
            "ALTER TABLE assigned_tasks ADD COLUMN feedback_at TIMESTAMP",
            "ALTER TABLE assigned_tasks ADD COLUMN feedback_seen BOOLEAN DEFAULT FALSE",
            "ALTER TABLE assigned_tasks ADD COLUMN due_date DATE",
            "ALTER TABLE meetings ADD COLUMN outcome_notes TEXT DEFAULT ''",
            "ALTER TABLE meetings ADD COLUMN action_items TEXT DEFAULT ''",
            "ALTER TABLE task_bank ADD COLUMN is_global BOOLEAN DEFAULT TRUE",
        ]:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                conn.rollback()


# ─────────────────────────────────────────────
# Seed
# ─────────────────────────────────────────────

def seed_db(db: Session):
    if not db.query(User).filter_by(username="admin").first():
        db.add(User(username="admin",
                    password=generate_password_hash("admin123"), role="admin"))
    if not db.query(User).filter_by(username="student1").first():
        db.add(User(username="student1",
                    password=generate_password_hash("student123"), role="student"))
    if db.query(TaskBank).count() == 0:
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
        db.add_all(starters)

    if db.query(Service).count() == 0:
        db.add_all([
            Service(name="שיעור",        description="פגישה אישית — לפי שעה",       unit="per_session", price_highschool=150, price_college=200, price_career=250),
            Service(name="כתיבת ראיון",  description="כתיבת קורות חיים ומכתב כיסוי", unit="fixed",       price_highschool=400, price_college=500, price_career=700),
            Service(name="ליווי תעסוקי", description="חבילת ליווי חודשית",            unit="monthly",     price_highschool=600, price_college=800, price_career=1200),
            Service(name="חיפוש כיוון",  description="סדרת פגישות לבחירת כיוון",     unit="monthly",     price_highschool=500, price_college=700, price_career=1000),
            Service(name="תוכנית מלאה",  description="ליווי מלא — הכל כלול",         unit="monthly",     price_highschool=900, price_college=1200, price_career=1800),
        ])

    db.commit()


# ─────────────────────────────────────────────
# App + lifespan
# ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(os.path.join(UPLOAD_FOLDER, "tasks"), exist_ok=True)
    _run_migrations()
    with SessionLocal() as db:
        seed_db(db)
    if os.environ.get("TESTING") != "true":
        _scheduler.start()
    yield
    if os.environ.get("TESTING") != "true":
        _scheduler.shutdown(wait=False)


_scheduler = BackgroundScheduler(timezone="UTC", daemon=True)
_scheduler.add_job(check_inactive_students, "cron", hour=9, minute=0)
_scheduler.add_job(check_overdue_tasks, "cron", hour=9, minute=30)

app = FastAPI(title="InterviewSync API", version="2.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(status_code=exc.status_code, content={"error": str(exc.detail)})


# ─────────────────────────────────────────────
# Routes — Health
# ─────────────────────────────────────────────

@app.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:
        db_status = "error"
    return {"status": "ok", "db": db_status}


# ─────────────────────────────────────────────
# Routes — Auth
# ─────────────────────────────────────────────

@app.post("/api/auth/login")
def login(body: dict = Body(default={}), db: Session = Depends(get_db)):
    username = body.get("username", "").strip()
    password = body.get("password", "")
    user     = db.query(User).filter_by(username=username).first()
    if not user or not check_password_hash(user.password, password):
        raise HTTPException(status_code=401, detail="שם משתמש או סיסמה שגויים")
    p    = user.profile
    name = (p.full_name if p and p.full_name else user.username)
    return {
        "token":   create_token(user.id, user.role),
        "user_id": user.id,
        "role":    user.role,
        "name":    name,
        "profile_complete": bool(p and p.education_level and p.career_goals and p.full_name),
    }


@app.get("/api/auth/me")
def me(uid: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    user = _or_404(db.get(User, uid))
    return {
        "user_id":  user.id, "username": user.username, "role": user.role,
        "profile":  _profile_dict(user.profile),
        "profile_complete": bool(user.profile and user.profile.education_level
                                 and user.profile.career_goals and user.profile.full_name),
    }


@app.post("/api/auth/onboarding")
def save_onboarding(body: dict = Body(default={}), uid: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    user    = _or_404(db.get(User, uid))
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
        raise HTTPException(status_code=400, detail="Missing required fields")

    if is_new or not profile.process_start_date:
        profile.process_start_date = date.today()

    profile.ai_coaching_strategy = ai_coaching_strategy(profile)

    if is_new:
        db.add(profile)
    db.commit()
    return {"ok": True, "profile": _profile_dict(profile)}


# ─────────────────────────────────────────────
# Routes — Students (admin)
# ─────────────────────────────────────────────

@app.get("/api/students")
def list_students(uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    students = db.query(User).filter_by(role="student").order_by(User.username).all()
    now = datetime.utcnow()
    result = []
    for s in students:
        total = db.query(AssignedTask).filter_by(user_id=s.id).count()
        done  = db.query(AssignedTask).filter_by(user_id=s.id, status="completed").count()

        last_task = (db.query(AssignedTask).filter_by(user_id=s.id, status="completed")
                     .order_by(AssignedTask.completed_at.desc()).first())
        last_meeting = (db.query(Meeting).filter_by(student_id=s.id)
                        .filter(Meeting.status.in_(["confirmed", "completed"]))
                        .filter(Meeting.scheduled_at <= now)
                        .order_by(Meeting.scheduled_at.desc()).first())
        dates = [d for d in [
            last_task.completed_at    if last_task    else None,
            last_meeting.scheduled_at if last_meeting else None,
        ] if d]
        last_activity_dt   = max(dates) if dates else None
        last_activity_days = (now - last_activity_dt).days if last_activity_dt else None

        result.append({
            "id": s.id, "username": s.username,
            "profile": _profile_dict(s.profile),
            "progress": {"total": total, "done": done,
                         "pct": round(done / total * 100) if total else 0},
            "last_activity_days": last_activity_days,
        })
    return result


@app.delete("/api/students/{sid}")
def delete_student(sid: int, uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    student = _or_404(db.query(User).filter_by(id=sid, role="student").first())
    db.query(AssignedTask).filter_by(user_id=sid).delete()
    db.query(Meeting).filter_by(student_id=sid).delete()
    db.query(MentorNote).filter_by(student_id=sid).delete()
    db.query(BillingRecord).filter_by(student_id=sid).delete()
    db.query(StudentBilling).filter_by(student_id=sid).delete()
    if student.profile:
        db.delete(student.profile)
    db.delete(student)
    db.commit()
    return {"ok": True}


@app.post("/api/students", status_code=201)
def create_student(body: dict = Body(default={}), uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    username  = body.get("username", "").strip()
    password  = body.get("password", "").strip()
    full_name = body.get("full_name", "").strip()
    phone     = body.get("phone", "").strip()

    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password required")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="password must be at least 6 characters")
    if db.query(User).filter_by(username=username).first():
        raise HTTPException(status_code=409, detail=f"Username '{username}' already taken")

    user = User(username=username, password=generate_password_hash(password), role="student")
    db.add(user)
    db.flush()
    if full_name or phone:
        db.add(StudentProfile(
            user_id=user.id, full_name=full_name,
            phone=normalize_phone(phone) if phone else "",
        ))
    db.commit()
    return {"id": user.id, "username": user.username}


@app.get("/api/students/{sid}")
def get_student(sid: int, uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    student = _or_404(db.query(User).filter_by(id=sid, role="student").first())
    active    = [_assignment_dict(a) for a in
                 db.query(AssignedTask).filter_by(user_id=sid, status="pending").all()]
    completed = [_assignment_dict(a) for a in
                 db.query(AssignedTask).filter_by(user_id=sid, status="completed").all()]
    meetings  = [_meeting_dict(m, db) for m in
                 db.query(Meeting).filter_by(student_id=sid)
                 .filter(Meeting.scheduled_at >= datetime.utcnow())
                 .filter(Meeting.status != "cancelled")
                 .order_by(Meeting.scheduled_at).all()]
    assigned_ids = [a.task_id for a in db.query(AssignedTask).filter_by(user_id=sid).all()]
    return {
        "id": student.id, "username": student.username,
        "profile": _profile_dict(student.profile),
        "active": active, "completed": completed,
        "meetings": meetings, "assigned_ids": assigned_ids,
    }


@app.patch("/api/students/{sid}/profile")
def update_student_profile(sid: int, body: dict = Body(default={}), uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    student = _or_404(db.query(User).filter_by(id=sid, role="student").first())
    p = student.profile
    if not p:
        p = StudentProfile(user_id=sid)
        db.add(p)
    for field in ("process_start_date", "target_end_date"):
        if field in body:
            val = body[field]
            setattr(p, field, date.fromisoformat(val) if val else None)
    for field in ("mentor_notes", "student_status",
                  "career_goals", "fears_weaknesses", "full_name",
                  "email", "education_level", "current_occupation_or_grade"):
        if field in body:
            setattr(p, field, body[field])
    if "phone" in body:
        p.phone = normalize_phone(body["phone"]) if body["phone"] else ""
    db.commit()
    return {"ok": True, "profile": _profile_dict(p)}


@app.patch("/api/students/{sid}/resume")
def update_resume(sid: int, body: dict = Body(default={}), uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    student = _or_404(db.query(User).filter_by(id=sid, role="student").first())
    p = student.profile
    if not p:
        raise HTTPException(status_code=404, detail="No profile")
    p.resume_content = body.get("resume_content", "")
    db.commit()
    return {"ok": True}


@app.get("/api/students/{sid}/notes")
def list_notes(sid: int, uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    _or_404(db.query(User).filter_by(id=sid, role="student").first())
    notes = (db.query(MentorNote).filter_by(student_id=sid)
             .order_by(MentorNote.created_at.desc()).all())
    return [{"id": n.id, "text": n.text, "created_at": n.created_at.isoformat()} for n in notes]


@app.post("/api/students/{sid}/notes", status_code=201)
def add_note(sid: int, body: dict = Body(default={}), uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    _or_404(db.query(User).filter_by(id=sid, role="student").first())
    text_val = body.get("text", "").strip()
    if not text_val:
        raise HTTPException(status_code=400, detail="text required")
    n = MentorNote(student_id=sid, text=text_val)
    db.add(n)
    db.commit()
    return {"id": n.id, "text": n.text, "created_at": n.created_at.isoformat()}


@app.delete("/api/notes/{nid}")
def delete_note(nid: int, uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    n = _or_404(db.get(MentorNote, nid))
    db.delete(n)
    db.commit()
    return {"ok": True}


@app.post("/api/students/{sid}/cv")
def upload_student_cv(sid: int, cv_file: UploadFile = File(None), uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    student = _or_404(db.query(User).filter_by(id=sid, role="student").first())
    p = student.profile
    if not p:
        raise HTTPException(status_code=404, detail="No profile")
    path = _save_upload(cv_file, str(sid), "cv")
    if not path:
        raise HTTPException(status_code=400, detail="Invalid or missing file (PDF/DOC only)")
    p.resume_file = path
    db.commit()
    return {"ok": True, "path": path}


@app.post("/api/students/{sid}/assignments")
def assign_tasks(sid: int, body: dict = Body(default={}), uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    student   = _or_404(db.query(User).filter_by(id=sid, role="student").first())
    task_ids  = body.get("task_ids", [])
    due_dates = body.get("due_dates", {})
    existing  = {at.task_id: at for at in db.query(AssignedTask).filter_by(user_id=sid).all()}

    for tid, at in existing.items():
        if tid not in task_ids and at.status == "pending":
            db.delete(at)

    new_ids = [tid for tid in task_ids if tid not in existing]
    for tid in new_ids:
        dd_str = due_dates.get(str(tid), "")
        try:
            dd = date.fromisoformat(dd_str) if dd_str else None
        except ValueError:
            dd = None
        db.add(AssignedTask(user_id=sid, task_id=tid, due_date=dd))

    for tid in task_ids:
        if tid in existing:
            dd_str = due_dates.get(str(tid), "")
            if dd_str:
                try:
                    existing[tid].due_date = date.fromisoformat(dd_str)
                except ValueError:
                    pass
            elif dd_str == "":
                existing[tid].due_date = None

    db.commit()

    notified = False
    p = student.profile
    if new_ids and p and p.phone:
        name = p.full_name or student.username
        ok, _ = send_whatsapp(p.phone,
            f"שלום {name}! המנטור שלך הוסיף {len(new_ids)} משימות חדשות. היכנס/י למערכת. 📋")
        notified = ok

    return {"assigned": len(new_ids), "notified": notified}


# ─────────────────────────────────────────────
# Routes — AI
# ─────────────────────────────────────────────

@app.post("/api/ai/coaching-strategy/{sid}")
def regen_strategy(sid: int, uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    student = _or_404(db.query(User).filter_by(id=sid, role="student").first())
    p = student.profile
    if not p:
        raise HTTPException(status_code=404, detail="No profile")
    if not _ai_client():
        raise HTTPException(status_code=503, detail="no_api_key")
    result = ai_coaching_strategy(p)
    if result:
        p.ai_coaching_strategy = result
        p.ai_strategy_updated_at = datetime.utcnow()
        db.commit()
    return {
        "strategy": p.ai_coaching_strategy or "",
        "updated_at": p.ai_strategy_updated_at.isoformat() if p.ai_strategy_updated_at else None,
    }


@app.post("/api/ai/tasks/{sid}/suggest")
def suggest_ai_tasks(sid: int, uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    student = _or_404(db.query(User).filter_by(id=sid, role="student").first())
    p = student.profile
    if not p:
        raise HTTPException(status_code=404, detail="No profile")
    suggestions = ai_generate_tasks(p, count=5)
    if not suggestions:
        raise HTTPException(status_code=503, detail="AI unavailable or no API key")
    return {"suggestions": suggestions}


@app.post("/api/ai/tasks/{sid}/confirm")
def confirm_ai_tasks(sid: int, body: dict = Body(default={}), uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    student      = _or_404(db.query(User).filter_by(id=sid, role="student").first())
    p            = student.profile
    tasks        = body.get("tasks", [])
    save_to_bank = body.get("save_to_bank", False)

    created = []
    for s in tasks:
        title = s.get("title", "").strip()
        if not title:
            continue
        task = TaskBank(
            title=title, description=s.get("description", ""),
            category=s.get("category", "כללי"), task_type=s.get("task_type", "task"),
            is_global=save_to_bank,
        )
        db.add(task)
        db.flush()
        db.add(AssignedTask(user_id=sid, task_id=task.id))
        created.append(_task_dict(task))

    db.commit()

    if p and p.phone and created:
        name = p.full_name or student.username
        send_whatsapp(p.phone, f"שלום {name}! המנטור הוסיף {len(created)} משימות חדשות. 📋")

    return {"created": len(created), "tasks": created, "saved_to_bank": save_to_bank}


@app.post("/api/ai/tasks/{sid}")
def gen_ai_tasks(sid: int, uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    student = _or_404(db.query(User).filter_by(id=sid, role="student").first())
    p = student.profile
    if not p:
        raise HTTPException(status_code=404, detail="No profile")
    suggestions = ai_generate_tasks(p, count=5)
    if not suggestions:
        raise HTTPException(status_code=503, detail="AI unavailable or no API key")

    created = []
    for s in suggestions:
        title = s.get("title", "").strip()
        if not title:
            continue
        task = TaskBank(title=title, description=s.get("description", ""),
                        category=s.get("category", "כללי"), task_type=s.get("task_type", "task"))
        db.add(task)
        db.flush()
        db.add(AssignedTask(user_id=sid, task_id=task.id))
        created.append(_task_dict(task))

    db.commit()

    p2 = student.profile
    if p2 and p2.phone and created:
        name = p2.full_name or student.username
        send_whatsapp(p2.phone, f"שלום {name}! המנטור יצר {len(created)} משימות AI חדשות. 🤖")

    return {"created": len(created), "tasks": created}


# ─────────────────────────────────────────────
# Routes — Task Bank
# ─────────────────────────────────────────────

@app.get("/api/tasks")
def list_tasks(uid: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    tasks = (db.query(TaskBank).filter(TaskBank.is_global.isnot(False))
             .order_by(TaskBank.category, TaskBank.title).all())
    return [_task_dict(t) for t in tasks]


@app.post("/api/tasks", status_code=201)
def create_task(body: dict = Body(default={}), uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    title = body.get("title", "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title required")
    t = TaskBank(title=title, description=body.get("description", ""),
                 category=body.get("category", "כללי"), task_type=body.get("task_type", "task"))
    db.add(t)
    db.commit()
    return _task_dict(t)


@app.patch("/api/tasks/{tid}")
def update_task(tid: int, body: dict = Body(default={}), uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    t = _or_404(db.get(TaskBank, tid))
    for field in ("title", "description", "category", "task_type"):
        if field in body:
            setattr(t, field, body[field])
    if body.get("clear_resource"):
        t.resource_file = ""
    db.commit()
    return _task_dict(t)


@app.delete("/api/tasks/{tid}")
def delete_task(tid: int, uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    t = _or_404(db.get(TaskBank, tid))
    db.query(AssignedTask).filter_by(task_id=tid).delete()
    db.delete(t)
    db.commit()
    return {"ok": True}


@app.post("/api/tasks/{tid}/resource")
def upload_task_resource(tid: int, file: UploadFile = File(None), uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    t    = _or_404(db.get(TaskBank, tid))
    path = _save_upload(file, "tasks", f"resource_{tid}")
    if path:
        t.resource_file = path
        db.commit()
        return {"path": path}
    raise HTTPException(status_code=400, detail="Invalid or missing file")


# ─────────────────────────────────────────────
# Routes — Student (own tasks)
# ─────────────────────────────────────────────

@app.get("/api/my/tasks")
def my_tasks(uid: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    active    = [_assignment_dict(a) for a in
                 db.query(AssignedTask).filter_by(user_id=uid, status="pending").all()]
    completed = [_assignment_dict(a) for a in
                 db.query(AssignedTask).filter_by(user_id=uid, status="completed")
                 .order_by(AssignedTask.completed_at.desc()).all()]
    upcoming  = [_meeting_dict(m, db) for m in
                 db.query(Meeting).filter_by(student_id=uid)
                 .filter(Meeting.scheduled_at >= datetime.utcnow())
                 .filter(Meeting.status != "cancelled")
                 .order_by(Meeting.scheduled_at).limit(3).all()]
    return {
        "active": active, "completed": completed,
        "total": len(active) + len(completed),
        "upcoming_meetings": upcoming,
    }


@app.post("/api/my/tasks/{tid}/submit")
def submit_task(
    tid: int,
    submission_note: str = Form(""),
    submission_file: UploadFile = File(None),
    uid: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    at = _or_404(db.query(AssignedTask).filter_by(user_id=uid, task_id=tid).first())
    if at.status == "pending":
        at.status          = "completed"
        at.completed_at    = datetime.utcnow()
        at.submission_note = submission_note
        path = _save_upload(submission_file, str(uid), f"task_{tid}")
        if path:
            at.submission_file = path
        db.commit()
        if at.task and at.task.category:
            _notify_category_milestone(uid, at.task.category, db)
    return {"ok": True}


@app.patch("/api/admin/assignments/{at_id}/complete")
def admin_complete_task(at_id: int, body: dict = Body(default={}), uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    at   = _or_404(db.get(AssignedTask, at_id))
    note = body.get("note", "")
    at.status       = "completed"
    at.completed_at = datetime.utcnow()
    if note:
        at.submission_note = note
    db.commit()
    return {"ok": True, "completed_at": at.completed_at.isoformat()}


@app.patch("/api/admin/assignments/{at_id}/feedback")
def set_task_feedback(at_id: int, body: dict = Body(default={}), uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    at            = _or_404(db.get(AssignedTask, at_id))
    feedback_text = body.get("feedback", "").strip()
    if not feedback_text:
        raise HTTPException(status_code=400, detail="feedback required")
    at.feedback      = feedback_text
    at.feedback_at   = datetime.utcnow()
    at.feedback_seen = False
    db.commit()

    student = db.get(User, at.user_id)
    p = student.profile if student else None
    if p and p.phone and at.task:
        send_whatsapp(p.phone,
            f"המנטור שלך השאיר/ה פידבק על המשימה '{at.task.title}' — התחבר/י לצפות 👇")

    return {"ok": True, "feedback_at": at.feedback_at.isoformat()}


@app.post("/api/my/tasks/{tid}/feedback-seen")
def mark_feedback_seen(tid: int, uid: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    at = _or_404(db.query(AssignedTask).filter_by(task_id=tid, user_id=uid).first())
    at.feedback_seen = True
    db.commit()
    return {"ok": True}


@app.get("/api/my/tasks/{tid}/comments")
def get_my_task_comments(tid: int, uid: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    at = _or_404(db.query(AssignedTask).filter_by(task_id=tid, user_id=uid).first())
    for c in at.comments:
        if c.author_role == "admin" and not c.is_read:
            c.is_read = True
    db.commit()
    return [{"id": c.id, "author_role": c.author_role, "message": c.message,
             "created_at": c.created_at.isoformat()} for c in at.comments]


@app.post("/api/my/tasks/{tid}/comments")
def post_my_task_comment(tid: int, body: dict = Body(default={}), uid: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    at = _or_404(db.query(AssignedTask).filter_by(task_id=tid, user_id=uid).first())
    msg = body.get("message", "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="message required")
    c = TaskComment(assigned_task_id=at.id, author_role="student", message=msg)
    db.add(c)
    db.commit()
    return {"ok": True, "id": c.id, "created_at": c.created_at.isoformat()}


@app.get("/api/students/{sid}/tasks/{tid}/comments")
def admin_get_task_comments(sid: int, tid: int, uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    at = _or_404(db.query(AssignedTask).filter_by(task_id=tid, user_id=sid).first())
    return [{"id": c.id, "author_role": c.author_role, "message": c.message,
             "created_at": c.created_at.isoformat(), "is_read": c.is_read}
            for c in at.comments]


@app.post("/api/students/{sid}/tasks/{tid}/comments")
def admin_post_task_comment(sid: int, tid: int, body: dict = Body(default={}), uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    at = _or_404(db.query(AssignedTask).filter_by(task_id=tid, user_id=sid).first())
    msg = body.get("message", "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="message required")
    c = TaskComment(assigned_task_id=at.id, author_role="admin", message=msg)
    db.add(c)
    for existing in at.comments:
        if existing.author_role == "student" and not existing.is_read:
            existing.is_read = True
    db.commit()
    student = db.get(User, sid)
    p = student.profile if student else None
    if p and p.phone:
        task = db.get(TaskBank, tid)
        send_whatsapp(p.phone,
            f"המנטור שלך הגיב/ה על '{task.title if task else 'משימה'}' — התחבר/י לראות 👇")
    return {"ok": True, "id": c.id}


@app.get("/api/my/progress")
def my_progress(uid: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    all_tasks = db.query(AssignedTask).filter_by(user_id=uid).all()
    today     = date.today()
    fortnight = datetime.utcnow() - timedelta(days=14)
    total     = len(all_tasks)
    done      = sum(1 for t in all_tasks if t.status == "completed")
    overdue   = sum(1 for t in all_tasks if t.status == "pending" and t.due_date and t.due_date < today)
    recent_done = sum(1 for t in all_tasks if t.status == "completed" and t.completed_at and t.completed_at >= fortnight)

    categories: dict[str, dict] = {}
    for at in all_tasks:
        cat = at.task.category if at.task else "כללי"
        if cat not in categories:
            categories[cat] = {"total": 0, "done": 0}
        categories[cat]["total"] += 1
        if at.status == "completed":
            categories[cat]["done"] += 1

    meetings        = db.query(Meeting).filter_by(student_id=uid).all()
    meetings_done   = sum(1 for m in meetings if m.status == "completed")
    meetings_upcoming = sum(1 for m in meetings if m.scheduled_at >= datetime.utcnow() and m.status != "cancelled")

    profile = db.query(StudentProfile).filter_by(user_id=uid).first()
    days_in_process = None
    if profile and profile.process_start_date:
        days_in_process = (today - profile.process_start_date).days

    return {
        "total": total, "done": done,
        "pct": int(done / total * 100) if total > 0 else 0,
        "overdue": overdue, "recent_done": recent_done,
        "categories": [{"name": k, "total": v["total"], "done": v["done"],
                         "pct": int(v["done"] / v["total"] * 100) if v["total"] > 0 else 0}
                        for k, v in categories.items()],
        "meetings_done": meetings_done,
        "meetings_upcoming": meetings_upcoming,
        "days_in_process": days_in_process,
    }


@app.post("/api/my/ai-chat")
def student_ai_chat(body: dict = Body(default={}), uid: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    client = _ai_client()
    if not client:
        raise HTTPException(status_code=503, detail="no_api_key")
    history = body.get("messages", [])
    new_msg = body.get("message", "").strip()
    if not new_msg:
        raise HTTPException(status_code=400, detail="message required")

    context = _build_student_context(uid, db)
    system_prompt = (
        "אתה עוזר AI אישי לסטודנט בתהליך ייעוץ קריירה. "
        "יש לך גישה לנתוני הסטודנט:\n\n"
        f"{context}\n\n"
        "ענה לסטודנט ישירות בגוף שני. תן עצות מעשיות ומעודדות. "
        "עזור לו/ה להבין את מצבו/ה ומה הצעדים הבאים. "
        "ענה בעברית קצרה וממוקדת."
    )
    messages = [{"role": "system", "content": system_prompt}]
    for msg in history[-8:]:
        if msg.get("role") in ("user", "assistant"):
            messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": new_msg})

    try:
        r = client.chat.completions.create(
            model="llama-3.3-70b-versatile", messages=messages,
            max_tokens=400, temperature=0.7,
        )
        return {"reply": r.choices[0].message.content.strip()}
    except Exception as e:
        logger.error("Student chat error: %s", e)
        raise HTTPException(status_code=500, detail=f"שגיאה: {str(e)[:100]}")


@app.patch("/api/my/profile")
def update_my_profile(body: dict = Body(default={}), uid: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    p = db.query(StudentProfile).filter_by(user_id=uid).first()
    if not p:
        raise HTTPException(status_code=404, detail="profile not found")
    allowed = ("full_name", "email", "phone", "career_goals", "fears_weaknesses")
    for field in allowed:
        if field in body:
            val = body[field].strip() if isinstance(body[field], str) else body[field]
            if field == "phone":
                val = normalize_phone(val) if val else ""
            setattr(p, field, val)
    db.commit()
    return {"ok": True}


@app.post("/api/my/password")
def change_my_password(body: dict = Body(default={}), uid: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    user       = _or_404(db.get(User, uid))
    current_pw = body.get("current_password", "")
    new_pw     = body.get("new_password", "")
    if not check_password_hash(user.password, current_pw):
        raise HTTPException(status_code=400, detail="הסיסמה הנוכחית שגויה")
    if len(new_pw) < 6:
        raise HTTPException(status_code=400, detail="הסיסמה החדשה חייבת להכיל לפחות 6 תווים")
    user.password = generate_password_hash(new_pw)
    db.commit()
    return {"ok": True}


@app.get("/api/my/meetings")
def my_meetings(uid: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    meetings = (db.query(Meeting).filter_by(student_id=uid)
                .filter(Meeting.status != "cancelled")
                .order_by(Meeting.scheduled_at).all())
    return [_meeting_dict(m, db, include_token=True) for m in meetings]


# ─────────────────────────────────────────────
# Routes — Meetings (admin)
# ─────────────────────────────────────────────

@app.get("/api/meetings")
def list_meetings(
    year:  int = Query(default=None),
    month: int = Query(default=None),
    uid: int = Depends(require_admin_user),
    db: Session = Depends(get_db),
):
    if year  is None: year  = date.today().year
    if month is None: month = date.today().month
    year  = max(2020, min(2035, year))
    month = max(1,    min(12,   month))

    prev_year  = year - 1 if month == 1 else year
    prev_month = 12        if month == 1 else month - 1
    next_year  = year + 1  if month == 12 else year
    next_month = 1         if month == 12 else month + 1

    from_dt = datetime(year, month, 1)
    to_dt   = datetime(next_year, next_month, 1)

    month_meetings = (db.query(Meeting)
                      .filter(Meeting.scheduled_at >= from_dt,
                              Meeting.scheduled_at <  to_dt,
                              Meeting.status != "cancelled")
                      .order_by(Meeting.scheduled_at).all())

    by_day: dict = {}
    for m in month_meetings:
        s = db.get(User, m.student_id)
        p = s.profile if s else None
        name = (p.full_name if p and p.full_name else (s.username if s else "?"))
        edu  = (p.education_level or "college") if p else "college"
        by_day.setdefault(m.scheduled_at.day, []).append({
            "id": m.id, "name": name,
            "time": m.scheduled_at.strftime("%H:%M"),
            "duration": m.duration_min,
            "status": m.status, "notes": m.notes or "",
            "meeting_type":    m.meeting_type or "progress_review",
            "education_level": edu,
        })

    month_workshops = (db.query(Workshop)
                       .filter(Workshop.scheduled_at >= from_dt,
                               Workshop.scheduled_at <  to_dt,
                               Workshop.status != "cancelled")
                       .order_by(Workshop.scheduled_at).all())
    for w in month_workshops:
        if not w.scheduled_at:
            continue
        by_day.setdefault(w.scheduled_at.day, []).append({
            "id": w.id, "name": w.title,
            "time": w.scheduled_at.strftime("%H:%M"),
            "duration": 60,
            "status": w.status, "notes": w.notes or "",
            "event_type": "workshop",
            "meeting_type": "workshop",
            "education_level": "workshop",
            "workshop_type":  w.workshop_type or "one_time",
            "topic":          w.topic_category or "",
        })

    upcoming_workshops = [
        {"id": w.id, "name": w.title,
         "scheduled_at": w.scheduled_at.isoformat(),
         "duration_min": 60,
         "status": w.status, "notes": w.notes or "",
         "event_type": "workshop",
         "topic": w.topic_category or ""}
        for w in db.query(Workshop)
            .filter(Workshop.scheduled_at >= datetime.utcnow())
            .filter(Workshop.status != "cancelled")
            .order_by(Workshop.scheduled_at).limit(5).all()
        if w.scheduled_at
    ]

    cal_weeks = _cal.Calendar(firstweekday=6).monthdayscalendar(year, month)

    MONTHS_HE = {1:"ינואר",2:"פברואר",3:"מרץ",4:"אפריל",5:"מאי",6:"יוני",
                 7:"יולי",8:"אוגוסט",9:"ספטמבר",10:"אוקטובר",11:"נובמבר",12:"דצמבר"}

    upcoming = [_meeting_dict(m, db) for m in
                db.query(Meeting)
                .filter(Meeting.scheduled_at >= datetime.utcnow())
                .filter(Meeting.status != "cancelled")
                .order_by(Meeting.scheduled_at).limit(20).all()]

    students = [{"id": s.id,
                 "name": (s.profile.full_name if s.profile and s.profile.full_name else s.username)}
                for s in db.query(User).filter_by(role="student").order_by(User.username).all()]

    return {
        "cal_weeks": cal_weeks, "meetings_by_day": by_day,
        "year": year, "month": month, "month_name": MONTHS_HE[month],
        "prev_year": prev_year, "prev_month": prev_month,
        "next_year": next_year, "next_month": next_month,
        "upcoming": upcoming,
        "upcoming_workshops": upcoming_workshops,
        "students": students,
        "today": date.today().isoformat(),
    }


@app.post("/api/meetings", status_code=201)
def create_meeting(body: dict = Body(default={}), uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    sid       = body.get("student_id")
    sched_str = body.get("scheduled_at", "")
    if not sid or not sched_str:
        raise HTTPException(status_code=400, detail="student_id and scheduled_at required")
    try:
        scheduled_at = datetime.fromisoformat(sched_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid datetime format")

    student = _or_404(db.query(User).filter_by(id=sid, role="student").first())
    m = Meeting(student_id=sid, scheduled_at=scheduled_at,
                duration_min=body.get("duration_min", 60),
                notes=body.get("notes", ""),
                meeting_type=body.get("meeting_type", "progress_review"))
    db.add(m)
    db.commit()

    notified = False
    p = student.profile
    if p and p.phone:
        token        = meeting_token(m.id)
        frontend_url = os.environ.get("FRONTEND_URL", "http://localhost:5000")
        confirm_url  = f"{frontend_url}/meeting/{m.id}/confirm?token={token}"
        dt_str = scheduled_at.strftime("%d/%m/%Y בשעה %H:%M")
        name   = p.full_name or student.username
        ok, _  = send_whatsapp(p.phone,
            f"שלום {name}! נקבעה פגישה ביום {dt_str}.\nלאישור: {confirm_url}")
        notified = ok

    return {"id": m.id, "notified": notified}


@app.patch("/api/meetings/{mid}")
def update_meeting(mid: int, body: dict = Body(default={}), uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    m      = _or_404(db.get(Meeting, mid))
    action = body.get("action")

    if action == "cancel":
        m.status = "cancelled"
        db.commit()
        student = db.get(User, m.student_id)
        p = student.profile if student else None
        if p and p.phone:
            dt_str = m.scheduled_at.strftime("%d/%m/%Y %H:%M")
            name   = p.full_name or student.username
            send_whatsapp(p.phone, f"שלום {name}! הפגישה ב-{dt_str} בוטלה. נדבר בקרוב.")
        return {"ok": True}

    if action == "send_reminder":
        student = db.get(User, m.student_id)
        p = student.profile if student else None
        if not p or not p.phone:
            raise HTTPException(status_code=400, detail="no_phone")
        dt_str = m.scheduled_at.strftime("%d/%m/%Y בשעה %H:%M")
        name   = p.full_name or student.username
        ok, reason = send_whatsapp(p.phone,
            f"תזכורת 📅 שלום {name}! פגישה ב-{dt_str}. להתראות!")
        if ok:
            return {"ok": True}
        raise HTTPException(status_code=400 if reason == "no_config" else 500, detail=reason)

    if action == "mark_completed":
        m.status        = "completed"
        m.outcome_notes = body.get("outcome_notes", "").strip()
        m.action_items  = body.get("action_items", "").strip()
        db.commit()
        return {"ok": True}

    raise HTTPException(status_code=400, detail="Unknown action")


# ─────────────────────────────────────────────
# Routes — Meeting Confirmation (public)
# ─────────────────────────────────────────────

@app.get("/api/meetings/{mid}/confirm")
def confirm_meeting(mid: int, token: str = Query(default=""), db: Session = Depends(get_db)):
    m = _or_404(db.get(Meeting, mid))
    if token != meeting_token(mid):
        raise HTTPException(status_code=403, detail="Invalid token")

    already = (m.status == "confirmed")
    if not already and m.status == "pending":
        m.status = "confirmed"
        db.commit()
        student = db.get(User, m.student_id)
        p = student.profile if student else None
        if p and p.phone:
            dt_str = m.scheduled_at.strftime("%d/%m/%Y בשעה %H:%M")
            name   = p.full_name or student.username
            send_whatsapp(p.phone, f"✅ {name}, הפגישה ב-{dt_str} אושרה!")

    return {
        "already_confirmed": already or m.status == "confirmed",
        "meeting": {
            "scheduled_at": m.scheduled_at.isoformat(),
            "duration_min": m.duration_min, "notes": m.notes,
        },
    }


# ─────────────────────────────────────────────
# Routes — File Serving
# ─────────────────────────────────────────────

@app.get("/api/files/{filepath:path}")
def serve_file(filepath: str, uid: int = Depends(get_current_user_id)):
    full_path = os.path.join(UPLOAD_FOLDER, filepath)
    if not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(full_path)


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


@app.get("/api/business/overview")
def business_overview(uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    workshops_total  = db.query(Workshop).count()
    workshops_month  = db.query(Workshop).filter(Workshop.created_at >= month_start).count()
    inquiries_new    = db.query(Inquiry).filter_by(status="new").count()
    inquiries_total  = db.query(Inquiry).count()
    activities_month = db.query(ActivityLog).filter(ActivityLog.activity_date >= month_start.date()).count()

    upcoming = (db.query(Workshop)
                .filter(Workshop.scheduled_at >= now, Workshop.status != "cancelled")
                .order_by(Workshop.scheduled_at).limit(3).all())
    recent_inquiries = db.query(Inquiry).order_by(Inquiry.created_at.desc()).limit(5).all()

    return {
        "workshops_total":    workshops_total,
        "workshops_this_month": workshops_month,
        "inquiries_new":      inquiries_new,
        "inquiries_total":    inquiries_total,
        "activities_this_month": activities_month,
        "upcoming_workshops": [_workshop_dict(w) for w in upcoming],
        "recent_inquiries":   [_inquiry_dict(i) for i in recent_inquiries],
        "topic_categories":   TOPIC_CATEGORIES,
    }


@app.get("/api/workshops")
def list_workshops(uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    ws = db.query(Workshop).order_by(Workshop.created_at.desc()).all()
    return [_workshop_dict(w) for w in ws]


@app.post("/api/workshops", status_code=201)
def create_workshop(body: dict = Body(default={}), uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    title = body.get("title", "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title required")

    sched = None
    if body.get("scheduled_at"):
        try:
            sched = datetime.fromisoformat(body["scheduled_at"])
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid scheduled_at format")

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
    db.add(w)
    db.commit()
    return _workshop_dict(w)


@app.patch("/api/workshops/{wid}")
def update_workshop(wid: int, body: dict = Body(default={}), uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    w = _or_404(db.get(Workshop, wid))
    for field in ("title", "description", "topic_category", "workshop_type",
                  "status", "location", "notes"):
        if field in body:
            setattr(w, field, body[field])
    if "max_participants" in body:
        w.max_participants = body["max_participants"]
    if "scheduled_at" in body:
        val = body["scheduled_at"]
        w.scheduled_at = datetime.fromisoformat(val) if val else None
    db.commit()
    return _workshop_dict(w)


@app.delete("/api/workshops/{wid}")
def delete_workshop(wid: int, uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    w = _or_404(db.get(Workshop, wid))
    db.query(Inquiry).filter_by(workshop_id=wid).update({"workshop_id": None})
    db.query(ActivityLog).filter_by(workshop_id=wid).update({"workshop_id": None})
    db.delete(w)
    db.commit()
    return {"ok": True}


@app.get("/api/inquiries")
def list_inquiries(status: Optional[str] = Query(default=None), uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    q = db.query(Inquiry).order_by(Inquiry.created_at.desc())
    if status:
        q = q.filter_by(status=status)
    return [_inquiry_dict(i) for i in q.all()]


@app.post("/api/inquiries", status_code=201)
def create_inquiry(body: dict = Body(default={}), uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    name = body.get("full_name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="full_name required")
    i = Inquiry(
        full_name=name,
        phone=body.get("phone", ""),
        email=body.get("email", ""),
        topic=body.get("topic", ""),
        source=body.get("source", ""),
        notes=body.get("notes", ""),
    )
    db.add(i)
    db.commit()
    return _inquiry_dict(i)


@app.patch("/api/inquiries/{iid}")
def update_inquiry(iid: int, body: dict = Body(default={}), uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    i = _or_404(db.get(Inquiry, iid))
    for field in ("status", "notes", "topic", "phone", "email", "source"):
        if field in body:
            setattr(i, field, body[field])
    if "workshop_id" in body:
        i.workshop_id = body["workshop_id"]
        if body["workshop_id"] and i.status == "new":
            i.status = "assigned"
    db.commit()
    return _inquiry_dict(i)


@app.delete("/api/inquiries/{iid}")
def delete_inquiry(iid: int, uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    i = _or_404(db.get(Inquiry, iid))
    db.delete(i)
    db.commit()
    return {"ok": True}


@app.get("/api/activities")
def list_activities(uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    acts = db.query(ActivityLog).order_by(ActivityLog.activity_date.desc()).all()
    return [_activity_dict(a) for a in acts]


@app.post("/api/activities", status_code=201)
def create_activity(body: dict = Body(default={}), uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    title = body.get("title", "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title required")
    dt_raw = body.get("activity_date", "")
    if not dt_raw:
        raise HTTPException(status_code=400, detail="activity_date required")
    try:
        act_date = date.fromisoformat(dt_raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format")

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
    db.add(a)
    db.commit()
    return _activity_dict(a)


@app.patch("/api/activities/{aid}")
def update_activity(aid: int, body: dict = Body(default={}), uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    a = _or_404(db.get(ActivityLog, aid))
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
    db.commit()
    return _activity_dict(a)


@app.delete("/api/activities/{aid}")
def delete_activity(aid: int, uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    a = _or_404(db.get(ActivityLog, aid))
    db.delete(a)
    db.commit()
    return {"ok": True}


# ─────────────────────────────────────────────
# Dashboard Focus
# ─────────────────────────────────────────────

@app.get("/api/dashboard/focus")
def dashboard_focus(uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    now       = datetime.utcnow()
    threshold = int(os.environ.get("INACTIVITY_DAYS", 7))
    cutoff    = now - timedelta(days=threshold)

    students = db.query(User).filter_by(role="student").all()
    needs_attention = []
    for s in students:
        p = s.profile
        if not p or p.student_status == "completed":
            continue
        last_task = (db.query(AssignedTask).filter_by(user_id=s.id, status="completed")
                     .order_by(AssignedTask.completed_at.desc()).first())
        last_meeting = (db.query(Meeting).filter_by(student_id=s.id)
                        .filter(Meeting.status.in_(["confirmed", "completed"]))
                        .filter(Meeting.scheduled_at <= now)
                        .order_by(Meeting.scheduled_at.desc()).first())
        dates = [d for d in [
            last_task.completed_at    if last_task    else None,
            last_meeting.scheduled_at if last_meeting else None,
        ] if d]
        last_activity = max(dates) if dates else None
        if not last_activity or last_activity < cutoff:
            pending_count = db.query(AssignedTask).filter_by(user_id=s.id, status="pending").count()
            if pending_count:
                days = (now - last_activity).days if last_activity else None
                needs_attention.append({
                    "id": s.id,
                    "name": (p.full_name or s.username),
                    "days_inactive": days,
                    "pending_tasks": pending_count,
                })

    week_ago = now - timedelta(days=7)
    recent   = (db.query(AssignedTask)
                .filter(AssignedTask.status == "completed")
                .filter(AssignedTask.completed_at >= week_ago)
                .filter(
                    (AssignedTask.submission_note != "") |
                    (AssignedTask.submission_file != ""))
                .all())
    pending_submissions = []
    for at in recent:
        s = db.get(User, at.user_id)
        p = s.profile if s else None
        name = (p.full_name if p and p.full_name else (s.username if s else "?"))
        pending_submissions.append({
            "student_id":   at.user_id,
            "student_name": name,
            "task_title":   at.task.title if at.task else "?",
            "completed_at": at.completed_at.isoformat() if at.completed_at else None,
        })

    pending_confirmations = (db.query(Meeting)
                             .filter_by(status="pending")
                             .filter(Meeting.scheduled_at >= now)
                             .order_by(Meeting.scheduled_at).all())
    pending_meetings = []
    for m in pending_confirmations:
        s = db.get(User, m.student_id)
        p = s.profile if s else None
        name = (p.full_name if p and p.full_name else (s.username if s else "?"))
        pending_meetings.append({
            "meeting_id":   m.id,
            "student_id":   m.student_id,
            "student_name": name,
            "scheduled_at": m.scheduled_at.isoformat(),
        })

    today_d = date.today()
    overdue_ats = (db.query(AssignedTask)
                  .filter(AssignedTask.status == "pending")
                  .filter(AssignedTask.due_date.isnot(None))
                  .filter(AssignedTask.due_date < today_d)
                  .all())
    overdue_tasks = []
    for at in overdue_ats:
        s = db.get(User, at.user_id)
        p = s.profile if s else None
        overdue_tasks.append({
            "student_id":   at.user_id,
            "student_name": (p.full_name if p and p.full_name else (s.username if s else "?")),
            "task_title":   at.task.title if at.task else "?",
            "due_date":     at.due_date.isoformat(),
        })

    new_submissions_count = (db.query(AssignedTask)
                             .filter(AssignedTask.status == "completed")
                             .filter((AssignedTask.submission_note != "") | (AssignedTask.submission_file != ""))
                             .filter((AssignedTask.feedback == "") | (AssignedTask.feedback.is_(None)))
                             .count())

    return {
        "needs_attention":     sorted(needs_attention, key=lambda x: -(x["days_inactive"] or 999)),
        "pending_submissions": pending_submissions[:5],
        "pending_meetings":    pending_meetings[:5],
        "overdue_tasks":       overdue_tasks[:5],
        "new_submissions_count": new_submissions_count,
    }


@app.get("/api/submissions")
def list_submissions(uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    ats = (db.query(AssignedTask)
           .filter(AssignedTask.status == "completed")
           .filter((AssignedTask.submission_note != "") | (AssignedTask.submission_file != ""))
           .order_by(AssignedTask.completed_at.desc())
           .all())
    items = []
    for at in ats:
        s = db.get(User, at.user_id)
        p = s.profile if s else None
        items.append({
            **_assignment_dict(at),
            "student_id":   at.user_id,
            "student_name": (p.full_name if p and p.full_name else (s.username if s else "?")),
            "task_category": at.task.category if at.task else "כללי",
        })
    return items


@app.get("/api/dashboard/risk")
def dashboard_risk(uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    """Risk score for every active student — computed from DB, no AI calls."""
    now      = datetime.utcnow()
    today_d  = date.today()
    fortnight = now - timedelta(days=14)

    students = db.query(User).filter_by(role="student").all()
    result   = []

    for s in students:
        p = s.profile
        if not p or p.student_status in ("completed", "paused"):
            continue

        # Last activity
        last_task = (db.query(AssignedTask).filter_by(user_id=s.id, status="completed")
                     .order_by(AssignedTask.completed_at.desc()).first())
        last_meeting = (db.query(Meeting).filter_by(student_id=s.id)
                        .filter(Meeting.status.in_(["confirmed", "completed"]))
                        .filter(Meeting.scheduled_at <= now)
                        .order_by(Meeting.scheduled_at.desc()).first())
        dates = [d for d in [
            last_task.completed_at    if last_task    else None,
            last_meeting.scheduled_at if last_meeting else None,
        ] if d]
        last_activity = max(dates) if dates else None
        inactive_days = (now - last_activity).days if last_activity else None

        # Overdue + velocity
        active_tasks  = db.query(AssignedTask).filter_by(user_id=s.id, status="pending").all()
        overdue_count = sum(1 for a in active_tasks if a.due_date and a.due_date < today_d)
        recent_done   = db.query(AssignedTask).filter_by(user_id=s.id, status="completed") \
                          .filter(AssignedTask.completed_at >= fortnight).count()
        has_pending   = len(active_tasks) > 0

        # Scoring
        reasons = []
        if inactive_days is not None and inactive_days > 10:
            reasons.append(f"לא פעיל {inactive_days} ימים")
        elif inactive_days is None and has_pending:
            reasons.append("אף פעם לא היה פעיל")

        if overdue_count > 2:
            reasons.append(f"{overdue_count} משימות באיחור")
        elif overdue_count > 0:
            reasons.append(f"{overdue_count} משימה/ות באיחור")

        if has_pending and recent_done == 0 and (inactive_days is None or inactive_days > 7):
            reasons.append("0 השלמות ב-14 יום")

        # Determine level
        is_red    = (inactive_days is not None and inactive_days > 10) \
                    or (inactive_days is None and has_pending) \
                    or overdue_count > 2 \
                    or (has_pending and recent_done == 0 and (inactive_days is None or inactive_days > 10))
        is_yellow = not is_red and (
            (inactive_days is not None and inactive_days >= 7)
            or overdue_count >= 1
            or (has_pending and recent_done == 0))

        risk = "red" if is_red else ("yellow" if is_yellow else "green")

        if risk == "green":
            reason = f"פעיל לפני {inactive_days} ימים" if inactive_days is not None else "פעיל"
        else:
            reason = " | ".join(reasons) if reasons else "דורש מעקב"

        result.append({
            "id":     s.id,
            "name":   p.full_name or s.username,
            "risk":   risk,
            "reason": reason,
        })

    return result


@app.post("/api/students/bulk-status")
def bulk_student_status(body: dict = Body(default={}), uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    ids    = body.get("ids", [])
    status = body.get("status", "")
    if status not in ("active", "paused", "completed"):
        raise HTTPException(status_code=400, detail="invalid status")
    updated = 0
    for sid in ids:
        s = db.get(User, sid)
        if s and s.profile and s.role == "student":
            s.profile.student_status = status
            updated += 1
    db.commit()
    return {"ok": True, "updated": updated}


# ─────────────────────────────────────────────
# AI Chat
# ─────────────────────────────────────────────

def _build_student_context(sid: int, db: Session) -> str:
    student = db.query(User).filter_by(id=sid, role="student").first()
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

    now_dt   = datetime.utcnow()
    today_d  = date.today()
    fortnight = now_dt - timedelta(days=14)
    active    = db.query(AssignedTask).filter_by(user_id=sid, status="pending").all()
    all_done  = db.query(AssignedTask).filter_by(user_id=sid, status="completed").all()
    completed = sorted(all_done, key=lambda a: a.completed_at or datetime.min, reverse=True)[:5]

    recent_done  = [a for a in all_done if a.completed_at and a.completed_at >= fortnight]
    overdue_cnt  = sum(1 for a in active if a.due_date and a.due_date < today_d)
    feedback_cnt = sum(1 for a in all_done if a.feedback)
    cat_total:  dict[str, int] = {}
    cat_done:   dict[str, int] = {}
    for a in (active + all_done):
        cat = a.task.category if a.task else "כללי"
        cat_total[cat] = cat_total.get(cat, 0) + 1
        if a.status == "completed":
            cat_done[cat] = cat_done.get(cat, 0) + 1
    lines.append(f"\nסטטיסטיקות: {len(recent_done)} משימות ב-14 יום | {overdue_cnt} באיחור | {feedback_cnt} קיבלו פידבק מנטור")
    if cat_total:
        cat_summary = " | ".join(f"{c}: {cat_done.get(c,0)}/{cat_total[c]}" for c in cat_total)
        lines.append(f"פילוח קטגוריות: {cat_summary}")
    if p.target_end_date:
        days_left = (p.target_end_date - today_d).days
        lines.append(f"ימים עד סיום תהליך: {days_left}")

    all_meetings = db.query(Meeting).filter_by(student_id=sid).all()
    confirmed   = sum(1 for m in all_meetings if m.status == "confirmed")
    completed_m = sum(1 for m in all_meetings if m.status == "completed")
    pending_m   = sum(1 for m in all_meetings if m.status == "pending")
    if all_meetings:
        lines.append(f"פגישות: {confirmed} מאושרות | {completed_m} הושלמו | {pending_m} ממתינות")

    if active:
        lines.append("\nמשימות פעילות:")
        for a in active:
            t = a.task
            if not t:
                continue
            age = (now_dt - a.assigned_at).days if a.assigned_at else 0
            due_info     = f" | יעד: {a.due_date.isoformat()}" if a.due_date else ""
            overdue_mark = " ⚠ באיחור" if a.due_date and a.due_date < today_d else ""
            lines.append(f"  • {t.title} [{t.category}] — מחכה {age} ימים{due_info}{overdue_mark}")

    if completed:
        lines.append("\nמשימות שהושלמו לאחרונה:")
        for a in completed:
            if not a.task:
                continue
            note = f" | הגשה: {a.submission_note[:80]}" if a.submission_note else ""
            lines.append(f"  ✓ {a.task.title}{note}")

    notes = (db.query(MentorNote).filter_by(student_id=sid)
             .order_by(MentorNote.created_at.desc()).limit(4).all())
    if notes:
        lines.append("\nהערות מנטור אחרונות:")
        for n in notes:
            lines.append(f"  [{n.created_at.strftime('%d/%m')}] {n.text[:100]}")

    upcoming = (db.query(Meeting).filter_by(student_id=sid)
                .filter(Meeting.scheduled_at >= datetime.utcnow())
                .filter(Meeting.status != "cancelled")
                .order_by(Meeting.scheduled_at).limit(2).all())
    if upcoming:
        lines.append("\nפגישות קרובות:")
        for m in upcoming:
            lines.append(f"  📅 {m.scheduled_at.strftime('%d/%m %H:%M')} ({m.duration_min} דק׳)")

    if p.ai_coaching_strategy and len(p.ai_coaching_strategy) > 20:
        lines.append(f"\nאסטרטגיית הדרכה:\n{p.ai_coaching_strategy[:500]}")

    return "\n".join(lines)


@app.post("/api/students/{sid}/chat")
def student_chat(sid: int, body: dict = Body(default={}), uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    client = _ai_client()
    if not client:
        raise HTTPException(status_code=503, detail="no_api_key")

    history = body.get("messages", [])
    new_msg = body.get("message", "").strip()
    if not new_msg:
        raise HTTPException(status_code=400, detail="message required")

    context = _build_student_context(sid, db)
    system_prompt = (
        "אתה עוזר AI חכם למנטור קריירה. יש לך גישה לכל המידע על הסטודנט הבא:\n\n"
        f"{context}\n\n"
        "ענה בעברית קצרה וממוקדת. תן המלצות מעשיות. "
        "אם שואלים על התקדמות — נתח לפי הנתונים. "
        "אם שואלים על משימות — הסתמך על הרשימה. "
        "היה ישיר ואל תחזור על מה שכבר ידוע."
    )

    messages = [{"role": "system", "content": system_prompt}]
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
        return {"reply": r.choices[0].message.content.strip()}
    except Exception as e:
        logger.error("Chat error: %s", e)
        raise HTTPException(status_code=500, detail=f"שגיאה: {str(e)[:100]}")


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


@app.get("/api/services")
def list_services(uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    svcs = db.query(Service).order_by(Service.name).all()
    return [_service_dict(s) for s in svcs]


@app.post("/api/services", status_code=201)
def create_service(body: dict = Body(default={}), uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    s = Service(
        name=name,
        description=body.get("description", ""),
        unit=body.get("unit", "per_session"),
        price_highschool=float(body.get("price_highschool", 0)),
        price_college=float(body.get("price_college", 0)),
        price_career=float(body.get("price_career", 0)),
    )
    db.add(s)
    db.commit()
    return _service_dict(s)


@app.patch("/api/services/{sid}")
def update_service(sid: int, body: dict = Body(default={}), uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    s = _or_404(db.get(Service, sid))
    for field in ("name", "description", "unit"):
        if field in body:
            setattr(s, field, body[field])
    for field in ("price_highschool", "price_college", "price_career"):
        if field in body:
            setattr(s, field, float(body[field]))
    if "is_active" in body:
        s.is_active = bool(body["is_active"])
    db.commit()
    return _service_dict(s)


@app.delete("/api/services/{sid}")
def delete_service(sid: int, uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    s = _or_404(db.get(Service, sid))
    db.delete(s)
    db.commit()
    return {"ok": True}


@app.get("/api/students/{student_id}/billing")
def get_student_billing(student_id: int, uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    _or_404(db.query(User).filter_by(id=student_id, role="student").first())
    sb = db.query(StudentBilling).filter_by(student_id=student_id).first()
    if not sb:
        return {"assigned": False}
    s       = sb.service
    student = db.get(User, student_id)
    price   = _price_for_student(s, student.profile, sb.custom_price) if s else 0
    return {
        "assigned": True,
        "service_id":    sb.service_id,
        "service_name":  s.name if s else None,
        "service_unit":  s.unit if s else None,
        "custom_price":  sb.custom_price,
        "effective_price": price,
        "is_active":     sb.is_active,
    }


@app.post("/api/students/{student_id}/billing")
def set_student_billing(student_id: int, body: dict = Body(default={}), uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    _or_404(db.query(User).filter_by(id=student_id, role="student").first())
    sb = db.query(StudentBilling).filter_by(student_id=student_id).first()
    if not sb:
        sb = StudentBilling(student_id=student_id)
        db.add(sb)
    sb.service_id   = body.get("service_id")
    sb.custom_price = float(body["custom_price"]) if body.get("custom_price") else None
    sb.is_active    = body.get("is_active", True)
    db.commit()

    s     = db.get(Service, sb.service_id) if sb.service_id else None
    price = _price_for_student(s, db.get(User, student_id).profile, sb.custom_price) if s else 0
    return {"ok": True, "effective_price": price}


@app.get("/api/billing")
def billing_dashboard(month: Optional[str] = Query(default=None), uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    if month is None:
        month = datetime.utcnow().strftime("%Y-%m")

    records = (db.query(BillingRecord).filter_by(month=month)
               .order_by(BillingRecord.student_id).all())

    MONTHS_HE = {1:"ינואר",2:"פברואר",3:"מרץ",4:"אפריל",5:"מאי",6:"יוני",
                 7:"יולי",8:"אוגוסט",9:"ספטמבר",10:"אוקטובר",11:"נובמבר",12:"דצמבר"}
    y, m = int(month[:4]), int(month[5:])
    month_name = f"{MONTHS_HE[m]} {y}"
    prev_m = f"{y}-{m-1:02d}" if m > 1 else f"{y-1}-12"
    next_m = f"{y}-{m+1:02d}" if m < 12 else f"{y+1}-01"

    result = []
    for rec in records:
        s   = db.get(User, rec.student_id)
        p   = s.profile if s else None
        svc = db.get(Service, rec.service_id) if rec.service_id else None
        result.append({
            "id":             rec.id,
            "student_id":     rec.student_id,
            "student_name":   (p.full_name if p and p.full_name else (s.username if s else "?")),
            "service_name":   svc.name if svc else "—",
            "service_unit":   svc.unit if svc else "per_session",
            "month":          rec.month,
            "meetings_count": rec.meetings_count,
            "amount_due":     rec.amount_due,
            "paid_at":        rec.paid_at.isoformat() if rec.paid_at else None,
            "payment_note":   rec.payment_note,
        })

    total_due  = sum(r["amount_due"] for r in result)
    total_paid = sum(r["amount_due"] for r in result if r["paid_at"])

    return {
        "month":         month,
        "month_name":    month_name,
        "prev_month":    prev_m,
        "next_month":    next_m,
        "records":       result,
        "total_due":     total_due,
        "total_paid":    total_paid,
        "total_pending": total_due - total_paid,
    }


@app.post("/api/billing/generate/{month}")
def generate_billing(month: str, uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    try:
        y, m = int(month[:4]), int(month[5:])
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid month format (YYYY-MM)")

    from_dt = datetime(y, m, 1)
    to_dt   = datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1)

    students = db.query(User).filter_by(role="student").all()
    created  = 0

    for student in students:
        sb = db.query(StudentBilling).filter_by(student_id=student.id, is_active=True).first()
        if not sb or not sb.service_id:
            continue
        svc = db.get(Service, sb.service_id)
        if not svc:
            continue

        meeting_count = (db.query(Meeting)
                         .filter_by(student_id=student.id)
                         .filter(Meeting.scheduled_at >= from_dt,
                                 Meeting.scheduled_at <  to_dt)
                         .filter(Meeting.status.in_(["confirmed", "completed"]))
                         .count())

        price = _price_for_student(svc, student.profile, sb.custom_price)

        if svc.unit == "per_session":
            amount = meeting_count * price
            if amount == 0:
                continue
        else:
            amount = price

        rec = db.query(BillingRecord).filter_by(student_id=student.id, month=month).first()
        if not rec:
            rec = BillingRecord(student_id=student.id, month=month)
            db.add(rec)
        elif rec.paid_at:
            created += 1
            continue
        rec.service_id     = sb.service_id
        rec.meetings_count = meeting_count
        rec.amount_due     = amount
        created += 1

    db.commit()
    return {"ok": True, "processed": created, "month": month}


@app.patch("/api/billing/{rec_id}/pay")
def mark_billing_paid(rec_id: int, body: dict = Body(default={}), uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    rec = _or_404(db.get(BillingRecord, rec_id))
    if body.get("paid"):
        rec.paid_at      = datetime.utcnow()
        rec.payment_note = body.get("note", "")
    else:
        rec.paid_at      = None
        rec.payment_note = ""
    db.commit()
    return {"ok": True, "paid_at": rec.paid_at.isoformat() if rec.paid_at else None}


@app.get("/api/students/{student_id}/billing/history")
def student_billing_history(
    student_id: int,
    year: int = Query(default=None),
    uid: int = Depends(require_admin_user),
    db: Session = Depends(get_db),
):
    _or_404(db.query(User).filter_by(id=student_id, role="student").first())
    if year is None:
        year = datetime.utcnow().year

    records = (db.query(BillingRecord)
               .filter_by(student_id=student_id)
               .filter(BillingRecord.month.like(f"{year}-%"))
               .order_by(BillingRecord.month.desc()).all())

    result = []
    for rec in records:
        svc = db.get(Service, rec.service_id) if rec.service_id else None
        result.append({
            "id":             rec.id,
            "month":          rec.month,
            "service_name":   svc.name if svc else "—",
            "service_unit":   svc.unit if svc else "per_session",
            "meetings_count": rec.meetings_count,
            "amount_due":     rec.amount_due,
            "paid_at":        rec.paid_at.isoformat() if rec.paid_at else None,
            "payment_note":   rec.payment_note,
        })

    return {
        "year":          year,
        "records":       result,
        "total_year":    sum(r["amount_due"] for r in result),
        "meetings_year": sum(r["meetings_count"] for r in result),
    }


# ─────────────────────────────────────────────
# Reports
# ─────────────────────────────────────────────

@app.get("/api/students/{sid}/report")
def student_report_data(sid: int, uid: int = Depends(require_admin_user), db: Session = Depends(get_db)):
    student   = _or_404(db.query(User).filter_by(id=sid, role="student").first())
    notes     = (db.query(MentorNote).filter_by(student_id=sid)
                 .order_by(MentorNote.created_at.desc()).all())
    active    = db.query(AssignedTask).filter_by(user_id=sid, status="pending").all()
    completed = (db.query(AssignedTask).filter_by(user_id=sid, status="completed")
                 .order_by(AssignedTask.completed_at.desc()).all())
    return {
        "student":   {"id": student.id, "username": student.username},
        "profile":   _profile_dict(student.profile),
        "active":    [_assignment_dict(a) for a in active],
        "completed": [_assignment_dict(a) for a in completed],
        "notes":     [{"text": n.text, "created_at": n.created_at.isoformat()} for n in notes],
    }


@app.get("/api/reports/meetings")
def meetings_report(
    year:  int = Query(default=None),
    month: int = Query(default=None),
    uid: int = Depends(require_admin_user),
    db: Session = Depends(get_db),
):
    if year  is None: year  = date.today().year
    if month is None: month = date.today().month
    from_dt = datetime(year, month, 1)
    to_dt   = datetime(year + 1 if month == 12 else year,
                       1 if month == 12 else month + 1, 1)

    meetings = (db.query(Meeting)
                .filter(Meeting.scheduled_at >= from_dt,
                        Meeting.scheduled_at <  to_dt)
                .filter(Meeting.status != "cancelled")
                .order_by(Meeting.scheduled_at).all())

    per_student: dict = {}
    for m in meetings:
        s = db.get(User, m.student_id)
        p = s.profile if s else None
        name = (p.full_name if p and p.full_name else (s.username if s else "?"))
        if m.student_id not in per_student:
            per_student[m.student_id] = {"name": name, "count": 0, "total_min": 0, "meetings": []}
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
    return {
        "year": year, "month": month, "month_name": MONTHS_HE[month],
        "total_meetings": len(meetings),
        "total_students": len(per_student),
        "total_hours":    round(sum(m.duration_min or 0 for m in meetings) / 60, 1),
        "per_student":    list(per_student.values()),
    }
