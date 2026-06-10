"""
Backend integration tests — auth, students, tasks.
Fixtures provided by conftest.py.
"""


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


def test_protected_route_with_token(client, admin_token):
    r = client.get("/api/students", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_task_bank_list(client, admin_token):
    r = client.get("/api/tasks", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    assert len(r.json()) >= 10


def test_create_student(client, admin_token):
    r = client.post("/api/students",
                    json={"username": "newstudent_ci", "password": "pass123"},
                    headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 201
    assert r.json()["username"] == "newstudent_ci"


def test_create_student_duplicate(client, admin_token):
    r = client.post("/api/students",
                    json={"username": "student1", "password": "pass123"},
                    headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 409


def test_student_cannot_access_admin(client, student_token):
    r = client.get("/api/students", headers={"Authorization": f"Bearer {student_token}"})
    assert r.status_code == 403


def test_assign_task_to_student(client, admin_token, student_id):
    tasks = client.get("/api/tasks",
                       headers={"Authorization": f"Bearer {admin_token}"}).json()
    task_id = tasks[0]["id"]
    r = client.post(f"/api/students/{student_id}/assignments",
                    json={"task_ids": [task_id]},
                    headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code in (200, 201)


def test_student_sees_own_tasks(client, student_token):
    r = client.get("/api/my/tasks", headers={"Authorization": f"Bearer {student_token}"})
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_update_student_profile(client, admin_token, student_id):
    r = client.patch(f"/api/students/{student_id}/profile",
                     json={"career_goals": "להיות מהנדס תוכנה"},
                     headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200


def test_student_profile_self_update(client, student_token):
    r = client.patch("/api/my/profile",
                     json={"career_goals": "לעבוד בהייטק"},
                     headers={"Authorization": f"Bearer {student_token}"})
    assert r.status_code == 200
