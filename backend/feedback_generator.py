from __future__ import annotations

from typing import Any


def generate_feedback(result: dict[str, Any], max_score: float) -> str:
    criteria = result.get("criterion_scores", [])
    strengths = [item["criterion"] for item in criteria if item["points"] and item["score"] / item["points"] >= 0.8]
    focus = [item for item in criteria if item["points"] and item["score"] / item["points"] < 0.8]
    parts = [f"You earned {result['score']:g} of {max_score:g} points."]
    if strengths:
        parts.append("Strong work on " + ", ".join(strengths) + ".")
    if focus:
        details = [
            f"{item['criterion']}: {item['feedback'] or item['evidence'] or 'review this criterion'}"
            for item in focus
        ]
        parts.append("To improve, focus on " + " ".join(details))
    elif not strengths:
        parts.append("Review the criterion notes below and add evidence for each required concept.")
    if result.get("provider") == "demo_heuristic":
        parts.append("This is a low-confidence demo estimate and needs faculty review.")
    return " ".join(parts)
