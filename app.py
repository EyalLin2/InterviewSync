import os
import json
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from openai import OpenAI

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///interviewsync.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class User(db.Model):
    __tablename__ = "users"
    id                     = db.Column(db.Integer, primary_key=True)
    username               = db.Column(db.String(80), unique=True, nullable=False)
    password               = db.Column(db.String(256), nullable=False)
    role                   = db.Column(db.String(10), nullable=False, default="student")
    # Basic profile
    full_name              = db.Column(db.String(120), default="")
    email                  = db.Column(db.String(120), default="")
    age                    = db.Column(db.Integer, nullable=True)
    # Onboarding fields
    degree_field           = db.Column(db.Text, default="")
    interests              = db.Column(db.Text, default="")
    career_goals           = db.Column(db.Text, default="")
    previous_experience    = db.Column(db.Text, default="")  # interview/work experience
    main_challenges        = db.Column(db.Text, default="")  # self-reported challenges
    # AI-generated
    ai_onboarding_analysis = db.Column(db.Text, default="")
    answers                = db.relationship("Answer", backref="user", lazy=True)


class Question(db.Model):
    __tablename__ = "questions"
    id          = db.Column(db.Integer, primary_key=True)
    title       = db.Column(db.String(512), nullable=False)
    category    = db.Column(db.String(100), nullable=False)
    target_role = db.Column(db.String(100), nullable=False)
    hint        = db.Column(db.Text, default="")   # AI-generated guidance for student
    answers     = db.relationship("Answer", backref="question", lazy=True)


class Answer(db.Model):
    __tablename__ = "answers"
    id              = db.Column(db.Integer, primary_key=True)
    user_id         = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    question_id     = db.Column(db.Integer, db.ForeignKey("questions.id"), nullable=False)
    situation       = db.Column(db.Text, nullable=False)
    task            = db.Column(db.Text, nullable=False)
    action          = db.Column(db.Text, nullable=False)
    result          = db.Column(db.Text, nullable=False)
    mentor_feedback = db.Column(db.Text, default="")
    ai_feedback     = db.Column(db.Text, default="")


class QuestionAssignment(db.Model):
    __tablename__ = "question_assignments"
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    question_id = db.Column(db.Integer, db.ForeignKey("questions.id"), nullable=False)
    __table_args__ = (
        db.UniqueConstraint("user_id", "question_id", name="uq_user_question"),
    )


class GeneralTask(db.Model):
    __tablename__ = "general_tasks"
    id          = db.Column(db.Integer, primary_key=True)
    title       = db.Column(db.String(512), nullable=False)
    description = db.Column(db.Text, default="")
    task_type   = db.Column(db.String(30), default="custom")
    # task_type: "reading" | "video" | "exercise" | "custom" | "ai_generated"
    ai_hint     = db.Column(db.Text, default="")   # AI-generated interactive guidance
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)


class GeneralTaskAssignment(db.Model):
    __tablename__ = "general_task_assignments"
    id              = db.Column(db.Integer, primary_key=True)
    user_id         = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    task_id         = db.Column(db.Integer, db.ForeignKey("general_tasks.id"), nullable=False)
    completed       = db.Column(db.Boolean, default=False)
    completion_note = db.Column(db.Text, default="")
    assigned_at     = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__  = (
        db.UniqueConstraint("user_id", "task_id", name="uq_user_gtask"),
    )


# ---------------------------------------------------------------------------
# AI helpers
# ---------------------------------------------------------------------------

def get_ai_client():
    api_key = os.environ.get("AI_API_KEY")
    if not api_key:
        return None
    return OpenAI(api_key=api_key)


def ai_analyze_star(situation, task, action, result_text, question_title):
    client = get_ai_client()
    if not client:
        return "משוב AI אינו זמין (AI_API_KEY לא הוגדר)."

    prompt = f"""You are an expert interview coach. Analyze the following STAR-format answer to the interview question: "{question_title}"

Situation: {situation}
Task: {task}
Action: {action}
Result: {result_text}

Provide concise, constructive feedback (3-5 bullet points) covering:
- Clarity and specificity of each STAR component
- Impact and measurability of the result
- Areas for improvement
- Overall strength rating (Weak / Developing / Strong / Excellent)

Be encouraging but honest."""

    try:
        response = get_ai_client().chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
            temperature=0.7,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"ניתוח AI נכשל: {str(e)}"


