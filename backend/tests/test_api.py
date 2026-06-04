"""
Backend integration tests.
Run with: pytest backend/tests/ -v
Requires: DB_HOST, DB_USER, etc. OR uses SQLite fallback for CI.
"""

import os
import pytest

TEST_SECRET = "test-secret-key-long-enough-for-hs256-algorithm-32bytes"
os.environ["DATABASE_URL"]  = "sqlite:///:memory:"
os.environ["SECRET_KEY"]    = TEST_SECRET
os.environ["UPLOAD_FOLDER"] = "/tmp/interviewsync_test_uploads"

import sys  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as _app_module  # noqa: E402
_app_module.SECRET_KEY = TEST_SECRET
from app import app as flask_app, db, seed_db  # noqa: E402


@pytest.fixture
def app():
    flask_app.config["TESTING"] = True
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    with flask_app.app_context():
        db.create_all()
        seed_db()
        yield flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    data = r.get_json()
    assert data["status"] == "ok"


def test_login_wrong_credentials(client):
    r = client.post("/api/auth/login",
                    json={"username": "admin", "password": "wrongpassword"})
    assert r.status_code == 401


def test_login_success_admin(client):
    r = client.post("/api/auth/login",
                    json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200
    data = r.get_json()
    assert "token" in data
    assert data["role"] == "admin"


def test_login_success_student(client):
    r = client.post("/api/auth/login",
                    json={"username": "student1", "password": "student123"})
    assert r.status_code == 200
    data = r.get_json()
    assert data["role"] == "student"


def test_protected_route_no_token(client):
    r = client.get("/api/students")
    assert r.status_code == 401


def test_protected_route_with_token(client):
    login = client.post("/api/auth/login",
                        json={"username": "admin", "password": "admin123"})
    token = login.get_json()["token"]
    r = client.get("/api/students",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert isinstance(r.get_json(), list)


def test_task_bank_list(client):
    login = client.post("/api/auth/login",
                        json={"username": "admin", "password": "admin123"})
    token = login.get_json()["token"]
    r = client.get("/api/tasks",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    tasks = r.get_json()
    assert len(tasks) >= 10  # seed creates 10 tasks


def test_create_student(client):
    login = client.post("/api/auth/login",
                        json={"username": "admin", "password": "admin123"})
    token = login.get_json()["token"]
    r = client.post("/api/students",
                    json={"username": "newstudent", "password": "pass123"},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 201
    assert r.get_json()["username"] == "newstudent"


def test_create_student_duplicate(client):
    login = client.post("/api/auth/login",
                        json={"username": "admin", "password": "admin123"})
    token = login.get_json()["token"]
    # student1 already exists from seed
    r = client.post("/api/students",
                    json={"username": "student1", "password": "pass123"},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 409


def test_student_cannot_access_admin(client):
    login = client.post("/api/auth/login",
                        json={"username": "student1", "password": "student123"})
    token = login.get_json()["token"]
    r = client.get("/api/students",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
