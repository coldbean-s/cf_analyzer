"""Fetch Codeforces user submissions via plain requests (no browser needed for user.status)."""

import json
from datetime import datetime, timezone

import requests

CF_API = "https://codeforces.com/api"


def fetch_user_submissions(handle: str, count: int) -> list[dict]:
    """Fetch the latest `count` submissions for `handle` from CF API.

    CF user.status is public and does not require auth/browser.
    Returns raw CF submission objects.
    """
    resp = requests.get(
        f"{CF_API}/user.status",
        params={"handle": handle, "from": 1, "count": count},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data["status"] != "OK":
        raise RuntimeError(f"CF API error: {data.get('comment', '?')}")
    return data["result"]


def normalize_submission(raw: dict, handle: str) -> dict:
    """Convert a raw CF submission object to our DB schema dict."""
    problem = raw.get("problem", {})
    tags = json.dumps(problem.get("tags", []), ensure_ascii=False)
    rating = problem.get("rating")

    # CF timestamps are Unix seconds in UTC
    ts = raw.get("creationTimeSeconds", 0)
    submitted_at = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "id": raw["id"],
        "handle": handle,
        "contest_id": raw.get("contestId", 0),
        "problem_index": problem.get("index", "?"),
        "problem_name": problem.get("name", ""),
        "rating": rating,
        "tags": tags,
        "verdict": raw.get("verdict", "UNKNOWN"),
        "language": raw.get("programmingLanguage", ""),
        "submitted_at": submitted_at,
    }


def fetch_and_normalize(handle: str, count: int) -> list[dict]:
    raws = fetch_user_submissions(handle, count)
    return [normalize_submission(r, handle) for r in raws
            if r.get("contestId", 0) < 100000]
