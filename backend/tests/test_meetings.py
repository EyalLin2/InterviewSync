"""Meeting lifecycle integration tests."""

from datetime import datetime, timedelta


def _future(days=7):
    return (datetime.utcnow() + timedelta(days=days)).isoformat()


def test_create_meeting(client, admin_token, student_id):
    r = client.post("/api/meetings",
                    json={"student_id": student_id, "scheduled_at": _future()},
                    headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 201
    assert "id" in r.json()


def test_create_meeting_missing_fields(client, admin_token):
    r = client.post("/api/meetings",
                    json={"scheduled_at": _future()},
                    headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 400


def test_meeting_list(client, admin_token):
    r = client.get("/api/meetings", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_student_sees_own_meetings(client, student_token):
    r = client.get("/api/my/meetings", headers={"Authorization": f"Bearer {student_token}"})
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_cancel_meeting(client, admin_token, student_id):
    create_r = client.post("/api/meetings",
                           json={"student_id": student_id, "scheduled_at": _future(3)},
                           headers={"Authorization": f"Bearer {admin_token}"})
    mid = create_r.json()["id"]
    r = client.patch(f"/api/meetings/{mid}",
                     json={"action": "cancel"},
                     headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_confirm_meeting_invalid_token(client, admin_token, student_id):
    create_r = client.post("/api/meetings",
                           json={"student_id": student_id, "scheduled_at": _future(5)},
                           headers={"Authorization": f"Bearer {admin_token}"})
    mid = create_r.json()["id"]
    r = client.get(f"/api/meetings/{mid}/confirm?token=badtoken")
    assert r.status_code == 403


def test_mark_meeting_completed_with_outcome(client, admin_token, student_id):
    create_r = client.post("/api/meetings",
                           json={"student_id": student_id, "scheduled_at": _future(1)},
                           headers={"Authorization": f"Bearer {admin_token}"})
    mid = create_r.json()["id"]
    r = client.patch(f"/api/meetings/{mid}",
                     json={"action": "mark_completed",
                           "outcome_notes": "עבדנו על קורות חיים",
                           "action_items": "לסיים את הפרופיל"},
                     headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_unknown_meeting_action(client, admin_token, student_id):
    create_r = client.post("/api/meetings",
                           json={"student_id": student_id, "scheduled_at": _future(2)},
                           headers={"Authorization": f"Bearer {admin_token}"})
    mid = create_r.json()["id"]
    r = client.patch(f"/api/meetings/{mid}",
                     json={"action": "does_not_exist"},
                     headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 400
