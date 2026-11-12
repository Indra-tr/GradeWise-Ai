from __future__ import annotations

import csv
import io
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import PurePath
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend.database import Assessment, AuditLog, Base, SessionLocal, Submission, engine, get_db, utc_now
from backend.document_parser import DocumentParseError, extract_pdf_text
from backend.evaluator import evaluate_answer
from backend.feedback_generator import generate_feedback
from backend.scoring_engine import score_evaluation


load_dotenv()
DEFAULT_COURSE_CODE = os.getenv("DEFAULT_COURSE_CODE", "CS204")
DEFAULT_COURSE_TITLE = os.getenv("DEFAULT_COURSE_TITLE", "Data Structures")
ENROLLED_STUDENTS = int(os.getenv("ENROLLED_STUDENTS", "128"))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "25")) * 1024 * 1024


class RubricCriterionIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    points: float = Field(gt=0, le=10000)


class AssessmentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    course_code: str = Field(default=DEFAULT_COURSE_CODE, min_length=1, max_length=40)
    course_title: str = Field(default=DEFAULT_COURSE_TITLE, max_length=200)
    question_text: str = Field(default="", max_length=12000)
    reference_answer: str = Field(default="", max_length=20000)
    max_score: float = Field(gt=0, le=10000)
    rubric: list[RubricCriterionIn] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def rubric_total_matches_max_score(self):
        if self.rubric and abs(sum(item.points for item in self.rubric) - self.max_score) > 0.01:
            raise ValueError("Rubric criterion points must add up to max_score.")
        return self


class SubmissionReview(BaseModel):
    score: float = Field(ge=0)
    feedback: str | None = Field(default=None, max_length=10000)
    release: bool = False
    reviewer: str = Field(default="Faculty", min_length=1, max_length=200)
    reason: str = Field(default="", max_length=2000)


def _criterion_seed(score: float, points: float, name: str, confidence: float) -> dict:
    return {
        "criterion": name,
        "points": points,
        "score": score,
        "confidence": confidence,
        "evidence": "Seeded demo data for the dashboard prototype.",
        "feedback": "Review edge cases and explain the reasoning for full credit.",
    }


