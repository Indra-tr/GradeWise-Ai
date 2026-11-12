from __future__ import annotations

from typing import Any


def score_evaluation(raw: dict[str, Any], rubric: list[dict], max_score: float) -> dict[str, Any]:
    """Clamp model output to the rubric and calculate a reproducible partial-credit total."""
    raw_scores = raw.get("criterion_scores")
    if not isinstance(raw_scores, list):
        raise ValueError("Evaluator response is missing criterion_scores.")
    by_name = {str(item.get("criterion", "")).strip().casefold(): item for item in raw_scores if isinstance(item, dict)}
    scored = []
    confidences = []
    for criterion in rubric:
        name = criterion["name"]
        points = float(criterion["points"])
        item = by_name.get(name.casefold())
        if item is None:
            raise ValueError(f"Evaluator response is missing rubric criterion: {name}.")
        try:
            earned = float(item.get("score", 0))
            confidence = float(item.get("confidence", raw.get("overall_confidence", 0.5)))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Evaluator returned an invalid score for {name}.") from exc
        earned = round(max(0.0, min(points, earned)), 2)
        confidence = max(0.0, min(1.0, confidence))
        confidences.append(confidence)
        scored.append({
            "criterion": name,
            "points": points,
            "score": earned,
            "confidence": round(confidence, 3),
            "evidence": str(item.get("evidence", ""))[:1200],
            "feedback": str(item.get("feedback", ""))[:1200],
        })

    total = round(sum(item["score"] for item in scored), 2)
    if total > max_score:
        total = round(max_score, 2)
    model_confidence = raw.get("overall_confidence")
    try:
        confidence = float(model_confidence) if model_confidence is not None else sum(confidences) / len(confidences)
    except (TypeError, ValueError):
        confidence = sum(confidences) / len(confidences)
    confidence = round(max(0.0, min(1.0, confidence)), 3)

    if confidence < 0.62:
        status = "Flagged"
    elif confidence >= 0.88:
        status = "Auto-graded"
    else:
        status = "Needs review"

    return {
        "score": total,
        "confidence": confidence,
        "status": status,
        "criterion_scores": scored,
        "summary": str(raw.get("summary", ""))[:2000],
        "provider": str(raw.get("provider", "unknown")),
    }
