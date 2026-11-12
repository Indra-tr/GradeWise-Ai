# GradeWise - AI

### AI-Powered Subjective Answer Evaluation System

GradeWise - AI is an AI-assisted platform designed to automate subjective answer evaluation using Artificial Intelligence, Natural Language Processing, and Machine Learning.

The system evaluates student answers using reference answers, semantic similarity, and predefined marking rubrics. It generates marks, detailed feedback, and performance insights to help teachers assess students more efficiently.

The application uses **HTML, CSS, and JavaScript** for the frontend and **Python Flask** for the backend.

---

## Features

- AI-powered subjective answer evaluation
- Semantic similarity analysis
- Rubric-based grading
- Reference answer comparison
- PDF answer sheet upload
- Automated marks calculation
- Personalized student feedback
- Teacher evaluation dashboard
- Student performance dashboard
- Evaluation reports and analytics
- Database-based data management
- Teacher review and grade approval

---

## Technologies Used

### Frontend

| Technology | Purpose |
|---|---|
| HTML5 | Web page structure |
| CSS3 | Styling and responsive design |
| JavaScript | Frontend interactions and API requests |

### Backend and AI

| Technology | Purpose |
|---|---|
| Python | Core backend programming |
| Flask | Web framework and API development |
| Google Gemini API | AI-based answer evaluation |
| Sentence Transformers | Semantic similarity |
| Scikit-learn | Machine learning utilities |
| NumPy | Numerical calculations |
| Pandas | Data processing |
| PyMuPDF | PDF text extraction |
| Pillow | Image processing |
| Plotly | Interactive charts |
| SQLAlchemy | Database management |
| Pydantic | Data validation |
| Python Dotenv | Environment variable management |

---

## Project Structure

```text
GradeWise-AI/
│
├── app.py
├── grading.py
├── db.py
├── document_parser.py
├── reports.py
│
├── templates/
│   └── index.html
│
├── static/
│   ├── css/
│   │   └── style.css
│   │
│   ├── js/
│   │   └── script.js
│   │
│   └── images/
│
├── data/
│   └── database.db
│
├── requirements.txt
├── .env
├── .env.example
├── .gitignore
└── README.md
```

---

## Requirements

- Python 3.10 or newer
- Visual Studio Code
- Internet connection
- Google Gemini API key

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

### Step 5: Configure Gemini API

Create a `.env` file in the project root directory.

Add your Gemini API key:

```env
GEMINI_API_KEY=your_actual_api_key
GEMINI_MODEL=gemini-2.5-flash
```

Get your API key from:

https://aistudio.google.com/apikey

**Important:** Never upload your actual API key to GitHub.

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

---

## System Workflow

1. Teacher creates an assignment with questions.
2. Teacher provides reference answers and marking rubrics.
3. Student submits answers using text input or PDF upload.
4. The backend extracts text from submitted PDF documents.
5. Semantic analysis compares the student's answer with reference material.
6. Gemini AI evaluates the answer based on the marking rubric.
7. The system generates marks and personalized feedback.
8. Teacher reviews and approves the generated evaluation.
9. Students can view their approved marks and feedback.

---

## AI and Machine Learning

GradeWise - AI uses the following technologies:

**Google Gemini API**
- AI-based answer analysis.
- Rubric-based evaluation.
- Personalized feedback generation.

**Sentence Transformers**
- Converts text into semantic embeddings.
- Helps identify meaning and context similarities.

**Scikit-learn**
- Similarity calculations.
- Machine learning utilities.

**NumPy**
- Numerical operations and calculations.

**Pydantic**
- Validation of structured evaluation results.

---

## Database Management

SQLAlchemy is used for database interaction and management.

The database stores:

- Student information
- Teacher information
- Assignment details
- Questions and reference answers
- Marking rubrics
- Student submissions
- Evaluation results
- Teacher feedback

---

## Security and Responsible AI

- Gemini API keys are stored securely in environment variables.
- API calls are handled by the Python backend.
- Student and teacher access should use authentication and authorization.
- Uploaded documents should be validated.
- AI-generated marks are provisional until approved by a teacher.
- Student information should be protected.

---

## Future Enhancements

- Handwritten answer recognition using OCR.
- Multilingual answer evaluation.
- Advanced machine learning grading models.
- Student performance prediction.
- Detailed academic analytics.
- Integration with college management systems.

---

## Important Disclaimer

GradeWise - AI is designed as an AI-assisted academic evaluation platform.

AI-generated marks and feedback should be reviewed by teachers before final approval. The system supports human academic judgment rather than replacing it.

---

## License

Developed for educational and hackathon purposes.
