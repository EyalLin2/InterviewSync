import os
import json
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
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
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(10), nullable=False, default="student")  # student | admin
    answers = db.relationship("Answer", backref="user", lazy=True)


class Question(db.Model):
    __tablename__ = "questions"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(512), nullable=False)
    category = db.Column(db.String(100), nullable=False)
    target_role = db.Column(db.String(100), nullable=False)
    answers = db.relationship("Answer", backref="question", lazy=True)


class Answer(db.Model):
    __tablename__ = "answers"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    question_id = db.Column(db.Integer, db.ForeignKey("questions.id"), nullable=False)
    situation = db.Column(db.Text, nullable=False)
    task = db.Column(db.Text, nullable=False)
    action = db.Column(db.Text, nullable=False)
    result = db.Column(db.Text, nullable=False)
    mentor_feedback = db.Column(db.Text, default="")
    ai_feedback = db.Column(db.Text, default="")


# ---------------------------------------------------------------------------
# AI helpers
# ---------------------------------------------------------------------------

def get_ai_client():
    api_key = os.environ.get("AI_API_KEY")
    if not api_key:
        return None
    return OpenAI(api_key=api_key)


def ai_analyze_star(situation, task, action, result_text, question_title):
    """Return AI feedback on a STAR answer, or a placeholder if no key."""
    client = get_ai_client()
    if not client:
        return "AI feedback is unavailable (AI_API_KEY not set)."

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
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
            temperature=0.7,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"AI analysis failed: {str(e)}"


def ai_generate_questions(target_role, count=3):
    """Return a list of interview question dicts for the given role."""
    client = get_ai_client()
    if not client:
        return []

    prompt = f"""Generate exactly {count} behavioral interview questions for a {target_role} role.

Return ONLY a JSON array with this exact structure (no markdown, no extra text):
[
  {{"title": "question text", "category": "category name"}},
  ...
]

Categories should be one of: Leadership, Problem-Solving, Teamwork, Communication, Adaptability, Technical."""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
            temperature=0.8,
        )
        raw = response.choices[0].message.content.strip()
        # Strip potential markdown fences
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        questions = json.loads(raw)
        return questions[:count]
    except Exception as e:
        app.logger.error("ai_generate_questions error: %s", e)
        return []


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def current_user():
    uid = session.get("user_id")
    return User.query.get(uid) if uid else None


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
            flash("Admin access required.", "danger")
            return redirect(url_for("index"))
        return fn(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            session["user_id"] = user.id
            flash(f"Welcome back, {user.username}!", "success")
            return redirect(url_for("admin") if user.role == "admin" else url_for("index"))
        flash("Invalid username or password.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    user = current_user()
    if user.role == "admin":
        return redirect(url_for("admin"))

    questions = Question.query.order_by(Question.category).all()
    my_answers = {a.question_id: a for a in Answer.query.filter_by(user_id=user.id).all()}

    if request.method == "POST":
        question_id = request.form.get("question_id", type=int)
        situation = request.form.get("situation", "").strip()
        task = request.form.get("task", "").strip()
        action = request.form.get("action", "").strip()
        result = request.form.get("result", "").strip()

        if not all([question_id, situation, task, action, result]):
            flash("Please fill in all four STAR fields.", "warning")
            return redirect(url_for("index"))

        question = Question.query.get_or_404(question_id)

        # Get AI feedback immediately
        ai_feedback = ai_analyze_star(situation, task, action, result, question.title)

        existing = my_answers.get(question_id)
        if existing:
            existing.situation = situation
            existing.task = task
            existing.action = action
            existing.result = result
            existing.ai_feedback = ai_feedback
        else:
            answer = Answer(
                user_id=user.id,
                question_id=question_id,
                situation=situation,
                task=task,
                action=action,
                result=result,
                ai_feedback=ai_feedback,
            )
            db.session.add(answer)

        db.session.commit()
        flash("Answer submitted! AI feedback is ready below.", "success")
        return redirect(url_for("index"))

    return render_template("index.html", user=user, questions=questions, my_answers=my_answers)


@app.route("/admin", methods=["GET", "POST"])
@login_required
@admin_required
def admin():
    user = current_user()
    students = User.query.filter_by(role="student").order_by(User.username).all()
    questions = Question.query.order_by(Question.target_role, Question.category).all()

    if request.method == "POST":
        action = request.form.get("action")

        if action == "add_feedback":
            answer_id = request.form.get("answer_id", type=int)
            feedback = request.form.get("mentor_feedback", "").strip()
            answer = Answer.query.get_or_404(answer_id)
            answer.mentor_feedback = feedback
            db.session.commit()
            flash("Mentor feedback saved.", "success")

        elif action == "generate_questions":
            target_role = request.form.get("target_role", "").strip()
            if not target_role:
                flash("Please enter a target role.", "warning")
            else:
                generated = ai_generate_questions(target_role, count=3)
                if generated:
                    for q in generated:
                        new_q = Question(
                            title=q.get("title", "Untitled question"),
                            category=q.get("category", "General"),
                            target_role=target_role,
                        )
                        db.session.add(new_q)
                    db.session.commit()
                    flash(f"Generated and saved {len(generated)} AI questions for '{target_role}'.", "success")
                else:
                    flash("AI question generation failed or AI_API_KEY not set.", "warning")

        elif action == "add_question":
            title = request.form.get("title", "").strip()
            category = request.form.get("category", "").strip()
            target_role = request.form.get("target_role_manual", "").strip()
            if title and category and target_role:
                db.session.add(Question(title=title, category=category, target_role=target_role))
                db.session.commit()
                flash("Question added.", "success")
            else:
                flash("Please fill in all question fields.", "warning")

        return redirect(url_for("admin"))

    # Collect all answers with student info for the dashboard
    all_answers = (
        db.session.query(Answer, User, Question)
        .join(User, Answer.user_id == User.id)
        .join(Question, Answer.question_id == Question.id)
        .order_by(User.username, Question.title)
        .all()
    )

    return render_template(
        "admin.html",
        user=user,
        students=students,
        questions=questions,
        all_answers=all_answers,
    )


# ---------------------------------------------------------------------------
# Seed & init
# ---------------------------------------------------------------------------

def seed_db():
    """Create default admin, a sample student, and starter questions."""
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
            Question(title="Tell me about a time you led a team through a difficult project.", category="Leadership", target_role="Software Engineer"),
            Question(title="Describe a situation where you had to solve a complex technical problem under time pressure.", category="Problem-Solving", target_role="Software Engineer"),
            Question(title="Give an example of when you had to collaborate cross-functionally to deliver a result.", category="Teamwork", target_role="Software Engineer"),
            Question(title="Tell me about a time you had to communicate a complex idea to a non-technical audience.", category="Communication", target_role="Product Manager"),
            Question(title="Describe a situation where priorities shifted unexpectedly. How did you adapt?", category="Adaptability", target_role="Product Manager"),
        ]
        db.session.add_all(starter_questions)

    db.session.commit()


with app.app_context():
    db.create_all()
    seed_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=os.environ.get("FLASK_DEBUG", "false").lower() == "true")
