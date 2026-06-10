"""Billing and service integration tests."""

from datetime import datetime


def test_list_services(client, admin_token):
    r = client.get("/api/services", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    assert len(r.json()) >= 5


def test_create_service(client, admin_token):
    r = client.post("/api/services",
                    json={"name": "שירות בדיקה", "unit": "per_session",
                          "price_highschool": 100, "price_college": 150, "price_career": 200},
                    headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "שירות בדיקה"
    return data["id"]


def test_update_service(client, admin_token):
    create_r = client.post("/api/services",
                           json={"name": "שירות לעדכון", "unit": "monthly",
                                 "price_highschool": 200, "price_college": 300, "price_career": 400},
                           headers={"Authorization": f"Bearer {admin_token}"})
    sid = create_r.json()["id"]
    r = client.patch(f"/api/services/{sid}",
                     json={"price_highschool": 250},
                     headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    assert r.json()["price_highschool"] == 250


def test_delete_service(client, admin_token):
    create_r = client.post("/api/services",
                           json={"name": "שירות למחיקה", "unit": "fixed",
                                 "price_highschool": 50, "price_college": 50, "price_career": 50},
                           headers={"Authorization": f"Bearer {admin_token}"})
    sid = create_r.json()["id"]
    r = client.delete(f"/api/services/{sid}",
                      headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_set_and_get_student_billing(client, admin_token, student_id):
    svcs = client.get("/api/services",
                      headers={"Authorization": f"Bearer {admin_token}"}).json()
    svc_id = svcs[0]["id"]
    set_r = client.post(f"/api/students/{student_id}/billing",
                        json={"service_id": svc_id, "is_active": True},
                        headers={"Authorization": f"Bearer {admin_token}"})
    assert set_r.status_code == 200
    assert set_r.json()["ok"] is True

    get_r = client.get(f"/api/students/{student_id}/billing",
                       headers={"Authorization": f"Bearer {admin_token}"})
    assert get_r.status_code == 200
    assert get_r.json()["assigned"] is True
    assert get_r.json()["service_id"] == svc_id


def test_billing_dashboard(client, admin_token):
    month = datetime.utcnow().strftime("%Y-%m")
    r = client.get(f"/api/billing?month={month}",
                   headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    data = r.json()
    assert "records" in data
    assert "month_name" in data


def test_generate_billing(client, admin_token):
    month = datetime.utcnow().strftime("%Y-%m")
    r = client.post(f"/api/billing/generate/{month}",
                    headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["month"] == month


def test_generate_billing_invalid_month(client, admin_token):
    r = client.post("/api/billing/generate/not-a-month",
                    headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 400
