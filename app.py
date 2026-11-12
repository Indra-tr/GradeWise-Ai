import hmac
import json
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from flask import (
    Flask,
    g,
    jsonify,
    redirect,
    request,
    send_from_directory,
    session as flask_session,
)
from dotenv import load_dotenv
from werkzeug.utils import secure_filename

from backend.google_vision_ocr import GoogleVisionError, extract_assignment_pdf
from backend.evaluator import evaluate_answer
from backend.scoring_engine import score_evaluation


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DEFAULT_DATABASE = BASE_DIR / "data" / "gradewise.sqlite3"
STATUSES = {"approved", "ai_evaluated", "pending"}
CHART_SUBJECTS = ["DBMS", "OS", "Maths", "Python", "AI/ML", "Java"]
SUPABASE_PAGE_SIZE = 1000


class SupabaseError(Exception):
    pass


class SupabaseAuthError(SupabaseError):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


def supabase_auth_request(
    base_url, api_key, path, method="GET", payload=None, access_token=None
):
    url = f"{base_url.rstrip('/')}/auth/v1/{path.lstrip('/')}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "apikey": api_key,
        "Authorization": f"Bearer {access_token or api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    outbound_request = Request(url, data=body, headers=headers, method=method)

    try:
        with urlopen(outbound_request, timeout=15) as response:
            response_body = response.read()
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        try:
            response_data = json.loads(detail)
        except json.JSONDecodeError:
            response_data = {}
        if isinstance(response_data, dict):
            detail = (
                response_data.get("msg")
                or response_data.get("message")
                or response_data.get("error_description")
                or detail
            )
        if error.code in {400, 401, 403, 422}:
            raise SupabaseAuthError(detail or "Supabase authentication failed.", error.code) from error
        raise SupabaseError(
            f"Supabase Auth request failed ({error.code}): {detail}"
        ) from error
    except (URLError, TimeoutError, OSError) as error:
        raise SupabaseError(f"Could not connect to Supabase Auth: {error}") from error

    if not response_body:
        return {}
    try:
        result = json.loads(response_body)
    except json.JSONDecodeError as error:
        raise SupabaseError("Supabase Auth returned invalid JSON.") from error
    if not isinstance(result, dict):
        raise SupabaseError("Supabase Auth returned an unexpected response format.")
    return result


def supabase_request(base_url, api_key, table, method="GET", params=None, payload=None):
    url = f"{base_url.rstrip('/')}/rest/v1/{table}"
    if params:
        url = f"{url}?{urlencode(params, safe=',!()*:')}"

    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "apikey": api_key,
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Prefer": "return=representation",
    }
    outbound_request = Request(url, data=body, headers=headers, method=method)

    try:
        with urlopen(outbound_request, timeout=15) as response:
            response_body = response.read()
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise SupabaseError(
            f"Supabase REST request failed ({error.code}): {detail}"
        ) from error
    except (URLError, TimeoutError, OSError) as error:
        raise SupabaseError(f"Could not connect to Supabase: {error}") from error

    if not response_body:
        return []
    try:
        result = json.loads(response_body)
    except json.JSONDecodeError as error:
        raise SupabaseError("Supabase returned an invalid JSON response.") from error
    if not isinstance(result, list):
        raise SupabaseError("Supabase returned an unexpected response format.")
    return result


