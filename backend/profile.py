"""Privacy-minimized candidate profile construction for RAG prompts."""
from __future__ import annotations

import re

MAX_ITEMS = 8
MAX_PROJECTS = 5
MAX_TEXT = 120

SKILL_KEYWORDS = [
    "Python", "Java", "Go", "C++", "JavaScript", "TypeScript", "React", "Vue",
    "FastAPI", "Django", "Spring", "SQL", "MySQL", "PostgreSQL", "Redis", "Kafka",
    "Docker", "Kubernetes", "LLM", "RAG", "Agent", "A/B 测试", "用户增长", "数据分析",
    "指标体系", "运营", "内容策略", "项目管理",
]

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d[-\s]?\d{4}[-\s]?\d{4}(?!\d)")
# Resume contacts commonly omit a scheme (for example github.com/name).  The
# trailing punctuation set intentionally leaves the surrounding career text in
# place after a URL has been removed.
_URL_RE = re.compile(
    r"(?:https?://|www\.)[^\s,，;；、]+|"
    r"(?:github\.com|linkedin\.com|gitee\.com|[A-Za-z0-9-]+\.(?:com|cn|net|org|io|me|dev))"
    r"(?:/[^\s,，;；、]*)?",
    re.IGNORECASE,
)


def redact_contact_details(value: object, limit: int | None = None) -> str:
    """Remove direct contact identifiers before any external model call.

    This intentionally retains surrounding career evidence, unlike `_items`,
    where a single list item is small enough to discard as a whole.
    """
    text = str(value or "")
    text = _EMAIL_RE.sub("", text)
    text = _PHONE_RE.sub("", text)
    text = _URL_RE.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text[:limit].strip() if limit else text.strip()


def _text(value: object, limit: int = MAX_TEXT) -> str:
    text = redact_contact_details(value)
    return re.sub(r"\s+", " ", text).strip(" ，,;；|/")[:limit]


def _items(value: object, limit: int = MAX_ITEMS) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        raw = str(item or "")
        # A list entry is intended to be one semantic fact (skill, outcome,
        # responsibility, etc.). If it contains contact data or a URL, keeping
        # the remainder can still retain an unnecessary identifying clue. Drop
        # that whole entry instead of trying to salvage a fragment.
        if _EMAIL_RE.search(raw) or _PHONE_RE.search(raw) or _URL_RE.search(raw):
            continue
        text = _text(item)
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _project(value: object) -> dict | None:
    if isinstance(value, str):
        value = {"name": value}
    if not isinstance(value, dict):
        return None
    name = _text(value.get("name") or value.get("project") or value.get("summary"))
    if not name:
        return None
    return {
        "name": name,
        "domain": _text(value.get("domain")),
        "stack": _items(value.get("stack") or value.get("skills")),
        "responsibilities": _items(value.get("responsibilities")),
        "outcomes": _items(value.get("outcomes")),
        "evidence_gaps": _items(value.get("evidence_gaps") or value.get("risks")),
    }


def normalize_profile(data: dict | None, target_role: str) -> dict:
    """Return only bounded fields safe to use for retrieval and LLM prompts."""
    data = data if isinstance(data, dict) else {}
    projects = [_project(item) for item in data.get("projects", [])]
    projects = [item for item in projects if item][:MAX_PROJECTS]
    summary = _text(data.get("summary"))
    if any(pattern.search(str(data.get("summary") or "")) for pattern in (_EMAIL_RE, _PHONE_RE, _URL_RE)):
        summary = ""
    return {
        "target_role": _text(target_role, 80),
        "projects": projects,
        "skills": _items(data.get("skills")),
        "strengths": _items(data.get("strengths")),
        "risks": _items(data.get("risks")),
        "summary": summary,
        "parsed_by": "llm" if data.get("parsed_by") == "llm" else "heuristic",
    }


def project_names(candidate_profile: dict) -> list[str]:
    """Compatibility view for planner code: projects are now structured cards."""
    names = []
    for project in (candidate_profile or {}).get("projects") or []:
        name = _text(project.get("name")) if isinstance(project, dict) else _text(project)
        if name:
            names.append(name)
    return names


def _heuristic_project_name(project_text: str) -> str:
    """Extract a compact project label from a free-form resume sentence.

    Raw resume sentences remain in the responsibility card, while an opening
    question gets a human-readable project name instead of repeating a clause.
    """
    text = _text(project_text)
    if not text:
        return ""
    patterns = (
        r"(?:我(?:做过|参与过|负责过)|参与(?:了)?|负责(?:了)?)?([\u4e00-\u9fffA-Za-z0-9+/#.-]{2,30}(?:项目|平台|系统|助手|工具|产品|应用))",
        r"([\u4e00-\u9fffA-Za-z0-9+/#.-]{2,30})(?=[，,：:；;])",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        name = match.group(1).strip("，,：:；; ")
        name = re.sub(r"^(?:我(?:做过|参与过|负责过)|参与(?:了)?|负责(?:了)?)", "", name)
        if len(name) >= 2:
            return name[:40]
    return text[:40]


def heuristic_profile(resume_text: str, target_role: str) -> dict:
    """Construct a small profile without retaining or embedding the full resume."""
    text = resume_text or ""
    skills = [skill for skill in SKILL_KEYWORDS if skill.lower() in text.lower()]
    lines = [_text(line) for line in re.split(r"[\n。]", text) if _text(line)]
    candidates = [line for line in lines if any(word in line for word in ("项目", "平台", "系统", "负责", "实习"))]
    project_text = candidates[0] if candidates else _text(text)
    project_name = _heuristic_project_name(project_text)
    outcomes = re.findall(r"[^，。；]{0,60}\d+(?:\.\d+)?%[^，。；]{0,60}", project_text)
    gaps = [] if outcomes else ["项目成果缺少可量化结果或验证方法"]
    return normalize_profile(
        {
            "skills": skills,
            "projects": [{
                "name": project_name,
                "stack": skills,
                "responsibilities": [project_text] if project_text else [],
                "outcomes": outcomes,
                "evidence_gaps": gaps,
            }] if project_text else [],
            "strengths": [f"具备 {skill} 实践线索" for skill in skills[:3]],
            "risks": gaps,
            "summary": _text(project_text or f"目标岗位：{target_role}"),
        },
        target_role,
    )
