"""
Backend integration tests.
Run with: pytest backend/tests/ -v
Requires DATABASE_URL to point to a PostgreSQL instance (set by CI).
"""

import os
import sys
import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key-long-enough-for-hs256-algorithm-32bytes")
os.environ.setdefault("UPLOAD_FOLDER", "/tmp/interviewsync_test_uploads")
os.environ["TESTING"] = "true"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402
from app import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    # TestClient enters the lifespan: creates tables, runs migrations, seeds data
    with TestClient(app) as c:
        yield c


# ── helpers ───────────────────────────────────────────────────────────────────

def _login(client, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    return r.json()["token"]


# ── tests ─────────────────────────────────────────────────────────────────────

def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_login_wrong_credentials(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "wrongpassword"})
    assert r.status_code == 401


def test_login_success_admin(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200
    data = r.json()
    assert "token" in data
    assert data["role"] == "admin"


def test_login_success_student(client):
    r = client.post("/api/auth/login", json={"username": "student1", "password": "student123"})
    assert r.status_code == 200
    assert r.json()["role"] == "student"


def test_protected_route_no_token(client):
    r = client.get("/api/students")
    assert r.status_code == 401


def test_protected_route_with_token(client):
    token = _login(client, "admin", "admin123")
    r = client.get("/api/students", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_task_bank_list(client):
    token = _login(client, "admin", "admin123")
    r = client.get("/api/tasks", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert len(r.json()) >= 10


def test_create_student(client):
    token = _login(client, "admin", "admin123")
    r = client.post("/api/students",
                    json={"username": "newstudent_ci", "password": "pass123"},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 201
    assert r.json()["username"] == "newstudent_ci"


def test_create_student_duplicate(client):
    token = _login(client, "admin", "admin123")
    r = client.post("/api/students",
                    json={"username": "student1", "password": "pass123"},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 409


def test_student_cannot_access_admin(client):
    token = _login(client, "student1", "student123")
    r = client.get("/api/students", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
