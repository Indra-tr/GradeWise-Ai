# GradeWise - AI

### Teacher Dashboard

The current runnable app is a teacher dashboard for assignment management,
evaluation review, grade approval, and performance charts. It uses Flask with
SQLite locally or Supabase when configured. Supabase Auth provides Student and
Teacher sign-up and login. Teachers can upload assignment PDFs; the backend
stores the PDF in a private Supabase Storage bucket, extracts text with Google
Cloud Vision for scanned pages, saves the OCR text with the assignment row, and
shows the assignment and a temporary PDF link to students.

---

## Features

- Assignment creation
- Assignment PDF upload to private Supabase Storage
- Google Vision document OCR for scanned PDF pages
- Student assignment list with secure PDF links and extracted-text preview
- Evaluation search and status filtering
- Grade review and approval
- Dashboard metrics and reporting-period charts
- Student and Teacher account sign-up/login when Supabase is configured
- SQLite local fallback or Supabase persistence

---

## Technologies Used

### Frontend

| Technology | Purpose |
|---|---|
| HTML5 | Web page structure |
| CSS3 | Styling and responsive design |
| JavaScript | Frontend interactions and API requests |

### Backend and persistence

| Technology | Purpose |
|---|---|
| Python and Flask | Backend and API |
| SQLite | Local fallback database |
| Supabase PostgREST | Hosted database API |
| Supabase Storage | Private assignment PDF storage |
| Google Cloud Vision REST API | OCR for scanned PDF pages |
| Python Dotenv | Server-side environment configuration |

---

## Project Structure

```text
GradeWise-AI/
│
├── app.py
├── teacher.html
├── student.html
├── index.html
├── styles.css
├── .env.example
├── backend/
│   └── google_vision_ocr.py
├── supabase/
│   └── schema.sql
├── tests/
│   └── test_app.py
├── requirements.txt
├── .gitignore
└── README.md
```

---

## Requirements

- Python 3.10 or newer
- Visual Studio Code
- Internet connection only when using Supabase
- Google Cloud Vision API enabled in the Google Cloud project for scanned PDFs

---

## Installation and Setup

### Step 1: Open the Project

Open the GradeWise-AI project folder in Visual Studio Code.

### Step 2: Create a Virtual Environment

Open the VS Code terminal and execute:

```bash
python -m venv .venv
```

### Step 3: Activate the Virtual Environment

For Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

If PowerShell blocks activation, execute:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Then activate the environment again.

### Step 4: Install Dependencies

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### Step 5: Configure the database

The dashboard uses SQLite locally by default. To use Supabase instead, follow the
Supabase setup below. Do not put database keys in frontend code or commit your
`.env` file.

### Step 6: Run the Application

Execute:

```bash
python app.py
```

Alternatively:

```bash
flask --app app run --debug
```

Open the application in your browser:

```text
http://127.0.0.1:5000
```

### Current dashboards

The current dashboard is served by `app.py`. When Supabase is configured, its API
uses the Supabase database and authentication; otherwise it uses SQLite for
persistent local development data.
Install dependencies and run:

```bash
python -m pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000` for the sign-in page. Choose Teacher or Student and
then log in or create an account. When Supabase is not configured, the SQLite
demo accepts Teacher `admin` / `123` and Student `student` / `123`; local sign-up
is unavailable. In SQLite mode, the database is created at
`data/gradewise.sqlite3` on first run and seeded with demonstration assignments
and evaluations. These fixed demo accounts are only for local testing, not real
student data. Creating assignments and approving grades update the selected
database.

The dashboard supports assignment PDF uploads, evaluation search and status
filtering, grade approval, and reporting-period charts. Uploaded assignments
are immediately listed in the student dashboard. Assignment PDFs are kept in a
private Storage bucket and delivered through short-lived signed links. Google
Vision OCR runs on pages without selectable text; searchable PDF text is
extracted locally. AI grading and student answer submissions are not part of
this upload flow.
Supabase authentication and role checks apply when Supabase is configured. The
`GRADEWISE_DEMO_AUTH` switch is intended only for controlled local testing;
disabling it leaves the SQLite fallback without account protection.

