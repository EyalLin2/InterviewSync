# InterviewSync

> AI-powered behavioral interview preparation platform for mentors and students.

![Python](https://img.shields.io/badge/Python-3.10-blue?logo=python) ![Flask](https://img.shields.io/badge/Flask-3.0-lightgrey?logo=flask) ![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker) ![Bootstrap](https://img.shields.io/badge/Bootstrap-5.3-purple?logo=bootstrap)

---

## Overview

InterviewSync is a lightweight, self-contained web app that helps students practice behavioral interviews using the **STAR method** (Situation, Task, Action, Result) — with instant AI-generated feedback and mentor review in one place.

A mentor (admin) manages the question bank and leaves written feedback on student answers. Students submit STAR responses and immediately receive AI coaching, all without leaving the app.

---

## Features

- **Student portal** — browse questions by role and category, submit or update STAR answers, see instant AI coaching and mentor notes side-by-side
- **Mentor dashboard** — view every student's answers in a tabbed accordion, write feedback, track submission counts at a glance
- **AI question generator** — type any target role (e.g. "Product Manager") and generate 3 tailored behavioral questions in one click
- **Instant STAR analysis** — each answer is analyzed by an LLM on submission; feedback is stored and persists across sessions
- **Graceful AI degradation** — all features work without an API key; AI fields show a clear placeholder instead of breaking
- **Zero-dependency auth** — session-based login with hashed passwords via Werkzeug; no external auth service required

---

## Tech Stack

| Layer | Technology |
|---|---|
| Web framework | Flask 3 |
| Database | SQLite + SQLAlchemy 2 |
| AI | OpenAI Python SDK (`gpt-4o-mini`) |
| Frontend | Bootstrap 5.3 (CDN) |
| Container | Docker (`python:3.10-slim`) |

---

## Local Setup (no Docker)

```bash
# 1. Clone the repo
git clone <repo-url>
cd InterviewSync

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run (AI optional)
AI_API_KEY=sk-...  python app.py
# Omit AI_API_KEY to run without AI features
```

Open `http://localhost:5000` in your browser. The SQLite database and seed data are created automatically on first run.

---

## Docker Setup

```bash
# Build the image
docker build -t interviewsync .

# Run with AI enabled
docker run -p 5000:5000 \
  -e AI_API_KEY=sk-... \
  -e SECRET_KEY=change-me-in-prod \
  interviewsync

# Run without AI (still fully functional)
docker run -p 5000:5000 interviewsync
```

> **Data persistence:** By default the SQLite file lives inside the container and is lost on restart. Mount a host volume to persist it:
> ```bash
> docker run -p 5000:5000 -v $(pwd)/data:/app/instance interviewsync
> ```

---

## Demo Credentials

| Role | Username | Password |
|---|---|---|
| Mentor (admin) | `admin` | `admin123` |
| Student | `student1` | `student123` |

Both accounts and five starter questions are seeded automatically on first run.

---

## AWS Free Tier Architecture

InterviewSync is deliberately sized to run comfortably within the **AWS Free Tier** — no Kubernetes, no managed clusters, no surprise bills.

### Recommended deployment target

| Option | Details |
|---|---|
| **EC2 t2.micro** | 750 hrs/month free (first 12 months). Run Docker directly via `docker run` or a minimal `docker-compose.yml`. SQLite persists on a 20 GB EBS volume (also free tier). Simplest path to production. |
| **ECS Fargate** | Free for the first 12 months under AWS Free Tier (limited vCPU/memory hours). Use a single task definition with the InterviewSync image. Requires swapping SQLite for a small RDS Postgres instance if you need persistence across task replacements. |

### Why not Kubernetes?

Kubernetes (EKS, self-managed) adds real operational cost and complexity:

- EKS control plane alone costs ~$73/month before any nodes.
- Managing pods, services, ingress controllers, and node groups is disproportionate for a single-container app with modest traffic.
- A single Docker container on EC2 or a single ECS task achieves the same availability for a fraction of the effort and zero extra cost.

Kubernetes becomes the right choice when you have multiple services, need horizontal auto-scaling under heavy load, or operate a multi-team platform. InterviewSync is intentionally none of those things.

### Scaling path (when you outgrow Free Tier)

1. **EC2 t2.micro → t3.small** — handles ~50 concurrent users without app changes.
2. **SQLite → Postgres (RDS db.t3.micro)** — swap the `SQLALCHEMY_DATABASE_URI` env var; no model changes needed.
3. **Add Gunicorn** — replace `python app.py` with `gunicorn -w 4 app:app` to handle concurrent requests properly.
4. **Put CloudFront in front** — cache static assets and reduce EC2 load with minimal config.

None of these steps require rewriting the application.