def seed_demo_data(db: Session) -> None:
    """Provide a small dashboard sample; this is separate from real uploaded work."""
    if db.query(Assessment).first():
        return

    rubric_q4 = [
        {"name": "Concept understanding", "description": "Correctly explains the relevant data structure concepts.", "points": 5.0},
        {"name": "Problem-solving approach", "description": "Presents a valid algorithm and intermediate reasoning.", "points": 6.0},
        {"name": "Code correctness", "description": "Produces correct code and handles expected input.", "points": 5.0},
        {"name": "Explanation & clarity", "description": "Explains complexity, assumptions, and edge cases clearly.", "points": 4.0},
    ]
    q4 = Assessment(
        name="Midterm · Q4", course_code=DEFAULT_COURSE_CODE, course_title=DEFAULT_COURSE_TITLE,
        question_text="Explain and implement the requested data-structure operation.",
        reference_answer="A correct answer describes the invariant, gives a working algorithm, handles empty and boundary inputs, and explains time complexity.",
        max_score=20, rubric=rubric_q4,
    )
    q3 = Assessment(
        name="Midterm · Q3", course_code=DEFAULT_COURSE_CODE, course_title=DEFAULT_COURSE_TITLE,
        question_text="Trace the operation and justify its result.",
        reference_answer="The answer shows the intermediate states and reaches the correct result.",
        max_score=15, rubric=[{"name": "Correct result", "description": "", "points": 8.0}, {"name": "Reasoning", "description": "", "points": 7.0}],
    )
    q2 = Assessment(
        name="Midterm · Q2", course_code=DEFAULT_COURSE_CODE, course_title=DEFAULT_COURSE_TITLE,
        question_text="Analyze the provided data-structure scenario.", reference_answer="A sound analysis supports the conclusion with a correct invariant.",
        max_score=20, rubric=[{"name": "Analysis", "description": "", "points": 12.0}, {"name": "Explanation", "description": "", "points": 8.0}],
    )
    lab = Assessment(
        name="Lab 2 · Binary search trees", course_code=DEFAULT_COURSE_CODE, course_title=DEFAULT_COURSE_TITLE,
        question_text="Implement a binary search tree operation.", reference_answer="The implementation preserves the binary search tree invariant.",
        max_score=100, rubric=[{"name": "Correctness", "description": "", "points": 70.0}, {"name": "Testing", "description": "", "points": 30.0}],
    )
    db.add_all([q4, q3, q2, lab])
    db.flush()
    now = utc_now()
    seed_submissions = [
        (q4, "CS204-018", "Alex Rivera", 18, 0.96, "Needs review", 0),
        (q4, "CS204-023", "Priya Sharma", 16, 0.78, "Needs review", 1),
        (q3, "CS204-031", "Jordan Miller", 12, 0.91, "Auto-graded", 2),
        (q2, "CS204-044", "Sam Kim", 14, 0.64, "Flagged", 3),
        (q4, "CS204-051", "Taylor Wong", 19, 0.98, "Auto-graded", 4),
    ]
    created = []
    for assessment, student_id, name, score, confidence, status, hours_ago in seed_submissions:
        if len(assessment.rubric) == 4:
            criteria = [
                _criterion_seed(round(score * 0.25, 2), 5, "Concept understanding", confidence),
                _criterion_seed(round(score * 0.30, 2), 6, "Problem-solving approach", confidence),
                _criterion_seed(round(score * 0.25, 2), 5, "Code correctness", confidence),
                _criterion_seed(round(score * 0.20, 2), 4, "Explanation & clarity", confidence),
            ]
        else:
            criteria = [
                _criterion_seed(round(score * 0.6, 2), assessment.rubric[0]["points"], assessment.rubric[0]["name"], confidence),
                _criterion_seed(round(score * 0.4, 2), assessment.rubric[1]["points"], assessment.rubric[1]["name"], confidence),
            ]
        submission = Submission(
            assessment_id=assessment.id, student_id=student_id, student_name=name,
            filename="sample-answer.pdf", submitted_at=now - timedelta(hours=hours_ago * 0.4 + 0.2),
            answer_text="Sample response used to populate the dashboard preview.",
            ai_score=float(score), final_score=float(score), confidence=confidence,
            status=status, feedback="Your solution is on the right track. Explain edge cases and complexity for full credit.",
            feedback_released=status == "Auto-graded", grading_details={"criterion_scores": criteria, "summary": "Seeded preview result.", "provider": "sample_data"},
        )
        db.add(submission)
        created.append(submission)
    db.flush()
    db.add_all([
        AuditLog(submission_id=created[1].id, actor="Dr. Maya Chen", action="SCORE_OVERRIDDEN", previous_score=14, new_score=16, reason="alternate valid approach", created_at=now - timedelta(minutes=25)),
        AuditLog(submission_id=created[0].id, actor="Dr. Maya Chen", action="FEEDBACK_EDITED", previous_score=18, new_score=18, reason="Comment added to criterion 2", created_at=now - timedelta(days=1)),
        AuditLog(submission_id=created[2].id, actor="Dr. Maya Chen", action="REVIEW_CONFIRMED", previous_score=12, new_score=12, reason="AI score retained", created_at=now - timedelta(days=2)),
    ])
    db.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    seed_setting = os.getenv("SEED_DEMO_DATA")
    if seed_setting is None:
        seed_setting = "true" if engine.dialect.name == "sqlite" else "false"
    if seed_setting.lower() not in {"0", "false", "no"}:
        with SessionLocal() as db:
            seed_demo_data(db)
    yield