Run the backend tests with:

```bash
python -m unittest discover -s tests -v
```

### Supabase setup

1. Create a Supabase project and open its SQL Editor.
2. Run the updated schema in [`supabase/schema.sql`](./supabase/schema.sql). It
   adds the assignment file/OCR columns and creates a private `assignments`
   Storage bucket. It is safe to run on the existing assignments/evaluations
   tables.
3. In the Supabase dashboard, copy the project URL, anon key, and service-role
   key. Add them to `.env` as `SUPABASE_URL`, `SUPABASE_ANON_KEY`, and
   `SUPABASE_SERVICE_ROLE_KEY`. If `.env` already exists, add the missing keys;
   do not replace its existing values.
4. Restart `python app.py`. Supabase mode is enabled only when all three values
   are present. The service-role key remains server-side.

### Google Vision OCR setup

1. Enable the Cloud Vision API in your Google Cloud project and create an API
   key.
2. Add `GOOGLE_VISION_API_KEY=...` to the server `.env`. Restrict the key to
   the Cloud Vision API and keep it out of HTML, JavaScript, and source control.
3. Install the updated Python dependencies with `pip install -r requirements.txt`.

The upload endpoint sends each scanned PDF page as a base64 image to Vision's
`images:annotate` endpoint with `DOCUMENT_TEXT_DETECTION`. PDFs with selectable
text use local extraction and do not make Vision calls. Vision API usage may
incur Google Cloud charges. The browser never receives the API key.

`POST /api/assignments` accepts multipart fields `title`, `subject`, optional
`due_date`, `total_marks`, and the PDF field `assignment_file`. It returns the
created assignment, OCR method, text excerpt, and a same-origin file route.
`GET /api/assignments` lists assignments for both dashboards;
`GET /api/assignments/<id>/file` returns the local PDF or redirects to a
15-minute Supabase signed URL.

Teacher and Student accounts use Supabase Auth email/password. Supabase email
confirmation settings apply. Teacher self-sign-up is enabled, so anyone can
create a Teacher account. Roles are assigned server-side in protected Auth app
metadata. Students have read-only access to evaluations whose `student_id`
matches their `auth.users.id`; existing demo rows are not automatically linked
to accounts.

The service-role key must remain server-side and must never be added to
`teacher.html`, `student.html`, `index.html`, or source control. If all three
Supabase settings are unset, the app continues using SQLite; if only some are
set, startup stops with a configuration error. The local SQLite database is
retained as a fallback and is not automatically copied to Supabase.

---

## Current database model

`assignments` stores title, subject, due date, marks, uploader, PDF Storage path,
OCR text, OCR method, and file metadata. `evaluations` remains linked to
assignments by `assignment_id`. Supabase uses `supabase/schema.sql`; the SQLite
fallback adds the upload columns automatically and stores local PDFs under
`data/assignment_uploads/`. Existing SQLite demo records are not copied to
Supabase.

---

## Security

- The Supabase service-role key is read only by the Flask backend.
- Assignment PDFs are stored in a private bucket; student links expire after 15 minutes.
- Restrict the Google Vision key to the Vision API and keep it only in `.env`.
- Row-level security is enabled in the supplied schema; no browser policies are
  added because the dashboard accesses Supabase through Flask after checking
  the authenticated role.
- Session tokens are stored in HttpOnly cookies.
- The local demo uses fixed, intentionally weak credentials and is not secure
  for real data or public deployment. Configure Supabase Auth for real accounts.
- Teacher self-sign-up is enabled; add an invitation or approval process before
  exposing the app publicly if unrestricted Teacher accounts are not acceptable.
- Students can only view records linked to their Supabase user ID and cannot
  create assignments or approve grades.

---

## Future Enhancements

- Multilingual answer evaluation.
- Advanced machine learning grading models.
- Student performance prediction.
- Detailed academic analytics.
- Integration with college management systems.

---

## Important Disclaimer

Teacher assignment uploads, PDF storage, Google Vision OCR, and student access to
published assignments are implemented. Automatic grading of student answer
submissions is not part of this version.

---

## License

Developed for educational and hackathon purposes.