def ai_generate_questions(target_role, count=3):
    client = get_ai_client()
    if not client:
        return []

    prompt = f"""Generate exactly {count} behavioral interview questions for a {target_role} role.

Return ONLY a JSON array with this exact structure (no markdown, no extra text):
[
  {{"title": "question text", "category": "category name", "hint": "2-3 sentence tip for the student on how to approach this question with a strong STAR answer"}},
  ...
]

Categories should be one of: Leadership, Problem-Solving, Teamwork, Communication, Adaptability, Technical."""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=800,
            temperature=0.8,
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        questions = json.loads(raw)
        return questions[:count]
    except Exception as e:
        app.logger.error("ai_generate_questions error: %s", e)
        return []


def ai_analyze_onboarding(student):
    """Return a Hebrew mentor coaching strategy based on the full student profile."""
    client = get_ai_client()
    if not client:
        return "ניתוח AI אינו זמין (AI_API_KEY לא הוגדר)."

    profile_parts = [
        f"שם: {student.full_name or student.username}",
        f"גיל: {student.age or 'לא צוין'}",
        f"תחום לימודים: {student.degree_field or 'לא צוין'}",
        f"תחומי עניין: {student.interests or 'לא צוינו'}",
        f"מטרות קריירה: {student.career_goals or 'לא צוינו'}",
        f"ניסיון קודם בראיונות/עבודה: {student.previous_experience or 'לא צוין'}",
        f"אתגרים עיקריים שמזהה בעצמו/ה: {student.main_challenges or 'לא צוינו'}",
    ]

    prompt = f"""אתה מאמן קריירה מומחה. להלן הפרופיל המלא של הסטודנט:

{chr(10).join(profile_parts)}

כתוב ניתוח מקצועי ומפורט (4-6 נקודות) עבור המנטור/ית, הכולל:
• נושאי מיקוד עיקריים לראיונות בהתאם לתחום ולמטרות
• חוזקות פוטנציאליות שניתן לנצל
• נקודות תורפה ואתגרים צפויים שיש לטפל בהם
• סוגי שאלות STAR שמומלץ לתרגל
• גישת אימון מומלצת אישית לסטודנט זה

כתוב בעברית, בגוף ראשון מרובים כאילו אתה מדבר אל המנטור/ית ישירות."""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=700,
            temperature=0.7,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"ניתוח AI נכשל: {str(e)}"


def ai_suggest_student_tasks(student):
    """Return a list of personalized task dicts for this student."""
    client = get_ai_client()
    if not client:
        return []

    profile = (
        f"שם: {student.full_name or student.username}, גיל: {student.age or '?'}, "
        f"תחום: {student.degree_field}, עניין: {student.interests}, "
        f"מטרות: {student.career_goals}, אתגרים: {student.main_challenges}"
    )

    prompt = f"""אתה מאמן קריירה. בהתבסס על הפרופיל הבא של סטודנט:
{profile}

צור בדיוק 5 משימות הכנה מותאמות אישית. מיקס של סוגים שונים.

החזר אך ורק מערך JSON (ללא מרקדאון, ללא טקסט נוסף):
[
  {{
    "title": "כותרת המשימה בעברית",
    "description": "תיאור קצר מה לעשות (1-2 משפטים)",
    "task_type": "reading|exercise|custom",
    "ai_hint": "הנחיה אינטראקטיבית לסטודנט — שאלות להרהר בהן, נקודות לשים לב, או דוגמה קצרה שתעזור לו להבין טוב יותר (2-4 משפטים)"
  }},
  ...
]"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000,
            temperature=0.8,
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)[:5]
    except Exception as e:
        app.logger.error("ai_suggest_student_tasks error: %s", e)
        return []


def ai_generate_task_hint(title, description, task_type):
    """Generate interactive guidance text for a manually-created general task."""
    client = get_ai_client()
    if not client:
        return ""

    prompt = f"""משימה: "{title}"
תיאור: "{description}"
סוג: {task_type}

כתוב הנחיה קצרה ואינטראקטיבית לסטודנט (2-4 משפטים בעברית) שתכלול:
- מה המטרה של המשימה הזו
- שאלה אחת לחשוב עליה לפני/אחרי המשימה
- טיפ קצר איך להפיק את המרב ממנה"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.7,
        )
        return response.choices[0].message.content.strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def current_user():
    uid = session.get("user_id")
    return db.session.get(User, uid) if uid else None


def login_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user or user.role != "admin":
            flash("נדרשת הרשאת מנטור.", "danger")
            return redirect(url_for("index"))
        return fn(*args, **kwargs)
    return wrapper


