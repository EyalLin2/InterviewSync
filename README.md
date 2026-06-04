# InterviewSync — Mentor CRM & Student Management System

> **Hebrew RTL, AI-powered career coaching platform** for mentors working with high-school and university students.
> Built as a **multi-tier microservice** for a DevOps Final Graduation Project.

---

## Architecture

```
Browser  →  Frontend Flask :5001  →  Backend REST API :8000  →  PostgreSQL :5432
              (BFF, JWT session)       (stateless, JWT auth)      (external DB)
```

## Quick Start (Local)

```bash
git clone https://github.com/EyalLin2/InterviewSync
cd InterviewSync
docker-compose up --build
```

- **App**: http://localhost:5001 — login: `admin / admin123` or `student1 / student123`
- **API health**: http://localhost:8000/health

---

## Application Structure

```
InterviewSync/
├── frontend/              # Flask BFF — serves Hebrew RTL UI, JWT session
│   ├── app.py             # All page routes + backend API proxy
│   ├── templates/         # 9 Jinja2 templates
│   │   ├── base.html, login.html, onboarding.html
│   │   ├── admin_hub.html          ← Dual-mode landing (Private / Business)
│   │   ├── admin.html              ← Private: student CRM
│   │   ├── student_file.html       ← Student detail (tabs: profile/AI/CV/notes)
│   │   ├── admin_business.html     ← Business: workshops/inquiries/activities
│   │   ├── admin_schedule.html     ← Meeting calendar
│   │   └── index.html, student_schedule.html, meeting_confirm.html
│   ├── Dockerfile, requirements.txt
│   └── charts/            ← Helm chart (Deployment, Service, Ingress)
│
├── backend/               # Flask REST API — stateless business logic
│   ├── app.py             # 40+ REST endpoints
│   ├── models.py          # 8 SQLAlchemy models
│   ├── Dockerfile, requirements.txt
│   └── charts/            ← Helm chart (Deployment, Service)
│
├── docker-compose.yml     # Local dev: postgres + backend + frontend
├── .github/workflows/ci.yml  ← GitHub Actions CI/CD pipeline
├── Makefile               # make test / make lint / make build
└── setup.cfg              # flake8 config
```

---

## Admin Modes

### 👤 Private (`/admin/private`) — Student CRM
- **360 student table** with progress bars, WhatsApp status
- **Student file** (tabs: Profile · AI Coaching · CV · Mentor Notes)
  - Quick Actions Bar: WhatsApp, email, schedule meeting, status badge
  - At-a-glance stats: days in process, task completion, next meeting
  - Edit profile modal, AI coaching strategy, CV editor
  - Task assignment modal with live search
- **Task Bank** — CRUD with file attachments (resource files for students)
- **Meeting Scheduler** — Hebrew calendar, WhatsApp confirmation flow

### 💼 Business (`/admin/business`) — Workshops & Inquiries
- **Overview dashboard** — stats + upcoming workshops + recent inquiries
- **Workshops** — one-time / recurring / custom, status lifecycle
- **Individual Inquiries** — track people who reach out, assign to workshops
- **Activity Log** — timeline of all professional activities

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend BFF | Python 3.11, Flask 3.0, Jinja2, Heebo font |
| Backend API | Python 3.11, Flask 3.0, Flask-CORS, PyJWT |
| Database | PostgreSQL 15 (SQLAlchemy 2.0) |
| Auth | JWT (7-day tokens, PyJWT) |
| AI | OpenAI GPT-4o-mini (optional) |
| Notifications | Twilio WhatsApp (optional) |
| UI | Bootstrap 5.3.3 RTL, Hebrew RTL layout |
| Containers | Docker, Docker Compose |
| Kubernetes | Helm charts (2 services), AWS ECR, ArgoCD-ready |
| CI/CD | GitHub Actions (lint → test → build → ECR push → ArgoCD sync) |
| Tests | pytest (10 backend + 8 frontend) |

---

## Environment Variables

### Backend
| Variable | Required | Description |
|----------|----------|-------------|
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | ✅ | PostgreSQL connection |
| `SECRET_KEY` | ✅ | JWT signing key |
| `AI_API_KEY` | ⬜ | OpenAI (AI features disabled if missing) |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM` | ⬜ | WhatsApp notifications |

### Frontend
| Variable | Required | Description |
|----------|----------|-------------|
| `BACKEND_URL` | ✅ | Backend URL (e.g., `http://backend:8000`) |
| `SECRET_KEY` | ✅ | Flask session key (must match backend) |
