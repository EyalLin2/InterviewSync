# InterviewSync — Mentor CRM & Student Management System

> **Hebrew RTL, AI-powered career coaching platform** for mentors working with high-school and university students.
> Built as a **multi-tier monolith** for a DevOps Final Graduation Project.

---

## Architecture

```
Browser  →  Frontend Flask BFF :5001  →  Backend FastAPI :8000  →  PostgreSQL :5432
              (Jinja2, JWT session)        (stateless, JWT auth)      (external DB)
```

## Quick Start (Local)

```bash
git clone https://github.com/EyalLin2/InterviewSync
cd InterviewSync
docker-compose up --build
```

- **App**: http://localhost:5001 — login: `admin / admin123` or `student1 / student123`
- **API health**: http://localhost:8000/health
- **API docs**: http://localhost:8000/docs ← Swagger UI (FastAPI auto-generated)

---

## Application Structure

```
InterviewSync/
├── frontend/              # Flask BFF — serves Hebrew RTL UI, JWT session
│   ├── app.py             # All page routes + backend API proxy
│   ├── templates/         # 12 Jinja2 templates
│   │   ├── base.html, login.html, onboarding.html
│   │   ├── admin_hub.html          ← Dual-mode landing (Private / Business)
│   │   ├── admin.html              ← Student CRM + focus panel + bulk actions
│   │   ├── student_file.html       ← Student detail (profile/AI/CV/notes/tasks)
│   │   ├── student_settings.html   ← Student self-service (profile + password)
│   │   ├── admin_submissions.html  ← Submission inbox with inline feedback
│   │   ├── admin_business.html     ← Business: workshops/inquiries/activities
│   │   ├── admin_schedule.html     ← Unified calendar (meetings + workshops)
│   │   └── index.html, student_schedule.html, meeting_confirm.html
│   ├── Dockerfile, requirements.txt
│   └── charts/            ← Helm chart (Deployment, Service, Ingress)
│
├── backend/               # FastAPI REST API — stateless business logic
│   ├── app.py             # 50+ REST endpoints (FastAPI + uvicorn)
│   ├── models.py          # 10 SQLAlchemy models (standalone, DeclarativeBase)
│   ├── Dockerfile, requirements.txt
│   └── charts/            ← Helm chart (Deployment, Service)
│
├── docker-compose.yml     # Local dev: postgres + backend + frontend
├── .github/workflows/ci.yml  ← GitHub Actions CI/CD pipeline
├── Makefile               # make test / make lint / make build
└── setup.cfg              # flake8 config
```

---

## Features

### 👤 Private CRM (`/admin/private`) — Student Management
- **Dashboard** — focus panel: inactive students, overdue tasks, pending submissions, unconfirmed meetings
- **Student roster** — progress bars, last-activity tracking, bulk status actions (active / paused / completed)
- **Student file** — profile · AI coaching strategy · CV editor · mentor notes · task assignment with due dates
- **Submission inbox** — all completed tasks with notes/files, inline feedback, filter by pending/received
- **Task Bank** — CRUD with resource file attachments
- **Meeting scheduler** — Hebrew calendar, WhatsApp confirmation flow with HMAC token

### 💼 Business (`/admin/business`) — Workshops & Inquiries
- **Overview dashboard** — stats + upcoming workshops + recent inquiries
- **Workshops** — one-time / recurring, status lifecycle
- **Inquiries** — track leads, assign to workshops
- **Activity Log** — timeline of professional activities

### 🎓 Student Portal
- **Task dashboard** — active + completed tasks, urgency badges (overdue red / due-soon yellow), feedback notifications
- **Schedule** — upcoming meetings with confirmation status
- **Settings** — edit profile (name, email, phone, goals) + change password

### 🤖 AI Features
- **Coaching strategy** — auto-generated on onboarding, regenerable by admin (Groq llama-3.3-70b)
- **AI chat** — admin chats with full student context (tasks, velocity, overdue count, category breakdown, meeting history)
- **Task suggestions** — AI generates 5 tasks from student profile; admin reviews before assigning

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend BFF | Python 3.11, Flask 3.0, Jinja2, Heebo font |
| Backend API | Python 3.11, **FastAPI 0.111**, **uvicorn**, PyJWT |
| Database | PostgreSQL 15 (SQLAlchemy 2.0, standalone `DeclarativeBase`) |
| Auth | JWT (7-day tokens, `HTTPBearer`) |
| AI | Groq llama-3.3-70b-versatile (optional) |
| Notifications | Twilio WhatsApp (optional) |
| Scheduler | APScheduler — daily inactivity reminders |
| UI | Bootstrap 5.3.3 RTL, Hebrew RTL layout |
| Containers | Docker, Docker Compose |
| Kubernetes | Helm charts (2 services), AWS ECR, ArgoCD-ready |
| CI/CD | GitHub Actions (lint → test → build → ECR push) |
| Tests | pytest |

---

## Environment Variables

### Backend
| Variable | Required | Description |
|----------|----------|-------------|
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | ✅ | PostgreSQL connection |
| `SECRET_KEY` | ✅ | JWT signing key |
| `GROQ_API_KEY` | ⬜ | Groq AI (AI features disabled if missing) |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM` | ⬜ | WhatsApp notifications |
| `FRONTEND_URL` | ⬜ | Used in meeting confirmation links (default: `http://localhost:5000`) |
| `INACTIVITY_DAYS` | ⬜ | Days before inactivity reminder fires (default: `7`) |

### Frontend
| Variable | Required | Description |
|----------|----------|-------------|
| `BACKEND_URL` | ✅ | Backend URL (e.g., `http://backend:8000`) |
| `SECRET_KEY` | ✅ | Flask session key |

---

## Completed Phases

| Phase | Feature | Status |
|-------|---------|--------|
| 1 | Feedback loop — task submission + mentor feedback + WhatsApp notify | ✅ Done |
| 2 | Student self-service — edit profile + change password | ✅ Done |
| 3 | Task due dates — urgency badges + overdue focus panel | ✅ Done |
| 4 | Submission inbox + bulk student status actions | ✅ Done |
| — | FastAPI migration — Flask backend → FastAPI + uvicorn | ✅ Done |

## Upcoming

| Phase | Feature |
|-------|---------|
| 5 | AI risk panel — green/yellow/red student risk scores on dashboard |
| 6 | Meeting outcomes — admin logs outcome, student sees meeting summary |
| 7 | Smart notifications — overdue alerts + milestone celebrations |
| 8 | Test coverage 60%+ |