app = FastAPI(title="GradeWise AI API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def assessment_payload(item: Assessment) -> dict:
    return {
        "id": item.id, "name": item.name, "course_code": item.course_code,
        "course_title": item.course_title, "question_text": item.question_text,
        "reference_answer": item.reference_answer, "max_score": item.max_score,
        "rubric": item.rubric, "created_at": item.created_at.isoformat() if item.created_at else None,
    }


def submission_payload(item: Submission, assessment: Assessment | None = None) -> dict:
    if assessment is None and hasattr(item, "assessment"):
        assessment = item.assessment
    return {
        "id": item.id, "assessment_id": item.assessment_id,
        "assessment": assessment.name if assessment else None,
        "student_id": item.student_id, "student_name": item.student_name,
        "filename": item.filename, "submitted_at": item.submitted_at.isoformat() if item.submitted_at else None,
        "score": item.final_score, "ai_score": item.ai_score, "max_score": assessment.max_score if assessment else None,
        "confidence": item.confidence, "confidence_percent": round(item.confidence * 100) if item.confidence is not None else None,
        "status": item.status, "feedback": item.feedback,
        "feedback_released": item.feedback_released,
        "criterion_scores": (item.grading_details or {}).get("criterion_scores", []),
        "grading_details": item.grading_details,
    }


def _get_assessment(db: Session, assessment_id: int) -> Assessment:
    assessment = db.get(Assessment, assessment_id)
    if not assessment:
        raise HTTPException(status_code=404, detail="Assessment not found.")
    return assessment


def _get_submission(db: Session, submission_id: int) -> tuple[Submission, Assessment]:
    submission = db.get(Submission, submission_id)
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found.")
    return submission, _get_assessment(db, submission.assessment_id)


def _percentage(score: float | None, max_score: float) -> float | None:
    return round(score / max_score * 100, 1) if score is not None and max_score else None


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "gradewise-ai"}


@app.get("/api/assessments")
def list_assessments(course_code: str | None = None, db: Session = Depends(get_db)):
    query = db.query(Assessment)
    if course_code:
        query = query.filter(Assessment.course_code == course_code)
    return [assessment_payload(item) for item in query.order_by(Assessment.created_at.desc()).all()]


@app.post("/api/assessments", status_code=201)
def create_assessment(body: AssessmentCreate, db: Session = Depends(get_db)):
    rubric = [item.model_dump() for item in body.rubric]
    if not rubric:
        rubric = [{"name": "Overall correctness", "description": "Add an answer key and detailed rubric before grading.", "points": body.max_score}]
    assessment = Assessment(
        name=body.name.strip(), course_code=body.course_code.strip(), course_title=body.course_title.strip(),
        question_text=body.question_text.strip(), reference_answer=body.reference_answer.strip(),
        max_score=body.max_score, rubric=rubric,
    )
    db.add(assessment)
    db.commit()
    db.refresh(assessment)
    return assessment_payload(assessment)


@app.get("/api/assessments/{assessment_id}")
def get_assessment(assessment_id: int, db: Session = Depends(get_db)):
    return assessment_payload(_get_assessment(db, assessment_id))


@app.post("/api/assessments/{assessment_id}/submissions", status_code=201)
async def upload_submission(
    assessment_id: int,
    file: Annotated[UploadFile, File(...)],
    student_name: Annotated[str, Form(min_length=1, max_length=200)],
    student_id: Annotated[str, Form(min_length=1, max_length=80)],
    db: Session = Depends(get_db),
):
    assessment = _get_assessment(db, assessment_id)
    filename = (file.filename or "answer.pdf").replace("\\", "/").split("/")[-1]
    if PurePath(filename).suffix.lower() != ".pdf":
        raise HTTPException(status_code=415, detail="Upload a PDF file.")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"PDF must be {MAX_UPLOAD_BYTES // (1024 * 1024)} MB or smaller.")
    if not data.startswith(b"%PDF-"):
        raise HTTPException(status_code=415, detail="The uploaded file is not a valid PDF.")
    try:
        answer_text, extraction_method = await run_in_threadpool(extract_pdf_text, data)
    except DocumentParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    submission = Submission(
        assessment_id=assessment.id, student_id=student_id.strip(), student_name=student_name.strip(),
        filename=filename[:255], answer_text=answer_text, status="Pending", grading_details={"extraction_method": extraction_method},
    )
    db.add(submission)
    db.commit()
    db.refresh(submission)
    return submission_payload(submission, assessment) | {"extraction_method": extraction_method}