def supabase_storage_request(
    base_url,
    api_key,
    endpoint,
    method="POST",
    payload=None,
    raw_body=None,
    content_type="application/json",
    extra_headers=None,
):
    url = f"{base_url.rstrip('/')}/storage/v1/{endpoint.lstrip('/')}"
    body = raw_body
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    headers = {
        "apikey": api_key,
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    if body is not None:
        headers["Content-Type"] = content_type
    if extra_headers:
        headers.update(extra_headers)
    outbound_request = Request(url, data=body, headers=headers, method=method)

    try:
        with urlopen(outbound_request, timeout=60) as response:
            response_body = response.read()
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        try:
            response_data = json.loads(detail)
            if isinstance(response_data, dict):
                detail = response_data.get("message") or response_data.get("error") or detail
        except json.JSONDecodeError:
            pass
        raise SupabaseError(f"Supabase Storage request failed ({error.code}): {detail}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise SupabaseError(f"Could not connect to Supabase Storage: {error}") from error

    if not response_body:
        return {}
    try:
        return json.loads(response_body)
    except json.JSONDecodeError as error:
        raise SupabaseError("Supabase Storage returned invalid JSON.") from error


def storage_url_path(value):
    return "/".join(quote(part, safe="") for part in value.split("/"))


def assignment_payload(row):
    result = {key: row[key] for key in row.keys()}
    ocr_text = result.pop("ocr_text", "") or ""
    result["ocr_excerpt"] = " ".join(ocr_text.split())[:220]
    result["has_file"] = bool(result.get("storage_path"))
    result["file_url"] = f"/api/assignments/{result['id']}/file" if result["has_file"] else None
    result.pop("storage_path", None)
    result.pop("created_by", None)
    return result


def supabase_select_all(base_url, api_key, table, params):
    rows = []
    offset = 0
    while True:
        page = supabase_request(
            base_url,
            api_key,
            table,
            params={**params, "limit": str(SUPABASE_PAGE_SIZE), "offset": str(offset)},
        )
        rows.extend(page)
        if len(page) < SUPABASE_PAGE_SIZE:
            return rows
        offset += SUPABASE_PAGE_SIZE


def relation_record(row, relation_name="assignments"):
    relation = row.get(relation_name) or {}
    if isinstance(relation, list):
        return relation[0] if relation else {}
    return relation


def parse_datetime(value):
    if value.endswith("Z"):
        value = f"{value[:-1]}+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def create_app(database_path=None):
    app = Flask(__name__)
    app.config["DATABASE_PATH"] = str(
        database_path or os.environ.get("GRADEWISE_DATABASE", DEFAULT_DATABASE)
    )
    max_assignment_bytes = int(os.environ.get("MAX_ASSIGNMENT_UPLOAD_MB", "20")) * 1024 * 1024
    app.config["MAX_ASSIGNMENT_UPLOAD_BYTES"] = max_assignment_bytes
    app.config["MAX_CONTENT_LENGTH"] = max_assignment_bytes + 1024 * 1024
    app.config["ASSIGNMENT_UPLOAD_DIR"] = str(BASE_DIR / "data" / "assignment_uploads")
    app.config["SUPABASE_ASSIGNMENTS_BUCKET"] = (
        os.environ.get("SUPABASE_ASSIGNMENTS_BUCKET", "assignments").strip()
        or "assignments"
    )
    app.config["GOOGLE_VISION_API_KEY"] = os.environ.get("GOOGLE_VISION_API_KEY", "").strip()
    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    supabase_anon_key = os.environ.get("SUPABASE_ANON_KEY", "").strip()
    supabase_configured = bool(supabase_url or supabase_key or supabase_anon_key)
    if supabase_configured and not (supabase_url and supabase_key and supabase_anon_key):
        raise RuntimeError(
            "Set SUPABASE_URL, SUPABASE_ANON_KEY, and "
            "SUPABASE_SERVICE_ROLE_KEY together, or leave all three unset."
        )
    demo_auth_value = os.environ.get("GRADEWISE_DEMO_AUTH", "true").strip().lower()
    if demo_auth_value not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
        raise RuntimeError("GRADEWISE_DEMO_AUTH must be set to a boolean value.")
    local_demo_auth_enabled = (
        not supabase_configured and demo_auth_value in {"1", "true", "yes", "on"}
    )
    app.config["SUPABASE_ENABLED"] = supabase_configured
    app.config["LOCAL_DEMO_AUTH_ENABLED"] = local_demo_auth_enabled
    app.config["SUPABASE_URL"] = supabase_url
    app.config["SUPABASE_SERVICE_ROLE_KEY"] = supabase_key
    app.config["SUPABASE_ANON_KEY"] = supabase_anon_key
    if local_demo_auth_enabled:
        app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(32)
        app.config["SESSION_COOKIE_HTTPONLY"] = True
        app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    def connect_database():
        path = app.config["DATABASE_PATH"]
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize_database():
        with closing(connect_database()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    due_date TEXT,
                    total_marks INTEGER NOT NULL CHECK (total_marks > 0),
                    created_at TEXT NOT NULL,
                    original_filename TEXT,
                    storage_path TEXT,
                    mime_type TEXT,
                    ocr_text TEXT NOT NULL DEFAULT '',
                    ocr_method TEXT NOT NULL DEFAULT 'none',
                    file_size INTEGER,
                    created_by TEXT
                );

                CREATE TABLE IF NOT EXISTS evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    assignment_id INTEGER NOT NULL REFERENCES assignments(id),
                    student TEXT NOT NULL,
                    score REAL,
                    status TEXT NOT NULL CHECK (
                        status IN ('approved', 'ai_evaluated', 'pending')
                    ),
                    submitted_at TEXT NOT NULL,
                    CHECK (score IS NULL OR score >= 0)
                );

                CREATE INDEX IF NOT EXISTS idx_evaluations_submitted_at
                    ON evaluations(submitted_at);
                CREATE INDEX IF NOT EXISTS idx_evaluations_status
                    ON evaluations(status);

                CREATE TABLE IF NOT EXISTS submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    assignment_id INTEGER NOT NULL REFERENCES assignments(id),
                    student_id TEXT NOT NULL,
                    student_name TEXT NOT NULL,
                    answer_text TEXT NOT NULL DEFAULT '',
                    submitted_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_submissions_assignment
                    ON submissions(assignment_id);
                """
            )
            assignment_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(assignments)")
            }
            assignment_migrations = {
                "original_filename": "TEXT",
                "storage_path": "TEXT",
                "mime_type": "TEXT",
                "ocr_text": "TEXT NOT NULL DEFAULT ''",
                "ocr_method": "TEXT NOT NULL DEFAULT 'none'",
                "file_size": "INTEGER",
                "created_by": "TEXT",
                "key_answer_text": "TEXT NOT NULL DEFAULT ''",
            }
            for column, definition in assignment_migrations.items():
                if column not in assignment_columns:
                    connection.execute(
                        f"ALTER TABLE assignments ADD COLUMN {column} {definition}"
                    )
            eval_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(evaluations)")
            }
            if "ai_feedback" not in eval_columns:
                connection.execute("ALTER TABLE evaluations ADD COLUMN ai_feedback TEXT")
            assignment_count = connection.execute(
                "SELECT COUNT(*) FROM assignments"
            ).fetchone()[0]
            if assignment_count == 0:
                seed_demo_data(connection)
            connection.commit()

    def seed_demo_data(connection):
        created_at = datetime.now().replace(microsecond=0).isoformat()
        samples = [
            ("DBMS - Normalization", "DBMS", "Ananya Rao", 18, "approved"),
            ("OS - Process Scheduling", "OS", "Rahul Kumar", 16, "approved"),
            ("AI/ML - Linear Regression", "AI/ML", "Sneha M", None, "pending"),
            ("Python - File Handling", "Python", "Arjun P", 17, "ai_evaluated"),
            ("Maths - Probability", "Maths", "Isha Patel", 15, "approved"),
            ("Java - Collections", "Java", "Karan Shah", None, "pending"),
        ]
        assignment_ids = {}
        for title, subject, _, _, _ in samples:
            cursor = connection.execute(
                """
                INSERT INTO assignments (title, subject, due_date, total_marks, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (title, subject, None, 20, created_at),
            )
            assignment_ids[subject] = cursor.lastrowid

        now = datetime.now().replace(microsecond=0)
        current_offsets = [1, 2, 26, 27, 28, 29]
        previous_scores = [15, 14, 13, 16, 15, 14]
        older_scores = [14, 12, 12, 15, 14, 13]
        older_students = ["Maya Singh", "Dev Patel", "Nina Das", "Kabir Rao", "Tara Shah", "Rohan Mehta"]
        for index, (title, subject, student, score, status) in enumerate(samples):
            submitted_at = now - timedelta(hours=current_offsets[index])
            connection.execute(
                """
                INSERT INTO evaluations
                    (assignment_id, student, score, status, submitted_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (assignment_ids[subject], student, score, status, submitted_at.isoformat()),
            )

            for days_ago, historical_score, historical_student in (
                (40, previous_scores[index], older_students[index]),
                (220, older_scores[index], f"Student {index + 1:02d}"),
            ):
                connection.execute(
                    """
                    INSERT INTO evaluations
                        (assignment_id, student, score, status, submitted_at)
                    VALUES (?, ?, ?, 'approved', ?)
                    """,
                    (
                        assignment_ids[subject],
                        historical_student,
                        historical_score,
                        (now - timedelta(days=days_ago)).isoformat(),
                    ),
                )

    if not app.config["SUPABASE_ENABLED"]:
        initialize_database()


    def select_supabase_rows(table, params):
        return supabase_select_all(
            app.config["SUPABASE_URL"],
            app.config["SUPABASE_SERVICE_ROLE_KEY"],
            table,
            params,
        )

    def request_supabase(table, method="GET", params=None, payload=None):
        return supabase_request(
            app.config["SUPABASE_URL"],
            app.config["SUPABASE_SERVICE_ROLE_KEY"],
            table,
            method=method,
            params=params,
            payload=payload,
        )

    def request_supabase_auth(
        path, method="GET", payload=None, access_token=None, admin=False
    ):
        api_key = (
            app.config["SUPABASE_SERVICE_ROLE_KEY"]
            if admin
            else app.config["SUPABASE_ANON_KEY"]
        )
        return supabase_auth_request(
            app.config["SUPABASE_URL"],
            api_key,
            path,
            method=method,
            payload=payload,
            access_token=access_token,
        )

    def set_auth_cookies(response, session):
        secure = request.is_secure
        access_token = session.get("access_token")
        if access_token:
            response.set_cookie(
                "gradewise_access_token",
                access_token,
                max_age=int(session.get("expires_in") or 3600),
                httponly=True,
                secure=secure,
                samesite="Lax",
                path="/",
            )
        refresh_token = session.get("refresh_token")
        if refresh_token:
            response.set_cookie(
                "gradewise_refresh_token",
                refresh_token,
                max_age=60 * 60 * 24 * 30,
                httponly=True,
                secure=secure,
                samesite="Lax",
                path="/api/auth",
            )
        return response

    def clear_auth_cookies(response):
        response.delete_cookie(
            "gradewise_access_token",
            path="/",
            httponly=True,
            secure=request.is_secure,
            samesite="Lax",
        )
        response.delete_cookie(
            "gradewise_refresh_token",
            path="/api/auth",
            httponly=True,
            secure=request.is_secure,
            samesite="Lax",
        )
        return response

    def auth_redirect(role):
        return "/teacher.html" if role == "teacher" else "/student.html"

    def apply_student_scope(params):
        if app.config["SUPABASE_ENABLED"]:
            user = g.current_user
            if (user.get("app_metadata") or {}).get("role") == "student":
                params["student_id"] = f"eq.{user['id']}"
        return params

    @app.before_request
    def authenticate_request():
        if app.config["SUPABASE_ENABLED"]:
            if request.path in {
                "/",
                "/index.html",
                "/styles.css",
                "/api/auth/signup",
                "/api/auth/login",
                "/api/auth/refresh",
                "/api/auth/logout",
            }:
                return None
            if not (
                request.path in {"/teacher.html", "/student.html"}
                or request.path.startswith("/api/")
            ):
                return None

            access_token = request.cookies.get("gradewise_access_token")
            if not access_token:
                if request.path.startswith("/api/"):
                    return jsonify(error="Please log in to continue."), 401
                return redirect("/")
            try:
                user = request_supabase_auth("user", access_token=access_token)
            except SupabaseAuthError as error:
                if error.status_code in {400, 401, 403}:
                    if request.path.startswith("/api/"):
                        return jsonify(error="Your session is invalid or expired."), 401
                    return redirect("/")
                raise

            role = (user.get("app_metadata") or {}).get("role")
            if role not in {"teacher", "student"}:
                return jsonify(error="This account does not have a valid dashboard role."), 403
            g.current_user = user

            if request.path == "/teacher.html" and role != "teacher":
                return redirect(auth_redirect(role))
            if request.path == "/student.html" and role != "student":
                return redirect(auth_redirect(role))
            if (
                request.path.startswith("/api/")
                and request.method in {"POST", "PATCH", "PUT", "DELETE"}
                and role != "teacher"
                and request.path != "/api/submissions"
            ):
                return jsonify(error="Only teachers can change assignments or grades."), 403
            return None

        if not app.config["LOCAL_DEMO_AUTH_ENABLED"]:
            return None
        if request.path in {
            "/",
            "/index.html",
            "/styles.css",
            "/api/auth/signup",
            "/api/auth/login",
            "/api/auth/refresh",
            "/api/auth/logout",
            "/api/auth/me",
        }:
            return None
        if not (
            request.path in {"/teacher.html", "/student.html"}
            or request.path.startswith("/api/")
        ):
            return None

        user = flask_session.get("local_demo_user")
        if not isinstance(user, dict) or user.get("role") not in {"teacher", "student"}:
            if request.path.startswith("/api/"):
                return jsonify(error="Please log in to continue."), 401
            return redirect("/")
        role = user["role"]
        g.current_user = {
            "email": user["email"],
            "user_metadata": {"full_name": user["full_name"]},
            "app_metadata": {"role": role},
        }

        if request.path == "/teacher.html" and role != "teacher":
            return redirect(auth_redirect(role))
        if request.path == "/student.html" and role != "student":
            return redirect(auth_redirect(role))
        if (
            request.path.startswith("/api/")
            and request.method in {"POST", "PATCH", "PUT", "DELETE"}
            and role != "teacher"
            and request.path != "/api/submissions"
        ):
            return jsonify(error="Only teachers can change assignments or grades."), 403
        return None

    @app.errorhandler(SupabaseError)
    def handle_supabase_error(error):
        app.logger.error("%s", error)
        return jsonify(error=str(error)), 502

    @app.errorhandler(413)
    def handle_upload_too_large(_error):
        limit_mb = app.config["MAX_ASSIGNMENT_UPLOAD_BYTES"] // (1024 * 1024)
        return jsonify(error=f"Request is too large. Assignment PDFs may be up to {limit_mb} MB."), 413

    @app.post("/api/auth/signup")
    def auth_signup():
        if not app.config["SUPABASE_ENABLED"]:
            return jsonify(
                error="Account sign-up requires Supabase Auth; local demo accounts are preconfigured."
            ), 503
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="Send account details as JSON."), 400

        role = payload.get("role")
        full_name = payload.get("full_name")
        email = payload.get("email")
        password = payload.get("password")
        if role not in {"teacher", "student"}:
            return jsonify(error="Choose Teacher or Student."), 400
        if not isinstance(full_name, str) or not full_name.strip() or len(full_name.strip()) > 120:
            return jsonify(error="Enter your name (up to 120 characters)."), 400
        if not isinstance(email, str) or "@" not in email or len(email) > 254:
            return jsonify(error="Enter a valid email address."), 400
        if not isinstance(password, str) or not 8 <= len(password) <= 128:
            return jsonify(error="Password must be 8 to 128 characters."), 400

        try:
            session = request_supabase_auth(
                "signup",
                method="POST",
                payload={
                    "email": email.strip(),
                    "password": password,
                    "data": {"full_name": full_name.strip()},
                },
            )
            user = session.get("user") or {}
            user_id = user.get("id")
            if not user_id:
                raise SupabaseError("Supabase Auth did not return the new user ID.")
            request_supabase_auth(
                f"admin/users/{quote(str(user_id), safe='')}",
                method="PUT",
                payload={"app_metadata": {"role": role}},
                admin=True,
            )

            access_token = session.get("access_token")
            if access_token:
                user = request_supabase_auth("user", access_token=access_token)
                if (user.get("app_metadata") or {}).get("role") != role:
                    raise SupabaseError("Supabase did not confirm the assigned account role.")
            response = jsonify(
                {
                    "message": (
                        "Account created."
                        if access_token
                        else "Account created. Confirm your email, then log in."
                    ),
                    "authenticated": bool(access_token),
                    "role": role,
                    "redirect_url": auth_redirect(role),
                }
            )
            return set_auth_cookies(response, session), 201
        except SupabaseAuthError as error:
            return jsonify(error=str(error)), 400

    @app.post("/api/auth/login")
    def auth_login():
        if not app.config["SUPABASE_ENABLED"]:
            if not app.config["LOCAL_DEMO_AUTH_ENABLED"]:
                return jsonify(error="Configure Supabase Auth before logging in."), 503
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict):
                return jsonify(error="Send login details as JSON."), 400
            role = payload.get("role")
            email = payload.get("email")
            password = payload.get("password")
            if role not in {"teacher", "student"}:
                return jsonify(error="Choose Teacher or Student."), 400
            if not isinstance(email, str) or not isinstance(password, str):
                return jsonify(error="Enter your username and password."), 400

            account = {
                "admin": ("teacher", "Admin"),
                "student": ("student", "Student"),
            }.get(email.strip().lower())
            if not account or not hmac.compare_digest(password, "123"):
                return jsonify(error="Username or password is incorrect."), 401
            actual_role, full_name = account
            if actual_role != role:
                return jsonify(
                    error=f"This account is registered as {actual_role.title()}."
                ), 403

            flask_session.clear()
            flask_session["local_demo_user"] = {
                "email": email.strip().lower(),
                "full_name": full_name,
                "role": actual_role,
            }
            return jsonify(
                {
                    "role": actual_role,
                    "redirect_url": auth_redirect(actual_role),
                    "authenticated": True,
                }
            )
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="Send login details as JSON."), 400
        role = payload.get("role")
        email = payload.get("email")
        password = payload.get("password")
        if role not in {"teacher", "student"}:
            return jsonify(error="Choose Teacher or Student."), 400
        if not isinstance(email, str) or not isinstance(password, str):
            return jsonify(error="Enter your email and password."), 400

        try:
            session = request_supabase_auth(
                "token?grant_type=password",
                method="POST",
                payload={"email": email.strip(), "password": password},
            )
            access_token = session.get("access_token")
            if not access_token:
                raise SupabaseError("Supabase Auth did not return a login session.")
            user = request_supabase_auth("user", access_token=access_token)
        except SupabaseAuthError:
            return jsonify(error="Email or password is incorrect, or email is unconfirmed."), 401

        actual_role = (user.get("app_metadata") or {}).get("role")
        if actual_role not in {"teacher", "student"}:
            return jsonify(error="This account does not have a valid dashboard role."), 403
        if actual_role != role:
            return jsonify(error=f"This account is registered as {actual_role.title()}."), 403

        response = jsonify(
            {"role": actual_role, "redirect_url": auth_redirect(actual_role)}
        )
        return set_auth_cookies(response, session)

    @app.post("/api/auth/refresh")
    def auth_refresh():
        if not app.config["SUPABASE_ENABLED"]:
            return jsonify(error="Supabase Auth is not configured."), 503
        refresh_token = request.cookies.get("gradewise_refresh_token")
        if not refresh_token:
            return jsonify(error="Please log in again."), 401
        try:
            session = request_supabase_auth(
                "token?grant_type=refresh_token",
                method="POST",
                payload={"refresh_token": refresh_token},
            )
            access_token = session.get("access_token")
            if not access_token:
                raise SupabaseError("Supabase Auth did not return a refreshed session.")
            user = request_supabase_auth("user", access_token=access_token)
        except SupabaseAuthError:
            response = jsonify(error="Your login session has expired.")
            return clear_auth_cookies(response), 401

        role = (user.get("app_metadata") or {}).get("role")
        if role not in {"teacher", "student"}:
            response = jsonify(error="This account does not have a valid dashboard role.")
            return clear_auth_cookies(response), 403
        response = jsonify({"role": role, "redirect_url": auth_redirect(role)})
        return set_auth_cookies(response, session)

    @app.post("/api/auth/logout")
    def auth_logout():
        if app.config["LOCAL_DEMO_AUTH_ENABLED"]:
            flask_session.clear()
        access_token = request.cookies.get("gradewise_access_token")
        if app.config["SUPABASE_ENABLED"] and access_token:
            try:
                request_supabase_auth(
                    "logout",
                    method="POST",
                    access_token=access_token,
                )
            except SupabaseAuthError as error:
                app.logger.info("Clearing an expired Supabase session: %s", error)
        return clear_auth_cookies(jsonify({"message": "Logged out."}))

    @app.get("/api/auth/me")
    def auth_me():
        if not app.config["SUPABASE_ENABLED"]:
            if not app.config["LOCAL_DEMO_AUTH_ENABLED"]:
                return jsonify({"auth_enabled": False})
            user = flask_session.get("local_demo_user")
            if not isinstance(user, dict):
                return jsonify(
                    {
                        "auth_enabled": True,
                        "demo_mode": True,
                        "authenticated": False,
                    }
                )
            role = user["role"]
            return jsonify(
                {
                    "auth_enabled": True,
                    "demo_mode": True,
                    "authenticated": True,
                    "email": user["email"],
                    "full_name": user["full_name"],
                    "role": role,
                    "redirect_url": auth_redirect(role),
                }
            )
        user = g.current_user
        return jsonify(
            {
                "auth_enabled": True,
                "id": user["id"],
                "email": user.get("email"),
                "full_name": (user.get("user_metadata") or {}).get("full_name"),
                "role": (user.get("app_metadata") or {}).get("role"),
                "redirect_url": auth_redirect(
                    (user.get("app_metadata") or {}).get("role")
                ),
            }
        )

    @app.get("/")
    @app.get("/index.html")
    def index_page():
        return send_from_directory(BASE_DIR, "index.html")

    @app.get("/teacher.html")
    def teacher_page():
        return send_from_directory(BASE_DIR, "teacher.html")

    @app.get("/styles.css")
    def stylesheet():
        return send_from_directory(BASE_DIR, "styles.css")

    @app.get("/student.html")
    def student_page():
        return send_from_directory(BASE_DIR, "student.html")

    @app.get("/api/dashboard")
    def dashboard():
        if app.config["SUPABASE_ENABLED"]:
            rows = select_supabase_rows(
                "evaluations",
                apply_student_scope({
                    "select": (
                        "status,score,"
                        "assignments!evaluations_assignment_id_fkey(total_marks)"
                    )
                }),
            )
            assignments = select_supabase_rows("assignments", {"select": "id"})
            counts = {status: 0 for status in STATUSES}
            percentages = []
            for row in rows:
                status = row.get("status")
                if status in counts:
                    counts[status] += 1
                score = row.get("score")
                total_marks = relation_record(row).get("total_marks")
                if score is not None and total_marks:
                    percentages.append(score * 100 / total_marks)

            submissions = len(rows)
            approved = counts["approved"]
            ai_evaluated = counts["ai_evaluated"]
            pending = counts["pending"]
            evaluated = approved + ai_evaluated
            average = sum(percentages) / len(percentages) if percentages else 0
            return jsonify(
                {
                    "submissions": submissions,
                    "evaluated": evaluated,
                    "pending": pending,
                    "approved": approved,
                    "ai_evaluated": ai_evaluated,
                    "assignment_count": len(assignments),
                    "class_average": round(average),
                    "evaluated_percent": (
                        round(evaluated * 100 / submissions) if submissions else 0
                    ),
                }
            )

        with closing(connect_database()) as connection:
            counts = connection.execute(
                """
                SELECT
                    COUNT(*) AS submissions,
                    SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) AS approved,
                    SUM(CASE WHEN status = 'ai_evaluated' THEN 1 ELSE 0 END) AS ai_evaluated,
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending
                FROM evaluations
                """
            ).fetchone()
            assignment_count = connection.execute(
                "SELECT COUNT(*) FROM assignments"
            ).fetchone()[0]
            average = connection.execute(
                """
                SELECT AVG(e.score * 100.0 / a.total_marks)
                FROM evaluations AS e
                JOIN assignments AS a ON a.id = e.assignment_id
                WHERE e.score IS NOT NULL
                """
            ).fetchone()[0]

        submissions = counts["submissions"] or 0
        approved = counts["approved"] or 0
        ai_evaluated = counts["ai_evaluated"] or 0
        pending = counts["pending"] or 0
        evaluated = approved + ai_evaluated
        return jsonify(
            {
                "submissions": submissions,
                "evaluated": evaluated,
                "pending": pending,
                "approved": approved,
                "ai_evaluated": ai_evaluated,
                "assignment_count": assignment_count,
                "class_average": round(average or 0),
                "evaluated_percent": round(evaluated * 100 / submissions) if submissions else 0,
            }
        )

    @app.get("/api/assignments")
    def assignments():
        columns = (
            "id,title,subject,due_date,total_marks,created_at,original_filename,"
            "storage_path,mime_type,ocr_text,ocr_method,file_size,created_by,key_answer_text"
        )
        if app.config["SUPABASE_ENABLED"]:
            rows = select_supabase_rows(
                "assignments",
                {
                    "select": columns,
                    "order": "created_at.desc,id.desc",
                },
            )
            return jsonify([assignment_payload(row) for row in rows])

        with closing(connect_database()) as connection:
            rows = connection.execute(
                """
                SELECT id, title, subject, due_date, total_marks, created_at,
                    original_filename, storage_path, mime_type, ocr_text,
                    ocr_method, file_size, created_by
                FROM assignments
                ORDER BY created_at DESC, id DESC
                """
            ).fetchall()
        return jsonify([assignment_payload(row) for row in rows])

    @app.post("/api/assignments")
    def create_assignment():
        upload = None
        if request.is_json:
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict):
                return jsonify(error="Send assignment details as a JSON object."), 400
        else:
            payload = request.form
            upload = request.files.get("assignment_file")
            if not payload:
                return jsonify(error="Send assignment details as multipart form data."), 400

        title = payload.get("title")
        subject = payload.get("subject")
        due_date = payload.get("due_date") or None
        raw_total_marks = payload.get("total_marks")
        if isinstance(raw_total_marks, bool):
            return jsonify(error="Total marks must be a whole number between 1 and 1000."), 400
        try:
            total_marks = int(raw_total_marks)
            if isinstance(raw_total_marks, float) and not raw_total_marks.is_integer():
                raise ValueError
        except (TypeError, ValueError):
            return jsonify(error="Total marks must be a whole number between 1 and 1000."), 400
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 160:
            return jsonify(error="Enter an assignment title of 1 to 160 characters."), 400
        if not isinstance(subject, str) or not subject.strip() or len(subject.strip()) > 80:
            return jsonify(error="Enter a subject of 1 to 80 characters."), 400
        if not 1 <= total_marks <= 1000:
            return jsonify(error="Total marks must be a whole number between 1 and 1000."), 400
        if due_date:
            try:
                date.fromisoformat(due_date)
            except (TypeError, ValueError):
                return jsonify(error="Due date must be a valid ISO date."), 400

        key_upload = request.files.get("key_answer_file") if not request.is_json else None
        key_answer_text = ""
        if key_upload and key_upload.filename:
            key_bytes = key_upload.stream.read(app.config["MAX_ASSIGNMENT_UPLOAD_BYTES"] + 1)
            if len(key_bytes) > app.config["MAX_ASSIGNMENT_UPLOAD_BYTES"]:
                return jsonify(error="Key answer PDF must be 20 MB or smaller."), 413
            if not key_bytes.startswith(b"%PDF-"):
                return jsonify(error="Key answer file must be a valid PDF."), 415
            try:
                key_ocr = extract_assignment_pdf(key_bytes, app.config["GOOGLE_VISION_API_KEY"])
                key_answer_text = key_ocr["text"]
            except GoogleVisionError as error:
                return jsonify(error=str(error)), error.status_code

        pdf_bytes = None
        original_filename = None
        ocr = {"text": "", "method": "none", "page_count": 0}
        if upload and upload.filename:
            original_filename = upload.filename.replace("\\", "/").rsplit("/", 1)[-1]
            safe_name = secure_filename(original_filename)
            if not safe_name or Path(safe_name).suffix.lower() != ".pdf":
                return jsonify(error="Upload an assignment PDF file."), 415
            pdf_bytes = upload.stream.read(app.config["MAX_ASSIGNMENT_UPLOAD_BYTES"] + 1)
            if len(pdf_bytes) > app.config["MAX_ASSIGNMENT_UPLOAD_BYTES"]:
                limit_mb = app.config["MAX_ASSIGNMENT_UPLOAD_BYTES"] // (1024 * 1024)
                return jsonify(error=f"Assignment PDF must be {limit_mb} MB or smaller."), 413
            if not pdf_bytes.startswith(b"%PDF-"):
                return jsonify(error="The uploaded file is not a valid PDF."), 415
            try:
                ocr = extract_assignment_pdf(pdf_bytes, app.config["GOOGLE_VISION_API_KEY"])
            except GoogleVisionError as error:
                return jsonify(error=str(error)), error.status_code
            original_filename = safe_name[:255]

        created_at = datetime.now().replace(microsecond=0).isoformat()
        current_user = getattr(g, "current_user", {}) or {}
        created_by = current_user.get("id")
        mime_type = "application/pdf" if pdf_bytes else None
        storage_path = None
        saved_local_path = None
        storage_uploaded = False
        try:
            if pdf_bytes and app.config["SUPABASE_ENABLED"]:
                storage_path = f"{uuid.uuid4().hex}/{original_filename}"
                object_path = (
                    f"{quote(app.config['SUPABASE_ASSIGNMENTS_BUCKET'], safe='')}/"
                    f"{storage_url_path(storage_path)}"
                )
                supabase_storage_request(
                    app.config["SUPABASE_URL"],
                    app.config["SUPABASE_SERVICE_ROLE_KEY"],
                    f"object/{object_path}",
                    method="POST",
                    raw_body=pdf_bytes,
                    content_type="application/pdf",
                    extra_headers={"x-upsert": "false"},
                )
                storage_uploaded = True
            elif pdf_bytes:
                storage_path = f"{uuid.uuid4().hex}_{original_filename}"
                upload_dir = Path(app.config["ASSIGNMENT_UPLOAD_DIR"])
                upload_dir.mkdir(parents=True, exist_ok=True)
                saved_local_path = upload_dir / storage_path
                saved_local_path.write_bytes(pdf_bytes)

            record = {
                "title": title.strip(),
                "subject": subject.strip(),
                "due_date": due_date,
                "total_marks": total_marks,
                "original_filename": original_filename,
                "storage_path": storage_path,
                "mime_type": mime_type,
                "ocr_text": ocr["text"],
                "ocr_method": ocr["method"],
                "file_size": len(pdf_bytes) if pdf_bytes else None,
                "created_by": created_by,
                "key_answer_text": key_answer_text,
            }
            if app.config["SUPABASE_ENABLED"]:
                rows = request_supabase(
                    "assignments", method="POST", payload=record
                )
                if not rows:
                    raise SupabaseError("Supabase did not return the created assignment.")
                return jsonify(assignment_payload(rows[0])), 201

            with closing(connect_database()) as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO assignments (
                        title, subject, due_date, total_marks, created_at,
                        original_filename, storage_path, mime_type, ocr_text,
                        ocr_method, file_size, created_by, key_answer_text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record["title"], record["subject"], record["due_date"],
                        record["total_marks"], created_at, record["original_filename"],
                        record["storage_path"], record["mime_type"], record["ocr_text"],
                        record["ocr_method"], record["file_size"], record["created_by"],
                        record["key_answer_text"],
                    ),
                )
                assignment_id = cursor.lastrowid
                connection.commit()
                row = connection.execute(
                    "SELECT * FROM assignments WHERE id = ?", (assignment_id,)
                ).fetchone()
            return jsonify(assignment_payload(row)), 201
        except Exception:
            if storage_uploaded and storage_path:
                try:
                    supabase_storage_request(
                        app.config["SUPABASE_URL"],
                        app.config["SUPABASE_SERVICE_ROLE_KEY"],
                        f"object/{quote(app.config['SUPABASE_ASSIGNMENTS_BUCKET'], safe='')}",
                        method="DELETE",
                        payload={"prefixes": [storage_path]},
                    )
                except SupabaseError:
                    app.logger.exception("Could not clean up an orphaned assignment PDF.")
            if saved_local_path:
                try:
                    saved_local_path.unlink(missing_ok=True)
                except OSError:
                    app.logger.exception("Could not clean up an orphaned local assignment PDF.")
            raise

    @app.get("/api/assignments/<int:assignment_id>/file")
    def assignment_file(assignment_id):
        if app.config["SUPABASE_ENABLED"]:
            rows = request_supabase(
                "assignments",
                params={
                    "id": f"eq.{assignment_id}",
                    "select": "storage_path,original_filename,mime_type",
                },
            )
            assignment = rows[0] if rows else None
        else:
            with closing(connect_database()) as connection:
                assignment = connection.execute(
                    "SELECT storage_path,original_filename,mime_type FROM assignments WHERE id = ?",
                    (assignment_id,),
                ).fetchone()
        if not assignment or not assignment["storage_path"]:
            return jsonify(error="Assignment file not found."), 404

        if app.config["SUPABASE_ENABLED"]:
            bucket = app.config["SUPABASE_ASSIGNMENTS_BUCKET"]
            object_path = (
                f"object/sign/{quote(bucket, safe='')}/"
                f"{storage_url_path(assignment['storage_path'])}"
            )
            signed = supabase_storage_request(
                app.config["SUPABASE_URL"],
                app.config["SUPABASE_SERVICE_ROLE_KEY"],
                object_path,
                method="POST",
                payload={"expiresIn": 900},
            )
            signed_url = signed.get("signedURL") or signed.get("signedUrl")
            if not signed_url:
                raise SupabaseError("Supabase Storage did not return a signed file URL.")
            if signed_url.startswith("http://") or signed_url.startswith("https://"):
                return redirect(signed_url)
            if signed_url.startswith("/storage/v1/"):
                signed_url = f"{app.config['SUPABASE_URL'].rstrip('/')}{signed_url}"
            else:
                signed_url = f"{app.config['SUPABASE_URL'].rstrip('/')}/storage/v1{signed_url if signed_url.startswith('/') else '/' + signed_url}"
            return redirect(signed_url)

        stored_name = assignment["storage_path"]
        if Path(stored_name).name != stored_name:
            return jsonify(error="Assignment file path is invalid."), 404
        return send_from_directory(
            app.config["ASSIGNMENT_UPLOAD_DIR"],
            stored_name,
            mimetype="application/pdf",
            as_attachment=False,
        )

    @app.post("/api/submissions")
    def create_submission():
        upload = request.files.get("answer_pdf")
        assignment_id = request.form.get("assignment_id")
        if not assignment_id:
            return jsonify(error="assignment_id is required."), 400
        try:
            assignment_id = int(assignment_id)
        except ValueError:
            return jsonify(error="assignment_id must be a number."), 400
        if not upload or not upload.filename:
            return jsonify(error="Upload a PDF answer file."), 400

        pdf_bytes = upload.stream.read(app.config["MAX_ASSIGNMENT_UPLOAD_BYTES"] + 1)
        if len(pdf_bytes) > app.config["MAX_ASSIGNMENT_UPLOAD_BYTES"]:
            return jsonify(error="PDF must be 20 MB or smaller."), 413
        if not pdf_bytes.startswith(b"%PDF-"):
            return jsonify(error="Upload a valid PDF file."), 415

        # OCR the student answer PDF
        try:
            ocr = extract_assignment_pdf(pdf_bytes, app.config["GOOGLE_VISION_API_KEY"])
        except GoogleVisionError as error:
            return jsonify(error=str(error)), error.status_code
        answer_text = ocr["text"]

        # Fetch assignment for question + total_marks
        if app.config["SUPABASE_ENABLED"]:
            rows = request_supabase(
                "assignments",
                params={"id": f"eq.{assignment_id}", "select": "id,title,ocr_text,total_marks,key_answer_text"},
            )
            assignment = rows[0] if rows else None
        else:
            with closing(connect_database()) as connection:
                assignment = connection.execute(
                    "SELECT id, title, ocr_text, total_marks, key_answer_text FROM assignments WHERE id = ?",
                    (assignment_id,),
                ).fetchone()
        if not assignment:
            return jsonify(error="Assignment not found."), 404

        total_marks = assignment["total_marks"]
        question = assignment["title"]
        reference_answer = assignment["key_answer_text"] or assignment["ocr_text"] or ""

        # Build a simple rubric from total_marks
        rubric = [
            {"name": "Content accuracy", "points": round(total_marks * 0.5, 1)},
            {"name": "Completeness", "points": round(total_marks * 0.3, 1)},
            {"name": "Clarity", "points": round(total_marks * 0.2, 1)},
        ]

        # Evaluate with Groq
        try:
            raw = evaluate_answer(question, reference_answer, answer_text, rubric)
            result = score_evaluation(raw, rubric, total_marks)
        except Exception as error:
            return jsonify(error=f"AI evaluation failed: {error}"), 502

        score = result["score"]
        eval_status = "ai_evaluated"
        ai_feedback = json.dumps({
            "summary": result["summary"],
            "confidence": result["confidence"],
            "criterion_scores": result["criterion_scores"],
        })
        submitted_at = datetime.now().replace(microsecond=0).isoformat()
        current_user = getattr(g, "current_user", {}) or {}
        student_id = current_user.get("id") or current_user.get("email", "unknown")
        student_name = (
            (current_user.get("user_metadata") or {}).get("full_name")
            or current_user.get("email", "Student")
        )

        if app.config["SUPABASE_ENABLED"]:
            request_supabase(
                "submissions",
                method="POST",
                payload={
                    "assignment_id": assignment_id,
                    "student_id": student_id,
                    "student_name": student_name,
                    "answer_text": answer_text,
                    "submitted_at": submitted_at,
                },
            )
            request_supabase(
                "evaluations",
                method="POST",
                payload={
                    "assignment_id": assignment_id,
                    "student": student_name,
                    "student_id": student_id,
                    "score": score,
                    "status": eval_status,
                    "submitted_at": submitted_at,
                    "ai_feedback": ai_feedback,
                },
            )
        else:
            with closing(connect_database()) as connection:
                connection.execute(
                    "INSERT INTO submissions (assignment_id, student_id, student_name, answer_text, submitted_at) VALUES (?, ?, ?, ?, ?)",
                    (assignment_id, student_id, student_name, answer_text, submitted_at),
                )
                connection.execute(
                    "INSERT INTO evaluations (assignment_id, student, score, status, submitted_at, ai_feedback) VALUES (?, ?, ?, ?, ?, ?)",
                    (assignment_id, student_name, score, eval_status, submitted_at, ai_feedback),
                )
                connection.commit()

        return jsonify({
            "score": score,
            "total_marks": total_marks,
            "status": result["status"],
            "confidence": result["confidence"],
            "summary": result["summary"],
            "criterion_scores": result["criterion_scores"],
            "provider": result["provider"],
        }), 201

    @app.get("/api/evaluations")
    def evaluations():
        status = request.args.get("status", "").strip()
        query = request.args.get("q", "").strip()
        if status and status not in STATUSES:
            return jsonify(error="Unknown evaluation status."), 400
        try:
            limit = min(max(int(request.args.get("limit", "20")), 1), 200)
            offset = max(int(request.args.get("offset", "0")), 0)
        except ValueError:
            return jsonify(error="Limit and offset must be whole numbers."), 400

        if app.config["SUPABASE_ENABLED"]:
            params = apply_student_scope({
                "select": (
                    "id,student,score,status,submitted_at,ai_feedback,"
                    "assignments!evaluations_assignment_id_fkey(title,subject,total_marks)"
                ),
                "order": "submitted_at.desc,id.desc",
            })
            if status:
                params["status"] = f"eq.{status}"
            rows = select_supabase_rows("evaluations", params)
            items = []
            normalized_query = query.casefold()
            for row in rows:
                assignment = relation_record(row)
                item = {
                    "id": row["id"],
                    "assignment_title": assignment.get("title"),
                    "subject": assignment.get("subject"),
                    "total_marks": assignment.get("total_marks"),
                    "student": row["student"],
                    "score": row.get("score"),
                    "status": row["status"],
                    "submitted_at": row["submitted_at"],
                    "ai_feedback": row.get("ai_feedback"),
                }
                if normalized_query and not any(
                    normalized_query in str(item[field] or "").casefold()
                    for field in ("assignment_title", "subject", "student")
                ):
                    continue
                items.append(item)
            return jsonify(
                {
                    "items": items[offset : offset + limit],
                    "total": len(items),
                    "limit": limit,
                    "offset": offset,
                }
            )

        clauses = []
        parameters = []
        if status:
            clauses.append("e.status = ?")
            parameters.append(status)
        if query:
            search_term = f"%{query}%"
            clauses.append(
                "(a.title LIKE ? OR a.subject LIKE ? OR e.student LIKE ?)"
            )
            parameters.extend([search_term, search_term, search_term])
        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with closing(connect_database()) as connection:
            total = connection.execute(
                f"""
                SELECT COUNT(*)
                FROM evaluations AS e
                JOIN assignments AS a ON a.id = e.assignment_id
                {where_clause}
                """,
                parameters,
            ).fetchone()[0]
            rows = connection.execute(
                f"""
                SELECT e.id, a.title AS assignment_title, a.subject, a.total_marks,
                       e.student, e.score, e.status, e.submitted_at, e.ai_feedback
                FROM evaluations AS e
                JOIN assignments AS a ON a.id = e.assignment_id
                {where_clause}
                ORDER BY e.submitted_at DESC, e.id DESC
                LIMIT ? OFFSET ?
                """,
                [*parameters, limit, offset],
            ).fetchall()

        return jsonify(
            {
                "items": [dict(row) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        )

    @app.patch("/api/evaluations/<int:evaluation_id>")
    def approve_evaluation(evaluation_id):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or payload.get("status") != "approved":
            return jsonify(error="Set the evaluation status to approved."), 400

        score = payload.get("score")
        if app.config["SUPABASE_ENABLED"]:
            rows = request_supabase(
                "evaluations",
                params={
                    "select": (
                        "id,status,score,"
                        "assignments!evaluations_assignment_id_fkey(total_marks)"
                    ),
                    "id": f"eq.{evaluation_id}",
                    "limit": "1",
                },
            )
            if not rows:
                return jsonify(error="Evaluation not found."), 404
            row = rows[0]
            if row["status"] == "approved":
                return jsonify(error="This evaluation is already approved."), 409
            if score is None:
                score = row.get("score")
            total_marks = relation_record(row).get("total_marks")
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                return jsonify(error="Enter a numeric score before approval."), 400
            if not 0 <= score <= total_marks:
                return jsonify(error=f"Score must be between 0 and {total_marks}."), 400

            updated = request_supabase(
                "evaluations",
                method="PATCH",
                params={"id": f"eq.{evaluation_id}"},
                payload={"score": score, "status": "approved"},
            )
            if not updated:
                return jsonify(error="Evaluation not found."), 404
            return jsonify(
                {
                    "id": evaluation_id,
                    "status": "approved",
                    "score": score,
                }
            )

        with closing(connect_database()) as connection:
            row = connection.execute(
                """
                SELECT e.status, e.score, a.total_marks
                FROM evaluations AS e
                JOIN assignments AS a ON a.id = e.assignment_id
                WHERE e.id = ?
                """,
                (evaluation_id,),
            ).fetchone()
            if row is None:
                return jsonify(error="Evaluation not found."), 404
            if row["status"] == "approved":
                return jsonify(error="This evaluation is already approved."), 409
            if score is None:
                score = row["score"]
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                return jsonify(error="Enter a numeric score before approval."), 400
            if not 0 <= score <= row["total_marks"]:
                return jsonify(error=f"Score must be between 0 and {row['total_marks']}."), 400

            connection.execute(
                "UPDATE evaluations SET score = ?, status = 'approved' WHERE id = ?",
                (score, evaluation_id),
            )
            connection.commit()

        return jsonify({"id": evaluation_id, "status": "approved", "score": score})

    @app.get("/api/performance")
    def performance():
        period = request.args.get("period", "month")
        now = datetime.now()
        if period == "month":
            current_start = datetime(now.year, now.month, 1)
            if now.month == 1:
                previous_start = datetime(now.year - 1, 12, 1)
            else:
                previous_start = datetime(now.year, now.month - 1, 1)
        elif period == "3months":
            current_start = now - timedelta(days=90)
            previous_start = now - timedelta(days=180)
        elif period == "semester":
            current_start = now - timedelta(days=182)
            previous_start = now - timedelta(days=364)
        else:
            return jsonify(error="Choose month, 3months, or semester."), 400

        if app.config["SUPABASE_ENABLED"]:
            rows = select_supabase_rows(
                "evaluations",
                apply_student_scope({
                    "select": (
                        "score,submitted_at,"
                        "assignments!evaluations_assignment_id_fkey(subject,total_marks)"
                    )
                }),
            )
        else:
            with closing(connect_database()) as connection:
                rows = connection.execute(
                    """
                    SELECT a.subject, e.score, a.total_marks, e.submitted_at
                    FROM evaluations AS e
                    JOIN assignments AS a ON a.id = e.assignment_id
                    WHERE e.score IS NOT NULL AND e.submitted_at >= ?
                    """,
                    (previous_start.isoformat(),),
                ).fetchall()

        values = {
            subject: {"previous": [], "current": []}
            for subject in CHART_SUBJECTS
        }
        for row in rows:
            if app.config["SUPABASE_ENABLED"]:
                assignment = relation_record(row)
                subject = assignment.get("subject")
                score = row.get("score")
                total_marks = assignment.get("total_marks")
                submitted_at = row.get("submitted_at")
                if score is None or not total_marks or not submitted_at:
                    continue
                parsed_submitted_at = parse_datetime(submitted_at)
                if parsed_submitted_at < previous_start:
                    continue
            else:
                subject = row["subject"]
                score = row["score"]
                total_marks = row["total_marks"]
                parsed_submitted_at = datetime.fromisoformat(row["submitted_at"])
            if subject not in values:
                values[subject] = {"previous": [], "current": []}
            series = (
                "current"
                if parsed_submitted_at >= current_start
                else "previous"
            )
            values[subject][series].append(score * 100 / total_marks)

        result = []
        for subject, series in values.items():
            result.append(
                {
                    "subject": subject,
                    "previous": round(sum(series["previous"]) / len(series["previous"]))
                    if series["previous"]
                    else 0,
                    "current": round(sum(series["current"]) / len(series["current"]))
                    if series["current"]
                    else 0,
                }
            )
        return jsonify({"period": period, "subjects": result})

    return app


app = create_app()


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
