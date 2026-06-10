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
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def admin_token(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return r.json()["token"]


@pytest.fixture(scope="session")
def student_token(client):
    r = client.post("/api/auth/login", json={"username": "student1", "password": "student123"})
    return r.json()["token"]


@pytest.fixture(scope="session")
def student_id(client, admin_token):
    r = client.get("/api/students", headers={"Authorization": f"Bearer {admin_token}"})
    return next(s["id"] for s in r.json() if s["username"] == "student1")
