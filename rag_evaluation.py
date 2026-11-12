"""Repeatable acceptance checks for source-backed interview retrieval."""
from __future__ import annotations

from collections.abc import Callable


def evaluate_cases(cases: list[dict], builder: Callable[[str, str, dict], list[dict]]) -> dict:
    """Evaluate only observable RAG contract, never raw resume content."""
    results = []
    for case in cases:
        plan = builder(case["role"], case.get("mode", "normal"), case.get("profile") or {}) or []
        issues = []
        if not plan:
            issues.append("no_source_backed_plan")
        elif not all(item.get("sources") for item in plan):
            issues.append("missing_source_evidence")
        if plan and not all((item.get("retrieval") or {}).get("retrieval_mode") in {"keyword", "hybrid"} for item in plan):
            issues.append("missing_retrieval_evidence")
        if plan and not plan[0].get("opening_personalized"):
            issues.append("opening_not_personalized")
        results.append({
            "id": case["id"],
            "role": case["role"],
            "question_count": len(plan),
            "issues": issues,
            "passed": not issues,
        })
    passed = sum(result["passed"] for result in results)
    return {
        "passed": passed == len(results),
        "summary": {"total": len(results), "passed": passed, "failed": len(results) - passed},
        "cases": results,
    }
