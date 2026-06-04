"""
Frontend BFF route tests.
Uses a mocked backend — no real backend needed for CI.
Run with: pytest frontend/tests/ -v
"""

import os
import pytest
from unittest.mock import patch, MagicMock

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("BACKEND_URL", "http://mock-backend:8000")

import sys  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import app as flask_app  # noqa: E402


@pytest.fixture
def client():
    flask_app.config["TESTING"] = True
    flask_app.config["SECRET_KEY"] = "test-secret-key"
    return flask_app.test_client()


def _mock_response(status_code, data):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = data
    return r


def test_login_page_renders(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert b"InterviewSync" in r.data


def test_login_page_contains_form(client):
    r = client.get("/login")
    assert b"username" in r.data
    assert b"password" in r.data


def test_unauthenticated_redirect_to_login(client):
    r = client.get("/")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_unauthenticated_admin_redirect(client):
    r = client.get("/admin")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


@patch("app._api")
def test_login_success_admin(mock_api, client):
    mock_api.return_value = _mock_response(200, {
        "token": "testtoken",
        "user_id": 1,
        "role": "admin",
        "name": "admin",
        "profile_complete": True,
    })
    r = client.post("/login",
                    data={"username": "admin", "password": "admin123"},
                    follow_redirects=False)
    assert r.status_code == 302
    assert "/admin" in r.headers["Location"]


@patch("app._api")
def test_login_bad_credentials(mock_api, client):
    mock_api.return_value = _mock_response(401, {"error": "שם משתמש או סיסמה שגויים"})
    r = client.post("/login",
                    data={"username": "admin", "password": "wrong"},
                    follow_redirects=True)
    assert r.status_code == 200
    assert "שגויים".encode() in r.data


@patch("app._api")
def test_login_student_no_profile_redirects_onboarding(mock_api, client):
    mock_api.return_value = _mock_response(200, {
        "token": "testtoken",
        "user_id": 2,
        "role": "student",
        "name": "student1",
        "profile_complete": False,
    })
    r = client.post("/login",
                    data={"username": "student1", "password": "student123"},
                    follow_redirects=False)
    assert r.status_code == 302
    assert "/onboarding" in r.headers["Location"]


def test_logout_clears_session(client):
    with client.session_transaction() as sess:
        sess["token"] = "some-token"
        sess["role"] = "admin"
    r = client.get("/logout", follow_redirects=False)
    assert r.status_code == 302
    with client.session_transaction() as sess:
        assert "token" not in sess