def _onboarding_complete(user):
    return bool(user.degree_field and user.career_goals and user.full_name and user.email)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    u = current_user()
    if u:
        return redirect(url_for("admin") if u.role == "admin" else url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            session["user_id"] = user.id
            flash(f"ברוך הבא, {user.full_name or user.username}!", "success")
            if user.role == "admin":
                return redirect(url_for("admin"))
            if not _onboarding_complete(user):
                return redirect(url_for("onboarding"))
            return redirect(url_for("index"))
        flash("שם משתמש או סיסמה שגויים.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/onboarding", methods=["GET", "POST"])
@login_required
def onboarding():
    user = current_user()
    if user.role == "admin":
        return redirect(url_for("admin"))
    if _onboarding_complete(user):
        return redirect(url_for("index"))

    if request.method == "POST":
        user.full_name           = request.form.get("full_name", "").strip()
        user.email               = request.form.get("email", "").strip()
        age_raw                  = request.form.get("age", "").strip()
        user.age                 = int(age_raw) if age_raw.isdigit() else None
        user.degree_field        = request.form.get("degree_field", "").strip()
        user.interests           = request.form.get("interests", "").strip()
        user.career_goals        = request.form.get("career_goals", "").strip()
        user.previous_experience = request.form.get("previous_experience", "").strip()
        user.main_challenges     = request.form.get("main_challenges", "").strip()

        if not (user.full_name and user.email and user.degree_field and user.career_goals):
            flash("יש למלא את השדות המסומנים כחובה.", "warning")
            return redirect(url_for("onboarding"))

        user.ai_onboarding_analysis = ai_analyze_onboarding(user)
        db.session.commit()
        flash("הפרופיל שלך נשמר בהצלחה!", "success")
        return redirect(url_for("index"))

    return render_template("onboarding.html", user=user)


@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    user = current_user()
    if user.role == "admin":
        return redirect(url_for("admin"))
    if not _onboarding_complete(user):
        return redirect(url_for("onboarding"))

    # ── STAR questions ──
    assigned_ids = [
        qa.question_id
        for qa in QuestionAssignment.query.filter_by(user_id=user.id).all()
    ]
    questions = (
        Question.query.filter(Question.id.in_(assigned_ids))
        .order_by(Question.category).all()
        if assigned_ids else []
    )
    my_answers = {a.question_id: a for a in Answer.query.filter_by(user_id=user.id).all()}
    assigned_count = len(questions)
    answered_count = sum(1 for q in questions if q.id in my_answers)

    # ── General tasks ──
    my_gta = {
        a.task_id: a
        for a in GeneralTaskAssignment.query.filter_by(user_id=user.id).all()
    }
    general_tasks = (
        GeneralTask.query.filter(GeneralTask.id.in_(list(my_gta.keys()))).all()
        if my_gta else []
    )
    general_task_count = len(general_tasks)
    general_tasks_done = sum(1 for a in my_gta.values() if a.completed)

    if request.method == "POST":
        # ── Complete a general task ──
        if request.form.get("action") == "complete_general_task":
            gtask_id = request.form.get("gtask_id", type=int)
            note     = request.form.get("completion_note", "").strip()
            gta      = GeneralTaskAssignment.query.filter_by(
                           user_id=user.id, task_id=gtask_id).first_or_404()
            gta.completed       = True
            gta.completion_note = note
            db.session.commit()
            flash("המשימה סומנה כהושלמה! כל הכבוד ✓", "success")
            return redirect(url_for("index"))

        # ── Submit STAR answer ──
        question_id = request.form.get("question_id", type=int)
        situation   = request.form.get("situation", "").strip()
        task        = request.form.get("task", "").strip()
        action      = request.form.get("action", "").strip()
        result      = request.form.get("result", "").strip()

        if not all([question_id, situation, task, action, result]):
            flash("יש למלא את כל ארבעת שדות ה-STAR.", "warning")
            return redirect(url_for("index"))
        if question_id not in assigned_ids:
            flash("שאלה לא חוקית.", "danger")
            return redirect(url_for("index"))

        question    = Question.query.get_or_404(question_id)
        ai_feedback = ai_analyze_star(situation, task, action, result, question.title)

        existing = my_answers.get(question_id)
        if existing:
            existing.situation   = situation
            existing.task        = task
            existing.action      = action
            existing.result      = result
            existing.ai_feedback = ai_feedback
        else:
            db.session.add(Answer(
                user_id=user.id, question_id=question_id,
                situation=situation, task=task, action=action,
                result=result, ai_feedback=ai_feedback,
            ))
        db.session.commit()
        flash("התשובה נשמרה! משוב AI מוכן למטה.", "success")
        return redirect(url_for("index"))

    return render_template(
        "index.html",
        user=user,
        questions=questions,
        my_answers=my_answers,
        assigned_count=assigned_count,
        answered_count=answered_count,
        general_tasks=general_tasks,
        my_gta=my_gta,
        general_task_count=general_task_count,
        general_tasks_done=general_tasks_done,
    )


@app.route("/admin", methods=["GET", "POST"])
@login_required
@admin_required
def admin():
    user      = current_user()
    students  = User.query.filter_by(role="student").order_by(User.username).all()
    questions = Question.query.order_by(Question.target_role, Question.category).all()

    if request.method == "POST":
        action = request.form.get("action")

        if action == "add_feedback":
            answer_id = request.form.get("answer_id", type=int)
            feedback  = request.form.get("mentor_feedback", "").strip()
            answer    = Answer.query.get_or_404(answer_id)
            answer.mentor_feedback = feedback
            db.session.commit()
            flash("משוב המנטור נשמר.", "success")

        elif action == "generate_questions":
            target_role = request.form.get("target_role", "").strip()
            if not target_role:
                flash("יש להזין תפקיד יעד.", "warning")
            else:
                generated = ai_generate_questions(target_role, count=3)
                if generated:
                    for q in generated:
                        db.session.add(Question(
                            title=q.get("title", "שאלה ללא כותרת"),
                            category=q.get("category", "כללי"),
                            target_role=target_role,
                            hint=q.get("hint", ""),
                        ))
                    db.session.commit()
                    flash(f"נוצרו ונשמרו {len(generated)} שאלות AI עבור '{target_role}'.", "success")
                else:
                    flash("יצירת שאלות AI נכשלה או שמפתח AI_API_KEY לא הוגדר.", "warning")

        elif action == "add_question":
            title       = request.form.get("title", "").strip()
            category    = request.form.get("category", "").strip()
            target_role = request.form.get("target_role_manual", "").strip()
            if title and category and target_role:
                hint = ai_generate_task_hint(title, "", "interview_question")
                db.session.add(Question(title=title, category=category,
                                        target_role=target_role, hint=hint))
                db.session.commit()
                flash("השאלה נוספה.", "success")
            else:
                flash("יש למלא את כל שדות השאלה.", "warning")

        elif action == "update_assignments":
            student_id = request.form.get("student_id", type=int)
            if student_id:
                User.query.get_or_404(student_id)
                checked_ids = request.form.getlist("assigned_questions", type=int)
                QuestionAssignment.query.filter_by(user_id=student_id).delete(
                    synchronize_session="fetch")
                for qid in checked_ids:
                    db.session.add(QuestionAssignment(user_id=student_id, question_id=qid))
                db.session.commit()
                flash("שאלות ה-STAR עודכנו בהצלחה.", "success")

        elif action == "add_general_task":
            title       = request.form.get("gt_title", "").strip()
            description = request.form.get("gt_description", "").strip()
            task_type   = request.form.get("gt_type", "custom")
            if title:
                ai_hint = ai_generate_task_hint(title, description, task_type)
                db.session.add(GeneralTask(
                    title=title, description=description,
                    task_type=task_type, ai_hint=ai_hint,
                ))
                db.session.commit()
                flash("המשימה הכללית נוספה.", "success")
            else:
                flash("יש להזין כותרת למשימה.", "warning")

        elif action == "assign_general_tasks":
            student_id = request.form.get("student_id", type=int)
            if student_id:
                User.query.get_or_404(student_id)
                checked_ids = request.form.getlist("assigned_gtasks", type=int)
                GeneralTaskAssignment.query.filter_by(user_id=student_id).delete(
                    synchronize_session="fetch")
                for tid in checked_ids:
                    db.session.add(GeneralTaskAssignment(
                        user_id=student_id, task_id=tid))
                db.session.commit()
                flash("משימות כלליות עודכנו בהצלחה.", "success")

        elif action == "ai_suggest_tasks":
            student_id = request.form.get("student_id", type=int)
            student    = User.query.get_or_404(student_id)
            suggestions = ai_suggest_student_tasks(student)
            if suggestions:
                count = 0
                for s in suggestions:
                    db.session.add(GeneralTask(
                        title=s.get("title", "משימה"),
                        description=s.get("description", ""),
                        task_type=s.get("task_type", "ai_generated"),
                        ai_hint=s.get("ai_hint", ""),
                    ))
                    count += 1
                db.session.commit()
                flash(f"נוצרו {count} משימות AI מותאמות אישית — שייך אותן לסטודנט מהרשימה.", "success")
            else:
                flash("יצירת משימות AI נכשלה או שמפתח AI_API_KEY לא הוגדר.", "warning")

        return redirect(url_for("admin"))

    # ── Build context ──
    all_assignments  = QuestionAssignment.query.all()
    all_answers_flat = Answer.query.all()
    answered_map: dict = {}
    for ans in all_answers_flat:
        answered_map.setdefault(ans.user_id, set()).add(ans.question_id)

    student_assignments: dict = {}
    for s in students:
        assigned_set = {qa.question_id for qa in all_assignments if qa.user_id == s.id}
        student_assignments[s.id] = {
            "assigned": assigned_set,
            "answered": assigned_set & answered_map.get(s.id, set()),
        }

    all_general_tasks = GeneralTask.query.order_by(GeneralTask.created_at.desc()).all()
    all_gta           = GeneralTaskAssignment.query.all()
    student_gtask_assignments: dict = {}
    for s in students:
        assigned  = {a.task_id for a in all_gta if a.user_id == s.id}
        completed = {a.task_id for a in all_gta if a.user_id == s.id and a.completed}
        student_gtask_assignments[s.id] = {"assigned": assigned, "completed": completed}

    all_answers = (
        db.session.query(Answer, User, Question)
        .join(User, Answer.user_id == User.id)
        .join(Question, Answer.question_id == Question.id)
        .order_by(User.username, Question.title)
        .all()
    )

    total_assigned = sum(len(d["assigned"]) for d in student_assignments.values())
    total_answered = sum(len(d["answered"]) for d in student_assignments.values())
    completion_pct = round(total_answered / total_assigned * 100) if total_assigned > 0 else 0

    return render_template(
        "admin.html",
        user=user,
        students=students,
        questions=questions,
        all_answers=all_answers,
        student_assignments=student_assignments,
        completion_pct=completion_pct,
        all_general_tasks=all_general_tasks,
        student_gtask_assignments=student_gtask_assignments,
    )


# ---------------------------------------------------------------------------
# Seed & init
# ---------------------------------------------------------------------------

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

    if Question.query.count() == 0:
        starter_questions = [
            Question(title="ספר/י על מקרה שבו הובלת צוות דרך פרויקט מאתגר.",
                     category="Leadership", target_role="Software Engineer",
                     hint="התמקד/י בתפקידך האישי בהובלה, כיצד קיבלת החלטות ומה למדת. ציין/י מספרים — גודל הצוות, משך הפרויקט, תוצאות מדידות."),
            Question(title="תאר/י מצב שבו נדרשת לפתור בעיה טכנית מורכבת תחת לחץ זמן.",
                     category="Problem-Solving", target_role="Software Engineer",
                     hint="הדגש/י את תהליך החשיבה שלך — איך פירקת את הבעיה לחלקים? מה ניסית? מה עבד? השתמש/י בנתונים כמותיים לתוצאה."),
            Question(title="תן/י דוגמה למקרה שבו עבדת בשיתוף פעולה חוצה-ארגוני כדי להשיג תוצאה.",
                     category="Teamwork", target_role="Software Engineer",
                     hint="הראה/י שאתה/את יודע/ת לנהל קשרים מחוץ לצוות הישיר. פרט/י את האתגרים בתקשורת ואיך התגברת עליהם."),
            Question(title="ספר/י על מקרה שבו היה עליך להסביר רעיון מורכב לקהל לא-טכני.",
                     category="Communication", target_role="Product Manager",
                     hint="בחר/י דוגמה שבה ההסבר הוביל לתוצאה — החלטה, אישור תקציב, שינוי עמדה. הראה/י שהתאמת את שפתך לקהל."),
            Question(title="תאר/י מצב שבו עדיפויות השתנו בצורה בלתי צפויה — כיצד הסתגלת?",
                     category="Adaptability", target_role="Product Manager",
                     hint="אל תראה/י את השינוי כבעיה — הראה/י גמישות ויוזמה. מה הוקרב? מה הועדף? מה למדת לגבי קבלת החלטות?"),
        ]
        db.session.add_all(starter_questions)

    db.session.commit()


with app.app_context():
    db.create_all()

    # Safe SQLite migration — adds new columns to existing databases
    migrations = [
        "ALTER TABLE users ADD COLUMN full_name TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN email TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN age INTEGER",
        "ALTER TABLE users ADD COLUMN degree_field TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN interests TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN career_goals TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN previous_experience TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN main_challenges TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN ai_onboarding_analysis TEXT DEFAULT ''",
        "ALTER TABLE questions ADD COLUMN hint TEXT DEFAULT ''",
    ]
    with db.engine.connect() as conn:
        for stmt in migrations:
            try:
                conn.execute(db.text(stmt))
                conn.commit()
            except Exception:
                conn.rollback()

    seed_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000,
            debug=os.environ.get("FLASK_DEBUG", "false").lower() == "true")
