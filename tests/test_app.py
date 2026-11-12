import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import URLError
from unittest.mock import patch

from app import create_app


class JsonResponse:
    def __init__(self, value):
        self.body = json.dumps(value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class TeacherDashboardApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        environment = patch.dict(
            os.environ,
            {
                "SUPABASE_URL": "",
                "SUPABASE_ANON_KEY": "",
                "SUPABASE_SERVICE_ROLE_KEY": "",
                "GRADEWISE_DEMO_AUTH": "false",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        database_path = Path(self.temporary_directory.name) / "test.sqlite3"
        self.app = create_app(database_path)
        self.app.testing = True
        self.client = self.app.test_client()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_dashboard_returns_counts_from_seeded_records(self):
        response = self.client.get("/api/dashboard")

        self.assertEqual(response.status_code, 200)
        dashboard = response.get_json()
        self.assertEqual(dashboard["submissions"], 18)
        self.assertEqual(dashboard["assignment_count"], 6)
        self.assertEqual(dashboard["pending"], 2)
        self.assertEqual(
            dashboard["submissions"],
            dashboard["approved"] + dashboard["ai_evaluated"] + dashboard["pending"],
        )

    def test_student_page_is_served_and_uses_shared_dashboard_api(self):
        response = self.client.get("/student.html")

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        response.close()
        self.assertIn("Hello, Student", page)
        self.assertIn('api("/api/dashboard")', page)

    def test_index_links_to_both_dashboard_pages(self):
        response = self.client.get("/index.html")

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        response.close()
        self.assertIn('data-role="teacher" data-mode="login"', page)
        self.assertIn('data-role="teacher" data-mode="signup"', page)
        self.assertIn('data-role="student" data-mode="login"', page)
        self.assertIn('data-role="student" data-mode="signup"', page)
        self.assertNotIn("Dashboard Overview", page)

    def test_root_defaults_to_main_index(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        response.close()
        self.assertIn("Choose how you want to continue", page)
        self.assertNotIn("Dashboard Overview", page)

    def create_demo_client(self):
        with patch.dict(os.environ, {"GRADEWISE_DEMO_AUTH": "true"}):
            demo_app = create_app(self.app.config["DATABASE_PATH"])
        demo_app.testing = True
        return demo_app.test_client()

    def test_local_demo_teacher_login_opens_teacher_dashboard(self):
        client = self.create_demo_client()

        unauthenticated = client.get("/api/dashboard")
        self.assertEqual(unauthenticated.status_code, 401)

        login = client.post(
            "/api/auth/login",
            json={"role": "teacher", "email": "admin", "password": "123"},
        )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.get_json()["redirect_url"], "/teacher.html")
        self.assertTrue(login.get_json()["authenticated"])
        teacher_page = client.get("/teacher.html")
        self.assertEqual(teacher_page.status_code, 200)
        teacher_page.close()
        self.assertEqual(client.get("/api/auth/me").get_json()["role"], "teacher")

    def test_local_demo_student_login_is_read_only(self):
        client = self.create_demo_client()

        wrong_role = client.post(
            "/api/auth/login",
            json={"role": "teacher", "email": "student", "password": "123"},
        )
        self.assertEqual(wrong_role.status_code, 403)

        login = client.post(
            "/api/auth/login",
            json={"role": "student", "email": "student", "password": "123"},
        )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.get_json()["redirect_url"], "/student.html")
        student_page = client.get("/student.html")
        self.assertEqual(student_page.status_code, 200)
        student_page.close()
        denied = client.post(
            "/api/assignments",
            json={"title": "Blocked", "subject": "Maths", "total_marks": 20},
        )
        self.assertEqual(denied.status_code, 403)

    def test_index_stylesheet_is_served(self):
        response = self.client.get("/styles.css")

        self.assertEqual(response.status_code, 200)
        self.assertIn("body", response.get_data(as_text=True))
        response.close()

    def test_assignment_creation_persists_across_app_instances(self):
        response = self.client.post(
            "/api/assignments",
            json={
                "title": "Sorting Algorithms",
                "subject": "Computer Science",
                "due_date": "2027-01-15",
                "total_marks": 30,
            },
        )

        self.assertEqual(response.status_code, 201)
        created_id = response.get_json()["id"]
        second_app = create_app(self.app.config["DATABASE_PATH"])
        second_client = second_app.test_client()
        assignments = second_client.get("/api/assignments").get_json()

        self.assertTrue(any(item["id"] == created_id for item in assignments))
        self.assertEqual(second_client.get("/api/dashboard").get_json()["assignment_count"], 7)

    def test_assignment_rejects_invalid_marks(self):
        response = self.client.post(
            "/api/assignments",
            json={"title": "Quiz", "subject": "Maths", "total_marks": 0},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Total marks", response.get_json()["error"])

    def test_evaluations_can_be_filtered_and_approved(self):
        pending_response = self.client.get("/api/evaluations?status=pending&limit=1")
        pending = pending_response.get_json()["items"][0]

        invalid_approval = self.client.patch(
            f"/api/evaluations/{pending['id']}",
            json={"status": "approved"},
        )
        self.assertEqual(invalid_approval.status_code, 400)

        approval = self.client.patch(
            f"/api/evaluations/{pending['id']}",
            json={"status": "approved", "score": 18},
        )
        self.assertEqual(approval.status_code, 200)
        self.assertEqual(approval.get_json()["status"], "approved")

        dashboard = self.client.get("/api/dashboard").get_json()
        self.assertEqual(dashboard["pending"], 1)
        self.assertEqual(dashboard["approved"], 16)

    def test_performance_endpoint_supports_reporting_periods(self):
        for period in ("month", "3months", "semester"):
            with self.subTest(period=period):
                response = self.client.get(f"/api/performance?period={period}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["period"], period)
                self.assertTrue(response.get_json()["subjects"])

        invalid_response = self.client.get("/api/performance?period=year")
        self.assertEqual(invalid_response.status_code, 400)


class SupabaseApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "unused.sqlite3"
        with patch.dict(
            os.environ,
            {
                "SUPABASE_URL": "https://example.supabase.co",
                "SUPABASE_ANON_KEY": "test-anon-key",
                "SUPABASE_SERVICE_ROLE_KEY": "test-service-key",
            },
        ):
            self.app = create_app(self.database_path)
        self.app.testing = True
        self.client = self.app.test_client()
        self.client.set_cookie("gradewise_access_token", "test-access-token")

    @staticmethod
    def auth_user(role="teacher", user_id="teacher-user-id"):
        return JsonResponse(
            {
                "id": user_id,
                "email": f"{role}@example.com",
                "app_metadata": {"role": role},
                "user_metadata": {"full_name": role.title()},
            }
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_dashboard_uses_supabase_without_creating_sqlite_database(self):
        responses = [
            self.auth_user(),
            JsonResponse(
                [
                    {
                        "status": "approved",
                        "score": 18,
                        "assignments": {"total_marks": 20},
                    },
                    {
                        "status": "pending",
                        "score": None,
                        "assignments": {"total_marks": 20},
                    },
                ]
            ),
            JsonResponse([{"id": 1}]),
        ]
        with patch("app.urlopen", side_effect=responses):
            response = self.client.get("/api/dashboard")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["submissions"], 2)
        self.assertEqual(response.get_json()["class_average"], 90)
        self.assertFalse(self.database_path.exists())

    def test_supabase_connection_errors_are_reported_without_sqlite_fallback(self):
        with patch("app.urlopen", side_effect=URLError("offline")):
            response = self.client.get("/api/dashboard")

        self.assertEqual(response.status_code, 502)
        self.assertIn("Could not connect to Supabase", response.get_json()["error"])
        self.assertNotIn("test-service-key", response.get_data(as_text=True))
        self.assertFalse(self.database_path.exists())

    def test_supabase_configuration_requires_all_values(self):
        with patch.dict(
            os.environ,
            {
                "SUPABASE_URL": "https://example.supabase.co",
                "SUPABASE_ANON_KEY": "test-anon-key",
                "SUPABASE_SERVICE_ROLE_KEY": "",
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "Set SUPABASE_URL"):
                create_app(self.database_path)

    def test_assignment_creation_posts_to_supabase(self):
        created = {
            "id": 8,
            "title": "Sorting Algorithms",
            "subject": "Computer Science",
            "due_date": "2027-01-15",
            "total_marks": 30,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        with patch(
            "app.urlopen",
            side_effect=[self.auth_user(), JsonResponse([created])],
        ) as request_mock:
            response = self.client.post(
                "/api/assignments",
                json={
                    "title": "Sorting Algorithms",
                    "subject": "Computer Science",
                    "due_date": "2027-01-15",
                    "total_marks": 30,
                },
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["id"], 8)
        outbound_request = request_mock.call_args.args[0]
        self.assertEqual(outbound_request.get_method(), "POST")
        self.assertIn("/rest/v1/assignments", outbound_request.full_url)

    def test_evaluations_are_filtered_and_paginated_from_supabase(self):
        rows = [
            {
                "id": 4,
                "student": "Ananya Rao",
                "score": 18,
                "status": "approved",
                "submitted_at": "2026-01-01T00:00:00+00:00",
                "assignments": {
                    "title": "DBMS Normalization",
                    "subject": "DBMS",
                    "total_marks": 20,
                },
            },
            {
                "id": 5,
                "student": "Rahul Kumar",
                "score": 15,
                "status": "approved",
                "submitted_at": "2026-01-02T00:00:00+00:00",
                "assignments": {
                    "title": "Operating Systems",
                    "subject": "OS",
                    "total_marks": 20,
                },
            },
        ]
        with patch("app.urlopen", side_effect=[self.auth_user(), JsonResponse(rows)]):
            response = self.client.get("/api/evaluations?q=DBMS&limit=1&offset=0")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["items"][0]["id"], 4)

    def test_approval_and_performance_use_supabase_rows(self):
        now = datetime.now().astimezone()
        current_timestamp = now.isoformat()
        previous_timestamp = (now - timedelta(days=120)).isoformat()
        responses = [
            self.auth_user(),
            JsonResponse(
                [
                    {
                        "id": 7,
                        "status": "pending",
                        "score": None,
                        "assignments": {"total_marks": 20},
                    }
                ]
            ),
            JsonResponse([{"id": 7, "status": "approved", "score": 18}]),
            self.auth_user(),
            JsonResponse(
                [
                    {
                        "score": 18,
                        "submitted_at": current_timestamp,
                        "assignments": {"subject": "DBMS", "total_marks": 20},
                    },
                    {
                        "score": 15,
                        "submitted_at": previous_timestamp,
                        "assignments": {"subject": "DBMS", "total_marks": 20},
                    },
                ]
            ),
        ]
        with patch("app.urlopen", side_effect=responses):
            approval = self.client.patch(
                "/api/evaluations/7",
                json={"status": "approved", "score": 18},
            )
            performance = self.client.get("/api/performance?period=3months")

        self.assertEqual(approval.status_code, 200)
        self.assertEqual(approval.get_json()["status"], "approved")
        self.assertEqual(performance.status_code, 200)
        dbms = next(
            subject for subject in performance.get_json()["subjects"]
            if subject["subject"] == "DBMS"
        )
        self.assertEqual(dbms["current"], 90)
        self.assertEqual(dbms["previous"], 75)

    def test_student_data_is_scoped_and_student_writes_are_forbidden(self):
        student_id = "student-user-id"
        with patch(
            "app.urlopen",
            side_effect=[
                self.auth_user("student", student_id),
                JsonResponse([]),
            ],
        ) as request_mock:
            response = self.client.get("/api/evaluations")

        self.assertEqual(response.status_code, 200)
        evaluations_request = request_mock.call_args_list[1].args[0]
        self.assertIn(f"student_id=eq.{student_id}", evaluations_request.full_url)

        with patch(
            "app.urlopen",
            return_value=self.auth_user("student", student_id),
        ):
            denied = self.client.patch(
                "/api/evaluations/7",
                json={"status": "approved", "score": 18},
            )
        self.assertEqual(denied.status_code, 403)

    def test_student_account_cannot_open_teacher_page(self):
        with patch(
            "app.urlopen",
            return_value=self.auth_user("student", "student-user-id"),
        ):
            response = self.client.get("/teacher.html")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/student.html")

    def test_signup_sets_role_in_server_managed_app_metadata(self):
        responses = [
            JsonResponse({"user": {"id": "new-student-id"}}),
            JsonResponse(
                {
                    "id": "new-student-id",
                    "app_metadata": {"role": "student"},
                }
            ),
        ]
        with patch("app.urlopen", side_effect=responses) as request_mock:
            response = self.client.post(
                "/api/auth/signup",
                json={
                    "role": "student",
                    "full_name": "A Student",
                    "email": "student@example.com",
                    "password": "valid-password",
                },
            )

        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.get_json()["authenticated"])
        admin_request = request_mock.call_args_list[1].args[0]
        self.assertIn("/auth/v1/admin/users/new-student-id", admin_request.full_url)
        self.assertEqual(
            json.loads(admin_request.data)["app_metadata"]["role"],
            "student",
        )

    def test_login_sets_http_only_session_cookies_and_role_redirect(self):
        session = {
            "access_token": "student-access-token",
            "refresh_token": "student-refresh-token",
            "expires_in": 3600,
        }
        responses = [
            JsonResponse(session),
            self.auth_user("student", "student-user-id"),
        ]
        with patch("app.urlopen", side_effect=responses):
            response = self.client.post(
                "/api/auth/login",
                json={
                    "role": "student",
                    "email": "student@example.com",
                    "password": "valid-password",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["redirect_url"], "/student.html")
        set_cookies = response.headers.getlist("Set-Cookie")
        self.assertTrue(any("gradewise_access_token=" in value and "HttpOnly" in value for value in set_cookies))
        self.assertTrue(any("gradewise_refresh_token=" in value and "HttpOnly" in value for value in set_cookies))


if __name__ == "__main__":
    unittest.main()
