# InterviewSync — Ideas & Known Issues

Log of future feature ideas and structural bugs to convert into GitHub Issues for version tracking.

---

## Future Feature Ideas

### 1. Progress Dashboard
**Description:** A per-student view showing answer quality scores over time as a line or bar chart. The AI feedback already contains a rating (Weak / Developing / Strong / Excellent); parse and store it as a numeric score on each `Answer` record, then render a chart using Chart.js or similar.

**Value:** Lets students and mentors see improvement at a glance instead of re-reading old answers. Gives the mentor a quick signal on who needs more attention.

**Suggested label:** `enhancement`, `student-ux`

---

### 2. Mock Interview Mode
**Description:** A timed, sequential session mode that presents questions one at a time (random or mentor-curated), hides all other questions, and accepts a single STAR answer per question before moving to the next. At the end, generate a combined AI report covering patterns across all answers (e.g. "your Results consistently lack measurable outcomes").

**Value:** Simulates real interview pressure and reduces the "look up answers before submitting" temptation that the current all-questions-visible layout allows.

**Suggested label:** `enhancement`, `feature-request`

---

### 3. Google OAuth Login
**Description:** Replace the username/password form with a "Sign in with Google" button using `authlib` or `flask-dance`. Map Google account email to the `User` record; keep the admin role assignable manually by the existing admin.

**Value:** Eliminates the need to manage student credentials entirely. Students use an account they already trust, reducing signup friction in a real cohort.

**Suggested label:** `enhancement`, `auth`

---

## Structural Bugs / Limitations

### Bug 1: SQLite data is lost on container restart
**Description:** The `interviewsync.db` file is written to `/app/instance/` inside the container filesystem. When the container stops or is replaced (e.g. a new Docker build, ECS task replacement, or EC2 reboot without a volume), the entire database is wiped.

**Impact:** All users, questions, and answers are permanently deleted on any container lifecycle event.

**Fix options:**
- **Quick fix (EC2):** Mount a host directory as a volume: `-v $(pwd)/data:/app/instance`. The file then survives container restarts.
- **Production fix (ECS/Fargate):** Swap SQLite for Postgres. Only the `SQLALCHEMY_DATABASE_URI` env var needs to change; no model code changes are required.

**Suggested label:** `bug`, `infrastructure`, `data-loss`

---

### Bug 2: Synchronous AI calls block the Flask dev server
**Description:** `ai_analyze_star()` and `ai_generate_questions()` are blocking HTTP calls to the OpenAI API made directly inside Flask route handlers. Flask's built-in development server is single-threaded by default, so while one request waits for an AI response (typically 2–8 seconds), all other incoming requests queue behind it.

**Impact:** Under any concurrent load (multiple students submitting answers simultaneously), the app becomes unresponsive. One slow OpenAI call freezes the server for every other user.

**Fix options:**
- **Short-term:** Deploy with Gunicorn using multiple workers (`gunicorn -w 4 app:app`). Each worker handles requests independently, so one blocking call does not starve others.
- **Proper fix:** Move AI calls to a background task queue (Celery + Redis, or RQ). The route returns immediately with a "processing" state; a worker processes the AI call and updates the `Answer` record; the frontend polls or uses SSE to display the result when ready.

**Suggested label:** `bug`, `performance`, `backend`
