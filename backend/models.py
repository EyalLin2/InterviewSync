import os
from datetime import datetime, date  # noqa: F401  # date used by SQLAlchemy Date column
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def get_database_url():
    """Build PostgreSQL URL from environment variables."""
    if url := os.environ.get("DATABASE_URL"):
        return url
    return (
        "postgresql://{user}:{pw}@{host}:{port}/{name}".format(
            user=os.environ.get("DB_USER", "interviewsync"),
            pw=os.environ.get("DB_PASSWORD", "devpassword"),
            host=os.environ.get("DB_HOST", "localhost"),
            port=os.environ.get("DB_PORT", "5432"),
            name=os.environ.get("DB_NAME", "interviewsync"),
        )
    )


class User(db.Model):
    __tablename__ = "users"
    id       = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(256), nullable=False)
    role     = db.Column(db.String(10), nullable=False, default="student")
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
    education_level             = db.Column(db.String(20), default="")
    current_occupation_or_grade = db.Column(db.String(200), default="")
    career_goals                = db.Column(db.Text, default="")
    fears_weaknesses            = db.Column(db.Text, default="")
    ai_coaching_strategy        = db.Column(db.Text, default="")
    resume_content              = db.Column(db.Text, default="")
    process_start_date          = db.Column(db.Date, nullable=True)
    target_end_date             = db.Column(db.Date, nullable=True)
    mentor_notes                = db.Column(db.Text, default="")
    student_status              = db.Column(db.String(20), default="active")
    # active | paused | completed
    created_at                  = db.Column(db.DateTime, default=datetime.utcnow)


class TaskBank(db.Model):
    __tablename__ = "task_bank"
    id            = db.Column(db.Integer, primary_key=True)
    title         = db.Column(db.String(512), nullable=False)
    description   = db.Column(db.Text, default="")
    category      = db.Column(db.String(100), default="כללי")
    task_type     = db.Column(db.String(30), default="task")
    resource_file = db.Column(db.String(512), default="")
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    assignments   = db.relationship("AssignedTask", backref="task", lazy=True)


class AssignedTask(db.Model):
    __tablename__ = "assigned_tasks"
    id              = db.Column(db.Integer, primary_key=True)
    user_id         = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    task_id         = db.Column(db.Integer, db.ForeignKey("task_bank.id"), nullable=False)
    status          = db.Column(db.String(20), default="pending")
    assigned_at     = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at    = db.Column(db.DateTime, nullable=True)
    submission_note = db.Column(db.Text, default="")
    submission_file = db.Column(db.String(512), default="")
    __table_args__  = (db.UniqueConstraint("user_id", "task_id", name="uq_assigned_task"),)


class Meeting(db.Model):
    __tablename__ = "meetings"
    id           = db.Column(db.Integer, primary_key=True)
    student_id   = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    scheduled_at = db.Column(db.DateTime, nullable=False)
    duration_min = db.Column(db.Integer, default=60)
    notes        = db.Column(db.Text, default="")
    status       = db.Column(db.String(20), default="pending")
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)


# ── Business-side models ───────────────────────────────────────────────────

TOPIC_CATEGORIES = [
    "LinkedIn",
    "קריירה וחיפוש עבודה",
    "פיתוח אישי",
    "הכנה לראיונות",
    "בניית קורות חיים",
    "כללי",
]


class Workshop(db.Model):
    __tablename__ = "workshops"
    id               = db.Column(db.Integer, primary_key=True)
    title            = db.Column(db.String(255), nullable=False)
    description      = db.Column(db.Text, default="")
    topic_category   = db.Column(db.String(100), default="כללי")
    workshop_type    = db.Column(db.String(20), default="one_time")
    # one_time | recurring | custom
    status           = db.Column(db.String(20), default="planned")
    # planned | active | completed | cancelled
    scheduled_at     = db.Column(db.DateTime, nullable=True)
    location         = db.Column(db.String(255), default="")
    max_participants = db.Column(db.Integer, nullable=True)
    notes            = db.Column(db.Text, default="")
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)
    inquiries        = db.relationship("Inquiry", backref="workshop", lazy=True)
    activities       = db.relationship("ActivityLog", backref="workshop", lazy=True)


class Inquiry(db.Model):
    __tablename__ = "inquiries"
    id          = db.Column(db.Integer, primary_key=True)
    full_name   = db.Column(db.String(120), nullable=False)
    phone       = db.Column(db.String(30), default="")
    email       = db.Column(db.String(120), default="")
    topic       = db.Column(db.Text, default="")
    source      = db.Column(db.String(30), default="")
    # whatsapp | email | referral | other
    notes       = db.Column(db.Text, default="")
    status      = db.Column(db.String(20), default="new")
    # new | in_contact | assigned | closed
    workshop_id = db.Column(db.Integer, db.ForeignKey("workshops.id"), nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)


class ActivityLog(db.Model):
    __tablename__ = "activity_logs"
    id                 = db.Column(db.Integer, primary_key=True)
    title              = db.Column(db.String(255), nullable=False)
    activity_type      = db.Column(db.String(30), default="other")
    # workshop | lecture | meeting | other
    topic_category     = db.Column(db.String(100), default="")
    activity_date      = db.Column(db.Date, nullable=False)
    duration_min       = db.Column(db.Integer, nullable=True)
    participants_count = db.Column(db.Integer, nullable=True)
    description        = db.Column(db.Text, default="")
    workshop_id        = db.Column(db.Integer, db.ForeignKey("workshops.id"), nullable=True)
    created_at         = db.Column(db.DateTime, default=datetime.utcnow)
