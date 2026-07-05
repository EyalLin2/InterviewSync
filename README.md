# InterviewSync — AI Career Coaching CRM

> **Hebrew RTL, AI-powered career coaching platform** for mentors working with students and career seekers.  
> Final project for a DevOps graduation course — full end-to-end stack: app → Docker → Kubernetes → Terraform → ArgoCD → Prometheus.

[![CI](https://github.com/EyalLin2/InterviewSync/actions/workflows/ci.yml/badge.svg)](https://github.com/EyalLin2/InterviewSync/actions/workflows/ci.yml)
[![Deploy](https://github.com/EyalLin2/InterviewSync/actions/workflows/deploy.yml/badge.svg)](https://github.com/EyalLin2/InterviewSync/actions/workflows/deploy.yml)

---

## Repository Structure (3 repos)

| Repo | Purpose |
|------|---------|
| **[InterviewSync](https://github.com/EyalLin2/InterviewSync)** (this repo) | App code, Helm charts, GitHub Actions CI/CD |
| **[interviewsync-infra](https://github.com/EyalLin2/interviewsync-infra)** | Terraform IaC — VPC, EKS, ECR, cluster addons |
| **[interviewsync-gitops](https://github.com/EyalLin2/interviewsync-gitops)** | ArgoCD desired state — Helm values per environment |

---

## Architecture

```
Browser
  │
  ▼
┌──────────────────────────┐   HTTP/JWT   ┌──────────────────────────┐
│  Frontend (Flask BFF)    │ ──────────► │  Backend (FastAPI)        │
│  Port 5001 / K8s :5000   │             │  Port 8000               │
│  Renders Hebrew RTL HTML │             │  REST API, JWT auth       │
│  Proxies all data to API │             │  AI (Groq), WhatsApp      │
└──────────────────────────┘             └────────────┬─────────────┘
                                                       │ SQLAlchemy ORM
                                                       ▼
                                         ┌──────────────────────────┐
                                         │  PostgreSQL 15            │
                                         │  Port 5432               │
                                         └──────────────────────────┘

Cloud (AWS EKS):
  nginx Ingress → Frontend (2 pods) → Backend (2 pods) → PostgreSQL StatefulSet
  Prometheus + Grafana | ArgoCD | Fluent Bit → CloudWatch
```

---

## Quick Start (Local)

```bash
git clone https://github.com/EyalLin2/InterviewSync
cd InterviewSync
cp .env.example .env   # add GROQ_API_KEY if you want AI features
docker compose up --build
```

| URL | Purpose |
|-----|---------|
| http://localhost:5001 | App — login: `admin / admin123` or `student1 / student123` |
| http://localhost:8000/health | Backend health check |
| http://localhost:8000/docs | Swagger UI (FastAPI auto-generated) |

---

## Features

### Admin — Private CRM (`/admin/private`)
| Feature | Description |
|---------|-------------|
| **Focus Panel** | Morning workflow: inactive students, overdue tasks, pending submissions, unconfirmed meetings |
| **Risk Scores** | AI-computed green/yellow/red risk dot per student (inactivity + overdue + velocity) |
| **Student Roster** | Progress bars, last activity, bulk status actions (active / paused / completed) |
| **Student File** | Profile · AI coaching strategy · CV editor · mentor notes · task assignment with due dates |
| **AI Task Suggestions** | Groq LLM generates 5 personalized tasks per student — assign directly or save to bank |
| **Submission Inbox** | All completed tasks in one view with inline feedback fields |
| **Task Bank** | Global reusable task library with CRUD, categories, file attachments |
| **Meeting Scheduler** | Hebrew calendar, WhatsApp confirmation flow, outcome notes |
| **AI Chat** | Admin chats with full student context (tasks, velocity, category breakdown, meetings) |

### Admin — Business (`/admin/business`)
| Feature | Description |
|---------|-------------|
| **Workshops** | One-time / recurring, status lifecycle (planned → completed) |
| **Inquiries** | Lead tracking, source, notes, assign to workshops |
| **Activity Log** | Professional activity timeline |
| **Billing** | Monthly invoicing per student, service catalog, paid/unpaid tracking |

### Student Portal
| Feature | Description |
|---------|-------------|
| **Task Dashboard** | Active tasks with urgency badges (overdue / due-soon), completed tasks with feedback |
| **Task Q&A** | Ask questions on any task — mentor replies inline; new reply badge |
| **AI Self-Chat** | Student asks AI about their own progress, next steps, weak areas |
| **Progress Analytics** | Category skill breakdown bars, velocity stats, days in process |
| **Schedule** | Upcoming meetings with confirm flow; past meetings show outcome summary |
| **Settings** | Edit profile (name, email, phone, goals) + change password |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend BFF | Python 3.11, Flask 3.0, Jinja2 RTL, Bootstrap 5.3.3 RTL, Lucide icons |
| Backend API | Python 3.11, FastAPI 0.111, Uvicorn (ASGI), PyJWT |
| Database | PostgreSQL 15, SQLAlchemy 2.0 ORM |
| Auth | JWT (7-day tokens, HS256, HTTPBearer) |
| AI | Groq `llama-3.3-70b-versatile` (disabled gracefully if key missing) |
| Notifications | Twilio WhatsApp (optional) |
| Scheduler | APScheduler — daily inactivity reminders inside FastAPI |
| Containers | Docker, Docker Compose (local dev) |
| Registry | AWS ECR (interviewsync-backend, interviewsync-frontend) |
| Kubernetes | AWS EKS 1.29 — 2 replicas per service, resource limits, health probes |
| Helm | `backend/charts/` + `frontend/charts/` — Deployment, Service, Ingress, Job, PrometheusRule |
| Infrastructure | Terraform — VPC, EKS, ECR, nginx-ingress, Prometheus, ArgoCD, Fluent Bit |
| GitOps | ArgoCD — auto-sync from `interviewsync-gitops` on every image tag update |
| CI/CD | GitHub Actions: Lint → Test → Docker Build → Helm Lint → ECR Push → GitOps update |
| Monitoring | kube-prometheus-stack (Prometheus + Grafana + Alertmanager → Slack) |
| Logging | AWS Fluent Bit → CloudWatch Logs (`/eks/<env>-interviewsync`) |
| Tests | pytest, FastAPI TestClient, real PostgreSQL in CI |

---

## Project Structure

```
InterviewSync/
├── backend/
│   ├── app.py                    # 60+ FastAPI endpoints (auth, students, tasks, AI, billing)
│   ├── models.py                 # 11 SQLAlchemy models
│   ├── tests/                    # pytest integration tests (real PostgreSQL)
│   ├── Dockerfile                # python:3.11-slim + gunicorn + uvicorn worker
│   ├── requirements.txt
│   └── charts/                   # Helm chart
│       ├── Chart.yaml
│       ├── values.yaml
│       └── templates/
│           ├── deployment.yaml   # 2 replicas, liveness+readiness on /health
│           ├── service.yaml      # ClusterIP :8000 (internal only)
│           ├── job.yaml          # DB schema init — runs pre-install/pre-upgrade
│           └── prometheusrule.yaml  # PodCrashLooping + PodNotReady → Slack
│
├── frontend/
│   ├── app.py                    # All Flask routes + backend API proxy
│   ├── templates/                # 14 Jinja2 templates (Hebrew RTL)
│   │   ├── base.html             # Design system CSS tokens + Bootstrap RTL
│   │   ├── _navbar.html          # Role-based navbar partial (student/admin/business)
│   │   ├── admin_hub.html        # Dual-mode landing (admin CRM + business)
│   │   ├── admin.html            # Student roster + focus panel + risk dots
│   │   ├── student_file.html     # Student detail: profile / AI / tasks / notes
│   │   └── ...                   # (login, onboarding, billing, schedule, etc.)
│   ├── Dockerfile
│   ├── requirements.txt
│   └── charts/                   # Helm chart
│       ├── Chart.yaml
│       ├── values.yaml
│       └── templates/
│           ├── deployment.yaml   # 2 replicas, liveness+readiness on /login
│           ├── service.yaml      # ClusterIP :5000
│           └── ingress.yaml      # nginx Ingress with TLS
│
├── .github/workflows/
│   ├── ci.yml                    # Lint → Test (real PG) → Docker Build → Helm Lint
│   └── deploy.yml                # ECR push + GitOps tag update (staging: main / prod: v*.*.*)
│
├── docker-compose.yml            # Local dev: postgres + backend + frontend
├── Makefile                      # make test / lint / coverage / build / up / down
└── setup.cfg                     # flake8 config (max-line 120)
```

---

## CI/CD Pipeline

### CI (`ci.yml`) — runs on every push + PR to `main` / `feature/**`

```
Stage 1: Lint       — flake8 backend/ + frontend/
Stage 2: Test       — pytest with real PostgreSQL 15 service container
Stage 3: Docker     — build both images (no push) — validates Dockerfiles
Stage 4: Helm Lint  — helm lint + helm template dry-run for both charts
```

### Deploy (`deploy.yml`) — runs on push to `main` or version tags

| Trigger | Environment | ECR tags | GitOps update |
|---------|-------------|----------|---------------|
| Push to `main` | Staging | `:latest` + `:<sha>` | `apps/staging/*-values.yaml` |
| Git tag `v*.*.*` | Production | `:<version>` + `:stable` | `apps/production/*-values.yaml` |

After ECR push, the workflow commits the new image SHA into `interviewsync-gitops`. ArgoCD detects the change within ~3 minutes and rolls out the new pods automatically.

### Required GitHub Secrets

Go to **GitHub → Settings → Secrets and Variables → Actions** and add:

| Secret | Description |
|--------|-------------|
| `AWS_ACCESS_KEY_ID` | IAM user with ECR push + EKS read |
| `AWS_SECRET_ACCESS_KEY` | IAM secret key |
| `AWS_REGION` | e.g. `us-east-1` |
| `AWS_ACCOUNT_ID` | 12-digit AWS account number |
| `GITOPS_TOKEN` | GitHub PAT with `repo` scope (for interviewsync-gitops) |
| `SLACK_WEBHOOK_URL` | (optional) Slack incoming webhook for build notifications |

### Branch Protection (`main`)
- Require status checks: `Lint`, `Test`, `Docker Build`, `Helm Lint`
- Require pull request before merging

**Feature branch convention:** `feature/RND-123-description`

---

## Kubernetes Deployment (AWS EKS)

### Helm Charts Summary

| Resource | Backend chart | Frontend chart |
|----------|--------------|----------------|
| Deployment | 2 replicas, `/health` probes | 2 replicas, `/login` probes |
| Service | ClusterIP :8000 (internal) | ClusterIP :5000 |
| Ingress | disabled — backend is private | nginx + TLS |
| Job | DB init (pre-install hook) | — |
| PrometheusRule | PodCrashLooping + PodNotReady alerts | — |

### Deploy to Kubernetes (via ArgoCD)

See **[interviewsync-infra README](https://github.com/EyalLin2/interviewsync-infra)** for full setup instructions.

Quick reference:
```bash
# 1. Apply Terraform (creates EKS + ECR + addons)
cd interviewsync-infra/environments/staging
terraform init && terraform apply

# 2. Configure kubectl
aws eks update-kubeconfig --region us-east-1 --name staging-interviewsync

# 3. Apply ArgoCD Applications
kubectl apply -f https://raw.githubusercontent.com/EyalLin2/interviewsync-gitops/main/argocd/staging/app-postgres.yaml
kubectl apply -f https://raw.githubusercontent.com/EyalLin2/interviewsync-gitops/main/argocd/staging/app-backend.yaml
kubectl apply -f https://raw.githubusercontent.com/EyalLin2/interviewsync-gitops/main/argocd/staging/app-frontend.yaml

# 4. Access ArgoCD UI
kubectl port-forward svc/argocd-server -n argocd 8080:443
# http://localhost:8080 — admin / (kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d)

# 5. Access Grafana (Kubernetes / Nodes dashboard built-in)
kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80
# http://localhost:3000
```

---

## Environment Variables

### Backend
| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DB_HOST/PORT/NAME/USER/PASSWORD` | ✅ | — | PostgreSQL connection |
| `SECRET_KEY` | ✅ | dev key | JWT signing key (change in production) |
| `GROQ_API_KEY` | ⬜ | — | Groq LLM — AI features silently disabled if missing |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_WHATSAPP_FROM` | ⬜ | — | WhatsApp notifications |
| `FRONTEND_URL` | ⬜ | `http://localhost:5001` | Used in meeting confirmation links |
| `INACTIVITY_DAYS` | ⬜ | `7` | Days before WhatsApp reminder fires |

### Frontend
| Variable | Required | Description |
|----------|----------|-------------|
| `BACKEND_URL` | ✅ | Backend URL (e.g., `http://backend:8000`) |
| `SECRET_KEY` | ✅ | Flask session signing key |

---

## Feature Completion

| Phase | Feature | Status |
|-------|---------|--------|
| 1 | Feedback loop — task submission + mentor feedback + WhatsApp | ✅ |
| 2 | Student self-service — edit profile + change password | ✅ |
| 3 | Task due dates — urgency badges + overdue focus panel | ✅ |
| 4 | Submission inbox + bulk student status actions | ✅ |
| — | FastAPI migration — Flask backend → FastAPI + Uvicorn | ✅ |
| 5 | AI risk panel — green/yellow/red risk scores | ✅ |
| 6 | Student portal — Q&A, AI chat, progress analytics, meeting outcomes | ✅ |
| 7 | Smart notifications — overdue WhatsApp + inactivity reminders | ✅ |
| 8 | Test coverage 60%+ | ✅ |
| — | UI design system — CSS tokens, role-based navbar, Lucide icons | ✅ |
| — | CI: Helm lint stage | ✅ |
| — | CD: ECR registry + ArgoCD GitOps trigger | ✅ |
| — | Helm: DB init Job + PrometheusRule (Slack alerts) | ✅ |
| — | Terraform infra repo (VPC + EKS + ECR + addons) | ✅ |
| — | ArgoCD GitOps repo (staging + production environments) | ✅ |
