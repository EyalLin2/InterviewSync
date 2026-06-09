import os
from datetime import datetime, date  # noqa: F401
from sqlalchemy import (
    Column, Integer, String, Text, Boolean, Date, DateTime, Float,
    ForeignKey, UniqueConstraint, create_engine
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker


def get_database_url() -> str:
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


class Base(DeclarativeBase):
    pass


engine = create_engine(get_database_url(), pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class User(Base):
    __tablename__ = "users"
    id       = Column(Integer, primary_key=True)
    username = Column(String(80), unique=True, nullable=False)
    password = Column(String(256), nullable=False)
    role     = Column(String(10), nullable=False, default="student")
    profile  = relationship("StudentProfile", backref="user", uselist=False, lazy="select")
    tasks    = relationship("AssignedTask", backref="student", lazy="select")
    meetings = relationship("Meeting", foreign_keys="Meeting.student_id",
                            backref="student", lazy="select")


class StudentProfile(Base):
    __tablename__ = "student_profiles"
    id                          = Column(Integer, primary_key=True)
    user_id                     = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    full_name                   = Column(String(120), default="")
    email                       = Column(String(120), default="")
    phone                       = Column(String(30), default="")
    education_level             = Column(String(20), default="")
    current_occupation_or_grade = Column(String(200), default="")
    career_goals                = Column(Text, default="")
    fears_weaknesses            = Column(Text, default="")
    ai_coaching_strategy        = Column(Text, default="")
    resume_content              = Column(Text, default="")
    process_start_date          = Column(Date, nullable=True)
    target_end_date             = Column(Date, nullable=True)
    mentor_notes                = Column(Text, default="")
    resume_file                 = Column(String(512), default="")
    student_status              = Column(String(20), default="active")
    ai_strategy_updated_at      = Column(DateTime, nullable=True)
    interests_hobbies           = Column(Text, default="")
    institution_name            = Column(String(200), default="")
    graduation_year             = Column(Integer, nullable=True)
    current_job                 = Column(String(200), default="")
    years_experience            = Column(Integer, nullable=True)
    reason_for_guidance         = Column(Text, default="")
    last_reminder_sent          = Column(Date, nullable=True)
    created_at                  = Column(DateTime, default=datetime.utcnow)


class TaskBank(Base):
    __tablename__ = "task_bank"
    id            = Column(Integer, primary_key=True)
    title         = Column(String(512), nullable=False)
    description   = Column(Text, default="")
    category      = Column(String(100), default="כללי")
    task_type     = Column(String(30), default="task")
    resource_file = Column(String(512), default="")
    created_at    = Column(DateTime, default=datetime.utcnow)
    assignments   = relationship("AssignedTask", backref="task", lazy="select")


class AssignedTask(Base):
    __tablename__ = "assigned_tasks"
    id              = Column(Integer, primary_key=True)
    user_id         = Column(Integer, ForeignKey("users.id"), nullable=False)
    task_id         = Column(Integer, ForeignKey("task_bank.id"), nullable=False)
    status          = Column(String(20), default="pending")
    assigned_at     = Column(DateTime, default=datetime.utcnow)
    completed_at    = Column(DateTime, nullable=True)
    submission_note = Column(Text, default="")
    submission_file = Column(String(512), default="")
    feedback        = Column(Text, default="")
    feedback_at     = Column(DateTime, nullable=True)
    feedback_seen   = Column(Boolean, default=False)
    due_date        = Column(Date, nullable=True)
    __table_args__  = (UniqueConstraint("user_id", "task_id", name="uq_assigned_task"),)


class Meeting(Base):
    __tablename__ = "meetings"
    id           = Column(Integer, primary_key=True)
    student_id   = Column(Integer, ForeignKey("users.id"), nullable=False)
    scheduled_at = Column(DateTime, nullable=False)
    duration_min = Column(Integer, default=60)
    notes        = Column(Text, default="")
    status       = Column(String(20), default="pending")
    meeting_type = Column(String(30), default="progress_review")
    created_at   = Column(DateTime, default=datetime.utcnow)


class MentorNote(Base):
    __tablename__ = "mentor_notes_log"
    id         = Column(Integer, primary_key=True)
    student_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    text       = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


# ── Business-side models ───────────────────────────────────────────────────

TOPIC_CATEGORIES = [
    "LinkedIn",
    "קריירה וחיפוש עבודה",
    "פיתוח אישי",
    "הכנה לראיונות",
    "בניית קורות חיים",
    "כללי",
]


class Workshop(Base):
    __tablename__ = "workshops"
    id               = Column(Integer, primary_key=True)
    title            = Column(String(255), nullable=False)
    description      = Column(Text, default="")
    topic_category   = Column(String(100), default="כללי")
    workshop_type    = Column(String(20), default="one_time")
    status           = Column(String(20), default="planned")
    scheduled_at     = Column(DateTime, nullable=True)
    location         = Column(String(255), default="")
    max_participants = Column(Integer, nullable=True)
    notes            = Column(Text, default="")
    created_at       = Column(DateTime, default=datetime.utcnow)
    inquiries        = relationship("Inquiry", backref="workshop", lazy="select")
    activities       = relationship("ActivityLog", backref="workshop", lazy="select")


class Inquiry(Base):
    __tablename__ = "inquiries"
    id          = Column(Integer, primary_key=True)
    full_name   = Column(String(120), nullable=False)
    phone       = Column(String(30), default="")
    email       = Column(String(120), default="")
    topic       = Column(Text, default="")
    source      = Column(String(30), default="")
    notes       = Column(Text, default="")
    status      = Column(String(20), default="new")
    workshop_id = Column(Integer, ForeignKey("workshops.id"), nullable=True)
    created_at  = Column(DateTime, default=datetime.utcnow)


class ActivityLog(Base):
    __tablename__ = "activity_logs"
    id                 = Column(Integer, primary_key=True)
    title              = Column(String(255), nullable=False)
    activity_type      = Column(String(30), default="other")
    topic_category     = Column(String(100), default="")
    activity_date      = Column(Date, nullable=False)
    duration_min       = Column(Integer, nullable=True)
    participants_count = Column(Integer, nullable=True)
    description        = Column(Text, default="")
    workshop_id        = Column(Integer, ForeignKey("workshops.id"), nullable=True)
    created_at         = Column(DateTime, default=datetime.utcnow)


# ── Billing models ─────────────────────────────────────────────────────────

class Service(Base):
    __tablename__ = "services"
    id                = Column(Integer, primary_key=True)
    name              = Column(String(120), nullable=False)
    description       = Column(Text, default="")
    unit              = Column(String(20), default="per_session")
    price_highschool  = Column(Float, default=0)
    price_college     = Column(Float, default=0)
    price_career      = Column(Float, default=0)
    is_active         = Column(Boolean, default=True)
    created_at        = Column(DateTime, default=datetime.utcnow)
    student_billings  = relationship("StudentBilling", backref="service", lazy="select")


class StudentBilling(Base):
    __tablename__ = "student_billing"
    id                  = Column(Integer, primary_key=True)
    student_id          = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False)
    service_id          = Column(Integer, ForeignKey("services.id"), nullable=True)
    custom_price        = Column(Float, nullable=True)
    sessions_per_month  = Column(Integer, nullable=True)
    started_at          = Column(Date, nullable=True)
    is_active           = Column(Boolean, default=True)


class BillingRecord(Base):
    __tablename__ = "billing_records"
    id              = Column(Integer, primary_key=True)
    student_id      = Column(Integer, ForeignKey("users.id"), nullable=False)
    service_id      = Column(Integer, ForeignKey("services.id"), nullable=True)
    month           = Column(String(7), nullable=False)
    meetings_count  = Column(Integer, default=0)
    amount_due      = Column(Float, default=0)
    paid_at         = Column(DateTime, nullable=True)
    payment_note    = Column(Text, default="")
    created_at      = Column(DateTime, default=datetime.utcnow)
    __table_args__  = (UniqueConstraint("student_id", "month", name="uq_billing_student_month"),)