@app.post("/api/submissions/{submission_id}/grade")
async def grade_submission(submission_id: int, db: Session = Depends(get_db)):
    submission, assessment = _get_submission(db, submission_id)
    if not assessment.question_text.strip() or not assessment.reference_answer.strip():
        raise HTTPException(status_code=422, detail="Add the question and reference answer before grading this assessment.")
    try:
        raw = await run_in_threadpool(
            evaluate_answer, assessment.question_text, assessment.reference_answer, submission.answer_text, assessment.rubric
        )
        result = score_evaluation(raw, assessment.rubric, assessment.max_score)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    submission.ai_score = result["score"]
    submission.final_score = result["score"]
    submission.confidence = result["confidence"]
    submission.status = result["status"]
    submission.feedback_released = False
    submission.feedback = generate_feedback(result, assessment.max_score)
    submission.grading_details = result | {"extraction_method": (submission.grading_details or {}).get("extraction_method")}
    db.add(AuditLog(
        submission_id=submission.id, actor="GradeWise AI", action="AI_GRADED",
        previous_score=None, new_score=result["score"], reason=f"Provider: {result['provider']}; confidence: {result['confidence']:.0%}.",
    ))
    db.commit()
    db.refresh(submission)
    return submission_payload(submission, assessment) | {"draft_feedback": submission.feedback}


