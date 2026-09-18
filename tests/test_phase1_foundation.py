"""Phase 1 foundation tests.

Run from repo root:  python -m unittest discover -s tests -v
Covers: registration/login, RBAC, hospital approval lifecycle, doctor + availability
management, and tenant isolation (Hospital A can never see Hospital B's doctors).
"""
from __future__ import annotations

import os
import tempfile
import unittest

# Point the app at a throwaway database BEFORE importing it.
_TMPDIR = tempfile.mkdtemp(prefix="hap-tests-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDIR}/test.db"
os.environ["APP_ENVIRONMENT"] = "test"

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.core.security import hash_password  # noqa: E402
from backend.app.database.base import SessionLocal  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.models import Hospital, User  # noqa: E402

client = TestClient(app)


def _make_platform_admin() -> None:
    db = SessionLocal()
    try:
        if not db.query(User).filter(User.email == "platform.admin@test.health").first():
            db.add(
                User(
                    email="platform.admin@test.health",
                    password_hash=hash_password("AdminPass1!"),
                    full_name="Platform Admin",
                    role="platform_admin",
                )
            )
            db.commit()
    finally:
        db.close()


def _register_hospital_admin(email: str, hospital_name: str, password: str = "Secret123!") -> dict:
    resp = client.post(
        "/auth/register-hospital-admin",
        json={
            "email": email,
            "password": password,
            "full_name": "Admin " + hospital_name,
            "hospital_name": hospital_name,
            "city": "Hyderabad",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestAuthAndRegistration(unittest.TestCase):
    def test_register_login_and_me(self) -> None:
        data = _register_hospital_admin("admin.one@test.health", "Test Hospital One")
        self.assertEqual(data["role"], "hospital_admin")
        self.assertTrue(data["access_token"])

        # Login returns a usable token
        login = client.post(
            "/auth/login", json={"email": "admin.one@test.health", "password": "Secret123!"}
        )
        self.assertEqual(login.status_code, 200, login.text)
        me = client.get("/auth/me", headers=_auth(login.json()["access_token"]))
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["role"], "hospital_admin")
        self.assertEqual(me.json()["email"], "admin.one@test.health")

    def test_patient_registration_and_login(self) -> None:
        resp = client.post(
            "/auth/register-patient",
            json={"email": "patient.one@test.health", "password": "Secret123!", "full_name": "Pat One"},
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(resp.json()["role"], "patient")
        login = client.post(
            "/auth/login", json={"email": "patient.one@test.health", "password": "Secret123!"}
        )
        self.assertEqual(login.status_code, 200)

    def test_duplicate_email_rejected(self) -> None:
        _register_hospital_admin("dupe.admin@test.health", "Dupe Hospital")
        resp = client.post(
            "/auth/register-hospital-admin",
            json={
                "email": "dupe.admin@test.health",
                "password": "Secret123!",
                "full_name": "Other",
                "hospital_name": "Other Hospital",
            },
        )
        self.assertEqual(resp.status_code, 409)

    def test_wrong_password_401(self) -> None:
        _register_hospital_admin("wrongpw.admin@test.health", "Wrong PW Hospital")
        resp = client.post(
            "/auth/login", json={"email": "wrongpw.admin@test.health", "password": "nope-nope"}
        )
        self.assertEqual(resp.status_code, 401)

    def test_invalid_email_422(self) -> None:
        resp = client.post(
            "/auth/register-patient",
            json={"email": "not-an-email", "password": "Secret123!", "full_name": "X"},
        )
        self.assertEqual(resp.status_code, 422)

    def test_unauthenticated_requests_rejected(self) -> None:
        self.assertEqual(client.get("/hospital/doctors").status_code, 401)
        self.assertEqual(client.get("/platform/hospitals").status_code, 401)
        self.assertEqual(client.get("/me/profile").status_code, 401)

    def test_short_password_rejected(self) -> None:
        resp = client.post(
            "/auth/register-patient",
            json={"email": "shortpw@test.health", "password": "short", "full_name": "X"},
        )
        self.assertEqual(resp.status_code, 422)


class TestHospitalLifecycleAndRBAC(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _make_platform_admin()
        login = client.post(
            "/auth/login", json={"email": "platform.admin@test.health", "password": "AdminPass1!"}
        )
        cls.platform_token = login.json()["access_token"]
        data = _register_hospital_admin("lifecycle.admin@test.health", "Lifecycle Hospital")
        cls.hospital_admin_token = data["access_token"]

    def test_hospital_starts_submitted_and_cannot_add_doctors(self) -> None:
        me = client.get("/hospital/me", headers=_auth(self.hospital_admin_token))
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["status"], "submitted")

        resp = client.post(
            "/hospital/doctors",
            headers=_auth(self.hospital_admin_token),
            json={"full_name": "Dr. Early", "specialty": "orthopedics"},
        )
        self.assertEqual(resp.status_code, 409)  # requires approval

    def test_non_platform_admin_cannot_review_hospitals(self) -> None:
        resp = client.get("/platform/hospitals", headers=_auth(self.hospital_admin_token))
        self.assertEqual(resp.status_code, 403)

    def test_platform_admin_approves_and_doctor_creation_unlocks(self) -> None:
        hospitals = client.get("/platform/hospitals", headers=_auth(self.platform_token)).json()
        target = next(h for h in hospitals if h["name"] == "Lifecycle Hospital")

        approved = client.post(
            f"/platform/hospitals/{target['id']}/review?action=approve",
            headers=_auth(self.platform_token),
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["status"], "approved")

        resp = client.post(
            "/hospital/doctors",
            headers=_auth(self.hospital_admin_token),
            json={
                "full_name": "Dr. Approved",
                "specialty": "orthopedics",
                "consultation_minutes": 30,
                "appointment_types": ["in_person"],
            },
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        self.assertEqual(resp.json()["status"], "active")


class TestTenantIsolationAndDirectory(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _make_platform_admin()
        plat = client.post(
            "/auth/login", json={"email": "platform.admin@test.health", "password": "AdminPass1!"}
        )
        cls.platform_token = plat.json()["access_token"]

        cls.hosp_a = _register_hospital_admin("iso.admin.a@test.health", "Iso Hospital A")
        cls.hosp_b = _register_hospital_admin("iso.admin.b@test.health", "Iso Hospital B")
        for token in (cls.hosp_a["access_token"], cls.hosp_b["access_token"]):
            hospitals = client.get("/platform/hospitals", headers=_auth(cls.platform_token)).json()
            by_name = {h["name"]: h["id"] for h in hospitals}
            name = "Iso Hospital A" if token == cls.hosp_a["access_token"] else "Iso Hospital B"
            client.post(
                f"/platform/hospitals/{by_name[name]}/review?action=approve",
                headers=_auth(cls.platform_token),
            )

        # Hospital A creates a doctor + availability
        doctor = client.post(
            "/hospital/doctors",
            headers=_auth(cls.hosp_a["access_token"]),
            json={"full_name": "Dr. Isolated", "specialty": "orthopedics"},
        )
        assert doctor.status_code == 201, doctor.text
        cls.doctor_a_id = doctor.json()["id"]
        avail = client.post(
            f"/hospital/doctors/{cls.doctor_a_id}/availability",
            headers=_auth(cls.hosp_a["access_token"]),
            json={"doctor_id": cls.doctor_a_id, "weekday": 0, "start_time": "09:00", "end_time": "12:00"},
        )
        assert avail.status_code == 201, avail.text

    def test_hospital_b_cannot_see_hospital_a_doctor(self) -> None:
        # Not in B's list
        doctors_b = client.get("/hospital/doctors", headers=_auth(self.hosp_b["access_token"])).json()
        self.assertEqual(doctors_b, [])

        # Direct access is 404 (existence hidden)
        resp = client.get(
            f"/hospital/doctors/{self.doctor_a_id}/availability",
            headers=_auth(self.hosp_b["access_token"]),
        )
        self.assertEqual(resp.status_code, 404)

    def test_hospital_b_cannot_modify_hospital_a_doctor(self) -> None:
        resp = client.patch(
            f"/hospital/doctors/{self.doctor_a_id}",
            headers=_auth(self.hosp_b["access_token"]),
            json={"status": "inactive"},
        )
        self.assertIn(resp.status_code, (403, 404))

        block = client.post(
            f"/hospital/doctors/{self.doctor_a_id}/blocked",
            headers=_auth(self.hosp_b["access_token"]),
            json={
                "doctor_id": self.doctor_a_id,
                "kind": "leave",
                "start_at": "2026-09-20T09:00:00",
                "end_at": "2026-09-20T17:00:00",
            },
        )
        self.assertIn(block.status_code, (403, 404))

    def test_availability_validation(self) -> None:
        resp = client.post(
            f"/hospital/doctors/{self.doctor_a_id}/availability",
            headers=_auth(self.hosp_a["access_token"]),
            json={"doctor_id": self.doctor_a_id, "weekday": 2, "start_time": "15:00", "end_time": "09:00"},
        )
        self.assertEqual(resp.status_code, 422)

        resp = client.post(
            f"/hospital/doctors/{self.doctor_a_id}/availability",
            headers=_auth(self.hosp_a["access_token"]),
            json={"doctor_id": self.doctor_a_id, "weekday": 7, "start_time": "09:00", "end_time": "10:00"},
        )
        self.assertEqual(resp.status_code, 422)

    def test_doctor_update_and_status_filter(self) -> None:
        resp = client.patch(
            f"/hospital/doctors/{self.doctor_a_id}",
            headers=_auth(self.hosp_a["access_token"]),
            json={"status": "inactive"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "inactive")
        # restore
        client.patch(
            f"/hospital/doctors/{self.doctor_a_id}",
            headers=_auth(self.hosp_a["access_token"]),
            json={"status": "active"},
        )

    def test_blocked_period_crud(self) -> None:
        created = client.post(
            f"/hospital/doctors/{self.doctor_a_id}/blocked",
            headers=_auth(self.hosp_a["access_token"]),
            json={
                "doctor_id": self.doctor_a_id,
                "kind": "blocked",
                "start_at": "2026-09-21T09:00:00",
                "end_at": "2026-09-21T10:00:00",
                "reason": "equipment maintenance",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        listed = client.get(
            f"/hospital/doctors/{self.doctor_a_id}/blocked",
            headers=_auth(self.hosp_a["access_token"]),
        )
        self.assertEqual(listed.status_code, 200)
        self.assertTrue(any(b["kind"] == "blocked" for b in listed.json()))

    def test_audit_trail_records_actions(self) -> None:
        audit = client.get("/platform/audit?limit=50", headers=_auth(self.platform_token))
        self.assertEqual(audit.status_code, 200)
        actions = [a["action"] for a in audit.json()]
        self.assertIn("hospital.approved", actions)
        self.assertIn("doctor.created", actions)


if __name__ == "__main__":
    unittest.main()
