# InterviewSync — Mentor CRM & Student Management System

> **Hebrew RTL, AI-powered career coaching platform** for mentors working with high-school and university students.
> Built as a **multi-tier microservice** for a DevOps Final Graduation Project.

---

## Architecture

```
Browser
  │ HTTP (Hebrew RTL UI)
  ▼
┌─────────────────────────┐
│   Frontend  :5000       │  Flask BFF — renders Jinja2 templates
│   (frontend/)           │  No DB access. JWT stored in session.
└────────────┬────────────┘
             │ REST API  Authorization: Bearer <jwt>
             ▼
┌─────────────────────────┐
│   Backend   :8000       │  Flask REST API — stateless business logic
│   (backend/)            │  AI (OpenAI), WhatsApp (Twilio), JWT auth
└────────────┬────────────┘
             │ SQL (psycopg2)
             ▼
┌─────────────────────────┐
│   PostgreSQL  :5432     │  External DB — configured via env vars
└─────────────────────────┘
```

---

## Features

| Feature | Description |
|---------|-------------|
| 👤 Student Management | Admin creates students, views full profiles, tracks progress |
| 📋 Task Bank | Global task pool (Resume, LinkedIn, Interview Prep) — assign to specific students |
| 🤖 AI Coaching | GPT-4o-mini generates personalized coaching strategies and task suggestions |
| 📅 Meeting Scheduler | Monthly Hebrew calendar, WhatsApp confirmation flow, reminders |
| 📁 File Uploads | Admin attaches resources to tasks; students submit files as proof |
| 📄 CV Management | Mentor pastes/edits student CV directly in student file |
| 🔔 WhatsApp Notifications | Via Twilio — task assignments, meeting confirmations, reminders |
| 🌐 Hebrew RTL | Full right-to-left layout, Heebo font, professional Hebrew UI |

---

## Quick Start (Local)

### Prerequisites
- Docker and Docker Compose

### Run
```bash
git clone <repo>
cd InterviewSync
docker-compose up --build
```

- **Frontend**: http://localhost:5000
- **Backend API**: http://localhost:8000/health
- **Default login**: admin / admin123 or student1 / student123

---

## Environment Variables

### Backend
| Variable | Required | Description |
|----------|----------|-------------|
| DB_HOST | yes | PostgreSQL host |
| DB_PORT | yes | PostgreSQL port (default 5432) |
| DB_NAME | yes | Database name |
| DB_USER | yes | Database user |
| DB_PASSWORD | yes | Database password |
| SECRET_KEY | yes | JWT signing key |
| AI_API_KEY | no | OpenAI key (AI features disabled if missing) |
| TWILIO_ACCOUNT_SID | no | Twilio SID (WhatsApp disabled if missing) |
| TWILIO_AUTH_TOKEN | no | Twilio auth token |
| TWILIO_WHATSAPP_FROM | no | e.g. whatsapp:+14155238886 |
| FRONTEND_URL | no | For WhatsApp confirmation links |

### Frontend
| Variable | Required | Description |
|----------|----------|-------------|
| BACKEND_URL | yes | Backend service URL (e.g. http://backend:8000) |
| SECRET_KEY | yes | Flask session key (must match backend) |

---

## Kubernetes / Helm Deployment

### Build and Push to AWS ECR
```bash
docker build -t interviewsync-backend ./backend
docker tag interviewsync-backend <ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com/interviewsync-backend:latest
docker push <ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com/interviewsync-backend:latest

docker build -t interviewsync-frontend ./frontend
docker tag interviewsync-frontend <ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com/interviewsync-frontend:latest
docker push <ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com/interviewsync-frontend:latest
```

### Create ECR Pull Secret
```bash
kubectl create secret docker-registry ecr-registry-secret \
  --docker-server=<ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com \
  --docker-username=AWS \
  --docker-password=$(aws ecr get-login-password --region <REGION>)
```

### Deploy with Helm
```bash
helm upgrade --install interviewsync-backend ./backend/charts \
  --set image.repository=<ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com/interviewsync-backend \
  --set env.DB_HOST=<YOUR_PG_HOST> \
  --set env.DB_PASSWORD=<YOUR_PG_PASSWORD> \
  --set env.SECRET_KEY=<YOUR_SECRET>

helm upgrade --install interviewsync-frontend ./frontend/charts \
  --set image.repository=<ACCOUNT>.dkr.ecr.<REGION>.amazonaws.com/interviewsync-frontend \
  --set env.SECRET_KEY=<YOUR_SECRET> \
  --set ingress.hosts[0].host=<YOUR_DOMAIN>
```

### Validate charts
```bash
helm template interviewsync-backend ./backend/charts --debug
helm template interviewsync-frontend ./frontend/charts --debug
```

---

## Project Structure

```
InterviewSync/
├── frontend/              # Flask BFF — serves Hebrew RTL UI
│   ├── app.py             # Routes + backend API proxy
│   ├── templates/         # 9 Jinja2 templates (base + 8 pages)
│   ├── static/
│   ├── requirements.txt
│   ├── Dockerfile
│   └── charts/            # Helm chart (Deployment, Service, Ingress)
│       ├── Chart.yaml
│       ├── values.yaml
│       └── templates/
├── backend/               # Flask REST API — stateless business logic
│   ├── app.py             # All REST endpoints + JWT auth
│   ├── models.py          # SQLAlchemy models (PostgreSQL)
│   ├── requirements.txt
│   ├── Dockerfile
│   └── charts/            # Helm chart (Deployment, Service)
│       ├── Chart.yaml
│       ├── values.yaml
│       └── templates/
├── docker-compose.yml     # Local dev (postgres + backend + frontend)
├── .gitignore
└── README.md
```