@app.get("/api/submissions")
def list_submissions(
    course_code: str | None = None,
    assessment_id: int | None = None,
    status: str | None = None,
    search: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    query = db.query(Submission, Assessment).join(Assessment, Submission.assessment_id == Assessment.id)
    if course_code:
        query = query.filter(Assessment.course_code == course_code)
    if assessment_id:
        query = query.filter(Submission.assessment_id == assessment_id)
    if status:
        query = query.filter(Submission.status == status)
    if search:
        like = f"%{search.strip()}%"
        query = query.filter((Submission.student_name.ilike(like)) | (Submission.student_id.ilike(like)))
    rows = query.order_by(Submission.submitted_at.desc()).limit(limit).all()
    return [submission_payload(submission, assessment) for submission, assessment in rows]


@app.get("/api/submissions/{submission_id}")
def get_submission(submission_id: int, db: Session = Depends(get_db)):
    submission, assessment = _get_submission(db, submission_id)
    return submission_payload(submission, assessment) | {"answer_text": submission.answer_text}


@app.patch("/api/submissions/{submission_id}/review")
def review_submission(submission_id: int, body: SubmissionReview, db: Session = Depends(get_db)):
    submission, assessment = _get_submission(db, submission_id)
    if submission.ai_score is None:
        raise HTTPException(status_code=409, detail="Grade this submission before faculty review.")
    if body.score > assessment.max_score:
        raise HTTPException(status_code=422, detail=f"Score cannot exceed {assessment.max_score:g}.")
    previous = submission.final_score
    score_changed = previous is None or abs(previous - body.score) > 0.001
    submission.final_score = round(body.score, 2)
    if body.feedback is not None:
        submission.feedback = body.feedback.strip()
    submission.feedback_released = body.release
    submission.status = "Released" if body.release else "Reviewed"
    action = "SCORE_OVERRIDDEN" if score_changed else ("FEEDBACK_RELEASED" if body.release else "REVIEW_CONFIRMED")
    reason = body.reason.strip() or ("Faculty changed the score." if score_changed else "Faculty confirmed the AI score.")
    db.add(AuditLog(
        submission_id=submission.id, actor=body.reviewer.strip(), action=action,
        previous_score=previous, new_score=submission.final_score, reason=reason,
    ))
    db.commit()
    db.refresh(submission)
    return submission_payload(submission, assessment)


@app.get("/api/dashboard")
def dashboard(course_code: str = DEFAULT_COURSE_CODE, db: Session = Depends(get_db)):
    assessments = db.query(Assessment).filter(Assessment.course_code == course_code).order_by(Assessment.created_at.desc()).all()
    assessment_by_id = {item.id: item for item in assessments}
    submissions = db.query(Submission).filter(Submission.assessment_id.in_(assessment_by_id.keys())).order_by(Submission.submitted_at.desc()).all() if assessment_by_id else []
    graded = [item for item in submissions if item.final_score is not None]
    pending_review = [item for item in submissions if item.status in {"Needs review", "Flagged"}]
    average = round(sum(_percentage(item.final_score, assessment_by_id[item.assessment_id].max_score) or 0 for item in graded) / len(graded), 1) if graded else 0.0
    rubric_totals: dict[str, list[float]] = {}
    for submission in graded:
        for criterion in (submission.grading_details or {}).get("criterion_scores", []):
            points = float(criterion.get("points", 0) or 0)
            score = float(criterion.get("score", 0) or 0)
            if points:
                rubric_totals.setdefault(criterion["criterion"], []).append(score / points * 100)
    rubric_performance = [
        {"criterion": name, "average_percent": round(sum(values) / len(values), 1)}
        for name, values in rubric_totals.items()
    ]
    trend = []
    for assessment in reversed(assessments[:6]):
        scores = [item for item in graded if item.assessment_id == assessment.id]
        trend.append({
            "assessment_id": assessment.id, "assessment": assessment.name,
            "average_percent": round(sum(_percentage(item.final_score, assessment.max_score) or 0 for item in scores) / len(scores), 1) if scores else None,
            "submission_count": len(scores),
        })
    review_count = sum(item.status in {"Needs review", "Flagged"} for item in submissions)
    rows = db.query(AuditLog, Submission, Assessment).join(Submission, AuditLog.submission_id == Submission.id).join(Assessment, Submission.assessment_id == Assessment.id).filter(Assessment.course_code == course_code).order_by(AuditLog.created_at.desc()).limit(10).all()
    audit = [{
        "id": log.id, "student_name": submission.student_name, "student_id": submission.student_id,
        "assessment": assessment.name, "actor": log.actor, "action": log.action,
        "previous_score": log.previous_score, "new_score": log.new_score, "reason": log.reason,
        "created_at": log.created_at.isoformat(),
    } for log, submission, assessment in rows]
    return {
        "course_code": course_code,
        "course_title": assessments[0].course_title if assessments else DEFAULT_COURSE_TITLE,
        "summary": {
            "enrolled_students": ENROLLED_STUDENTS,
            "submission_count": len(submissions),
            "to_be_reviewed": review_count,
            "flagged_count": sum(item.status == "Flagged" for item in submissions),
            "average_score_percent": average,
            "grading_completed_percent": round(len(graded) / len(submissions) * 100) if submissions else 0,
        },
        "assessments": [assessment_payload(item) for item in assessments],
        "recent_submissions": [submission_payload(item, assessment_by_id[item.assessment_id]) for item in submissions[:25]],
        "review_queue": [submission_payload(item, assessment_by_id[item.assessment_id]) for item in pending_review],
        "rubric_performance": rubric_performance,
        "performance_trend": trend,
        "audit_log": audit,
    }


@app.get("/api/students/{student_id}/dashboard")
def student_dashboard(student_id: str, course_code: str = DEFAULT_COURSE_CODE, db: Session = Depends(get_db)):
    rows = db.query(Submission, Assessment).join(Assessment, Submission.assessment_id == Assessment.id).filter(
        Submission.student_id == student_id, Assessment.course_code == course_code, Submission.feedback_released.is_(True)
    ).order_by(Submission.submitted_at.desc()).all()
    if not rows:
        return {"student_id": student_id, "course_code": course_code, "results": [], "assessment_count": 0, "current_grade_percent": None}
    percent_scores = [_percentage(sub.final_score, assessment.max_score) for sub, assessment in rows]
    values = [value for value in percent_scores if value is not None]
    return {
        "student_id": student_id, "course_code": course_code,
        "student_name": rows[0][0].student_name,
        "assessment_count": len(rows),
        "current_grade_percent": round(sum(values) / len(values), 1) if values else None,
        "results": [submission_payload(submission, assessment) for submission, assessment in rows],
    }


@app.get("/api/reports.csv")
def export_report(course_code: str = DEFAULT_COURSE_CODE, db: Session = Depends(get_db)):
    rows = db.query(Submission, Assessment).join(Assessment, Submission.assessment_id == Assessment.id).filter(
        Assessment.course_code == course_code
    ).order_by(Submission.submitted_at.desc()).all()
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["Student", "Student ID", "Assessment", "Submitted", "AI score", "Final score", "Max score", "Confidence", "Status", "Feedback released"])
    for submission, assessment in rows:
        writer.writerow([
            submission.student_name, submission.student_id, assessment.name,
            submission.submitted_at.isoformat() if submission.submitted_at else "",
            submission.ai_score if submission.ai_score is not None else "",
            submission.final_score if submission.final_score is not None else "", assessment.max_score,
            f"{submission.confidence:.0%}" if submission.confidence is not None else "",
            submission.status, "Yes" if submission.feedback_released else "No",
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="gradewise-{course_code}-report.csv"'},
    )
