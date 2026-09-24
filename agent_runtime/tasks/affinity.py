from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Protocol


class TaskLike(Protocol):
    task_type: str
    required_roles: Iterable[str]
    created_at: str | None


_TASK_TYPE_ALIASES = {
    "general": "general",
    "generic": "general",
    "math": "math",
    "mathematics": "math",
    "mathematical": "math",
    "proof": "math",
    "pde": "math",
    "analysis": "math",
    "engineering": "engineering",
    "software": "engineering",
    "code": "engineering",
    "coding": "engineering",
    "runtime": "engineering",
    "infrastructure": "engineering",
    "literature": "literature",
    "bibliography": "literature",
    "survey": "literature",
    "arxiv": "literature",
}


def canonical_task_type(task_type: str | None) -> str:
    value = (task_type or "general").strip().lower()
    return _TASK_TYPE_ALIASES.get(value, value)


def _words(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.lower()))


def infer_worker_categories(role: str) -> frozenset[str]:
    words = _words(role)
    categories: set[str] = set()

    if words & {
        "math", "mathematical", "mathematician", "proof", "pde",
        "analyst", "analysis", "reviewer", "referee",
    }:
        categories.add("math")

    if words & {
        "engineer", "engineering", "developer", "programmer", "coding",
        "software", "frontend", "backend", "runtime", "infrastructure",
    }:
        categories.add("engineering")

    if words & {"literature", "bibliography", "survey", "arxiv", "librarian"}:
        categories.add("literature")

    if words & {"general", "generalist", "assistant"}:
        categories.add("general")

    return frozenset(categories)


def role_requirement_matches(worker_role: str, required_role: str) -> bool:
    """Match an explicit role requirement without fuzzy/embedding retrieval.

    Exact normalized phrases match first. For descriptive role strings such as
    ``Senior Mathematical Reviewer & Journal Referee``, a requirement also
    matches when all meaningful requirement words occur in the worker role.
    """
    worker_words = _words(worker_role)
    required_words = _words(required_role)
    if not required_words:
        return False
    return required_words <= worker_words or worker_words <= required_words


def affinity_score(task: TaskLike, worker_role: str) -> int | None:
    """Return an auto-claim score, or ``None`` when the task is ineligible.

    Explicit ``required_roles`` are authoritative. Untyped/general tasks are
    deliberately *not* auto-claimed by specialized workers; they remain
    available for explicit/manual claim by the lead agent.
    """
    required_roles = tuple(
        role.strip() for role in task.required_roles if str(role).strip()
    )
    explicit_score = 0
    if required_roles:
        if not any(
            role_requirement_matches(worker_role, required)
            for required in required_roles
        ):
            return None
        explicit_score = 100

    task_type = canonical_task_type(task.task_type)
    worker_categories = infer_worker_categories(worker_role)

    if task_type == "general":
        if explicit_score:
            return explicit_score
        if "general" in worker_categories:
            return 10
        return None

    if task_type in worker_categories:
        return explicit_score + 50

    # An explicit role requirement wins over the coarse task-type category.
    if explicit_score:
        return explicit_score
    return None


def task_matches_worker(task: TaskLike, worker_role: str) -> bool:
    return affinity_score(task, worker_role) is not None


def select_tasks_for_worker(tasks: Iterable[TaskLike], worker_role: str) -> list[TaskLike]:
    scored: list[tuple[int, str, TaskLike]] = []
    for task in tasks:
        score = affinity_score(task, worker_role)
        if score is None:
            continue
        created_at = task.created_at or ""
        scored.append((score, created_at, task))

    # Prefer explicit-role matches, then task-type matches; preserve oldest-first
    # behavior within the same affinity score.
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in scored]
