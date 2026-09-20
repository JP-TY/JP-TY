#!/usr/bin/env python3
"""Generate accurate GitHub stats + streak SVGs for the JP-TY profile README.

Why this exists: third-party demo instances (readme-stats-*.vercel.app,
streak-stats.demolab.com) have no access to private repos, hit shared rate
limits, and compute streaks in UTC. This script queries the GitHub GraphQL
API directly with your own token, evaluates the streak in Asia/Manila, and
writes theme-matched SVGs into assets/ so the README embeds local images.

Usage:
    GH_STATS_TOKEN=<pat-with-repo-scope> python3 scripts/github_stats.py
    # falls back to GH_TOKEN / GITHUB_TOKEN (public-only if no private scope)

Output:
    assets/stats.svg   - stars / commits / PRs / issues / contributed-to
    assets/streak.svg  - total contributions / current + longest streak
    assets/stats.json  - machine-readable numbers + provenance

Stdlib only (urllib) so the GitHub Action needs no pip install.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.request
from zoneinfo import ZoneInfo

LOGIN = os.environ.get("GITHUB_LOGIN", "JP-TY")
TOKEN = (
    os.environ.get("GH_STATS_TOKEN")
    or os.environ.get("GH_TOKEN")
    or os.environ.get("GITHUB_TOKEN")
    or ""
)
INCLUDE_PRIVATE = os.environ.get("INCLUDE_PRIVATE", "true").lower() not in ("0", "false", "no")
TZ = ZoneInfo(os.environ.get("STATS_TZ", "Asia/Manila"))
ACCOUNT_CREATED = dt.date(2026, 1, 10)

# Theme matched to README (amber-on-ink)
BG = "#0A0C10"
BORDER = "#3A2F18"
AMBER = "#F5B544"
CREAM = "#F5ECD7"
MUTED = "#B3A67F"
DIM = "#6B6353"

GRAPHQL_URL = "https://api.github.com/graphql"
REST_URL = "https://api.github.com"


def _req(url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {
        "User-Agent": "jp-ty-stats/1.0",
        "Accept": "application/vnd.github+json",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def graphql(query: str, variables: dict) -> dict:
    out = _req(GRAPHQL_URL, {"query": query, "variables": variables})
    if "errors" in out:
        raise RuntimeError(f"GraphQL errors: {out['errors']}")
    return out["data"]


COLLECTION_QUERY = """
query($login: String!, $from: DateTime, $to: DateTime) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      restrictedContributionsCount
      totalPullRequestContributions
      totalIssueContributions
      totalPullRequestReviewContributions
      totalRepositoriesWithContributedCommits
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}
"""


def year_windows() -> list[tuple[dt.datetime, dt.datetime]]:
    today = dt.datetime.now(TZ).date()
    windows = []
    for year in range(ACCOUNT_CREATED.year, today.year + 1):
        start = dt.date(year, 1, 1)
        end = dt.date(year, 12, 31)
        if start < ACCOUNT_CREATED - dt.timedelta(days=7):
            start = ACCOUNT_CREATED - dt.timedelta(days=7)
        if end > today + dt.timedelta(days=1):
            end = today + dt.timedelta(days=1)
        windows.append((
            dt.datetime.combine(start, dt.time.min, tzinfo=dt.timezone.utc),
            dt.datetime.combine(end, dt.time.max, tzinfo=dt.timezone.utc),
        ))
    return windows


def fetch_stats() -> dict:
    days: dict[str, int] = {}
    commits = prs = issues = reviews = 0
    contributed_to_last_year = 0
    calendar_total = 0

    for start, end in year_windows():
        data = graphql(COLLECTION_QUERY, {
            "login": LOGIN,
            "from": start.isoformat(),
            "to": end.isoformat(),
        })
        cc = data["user"]["contributionsCollection"]
        commits += cc["totalCommitContributions"] or 0
        prs += cc["totalPullRequestContributions"] or 0
        issues += cc["totalIssueContributions"] or 0
        reviews += cc["totalPullRequestReviewContributions"] or 0
        # "Contributed to" is a trailing-year concept; keep the latest window.
        contributed_to_last_year = cc["totalRepositoriesWithContributedCommits"] or 0
        for week in cc["contributionCalendar"]["weeks"]:
            for d in week["contributionDays"]:
                days[d["date"]] = days.get(d["date"], 0) + d["contributionCount"]
    calendar_total = sum(days.values())
    stars = fetch_stars()
    streak = compute_streak(days)
    return {
        "login": LOGIN,
        "total_commits": commits,
        "total_contributions": calendar_total,
        "total_prs": prs,
        "total_issues": issues,
        "total_reviews": reviews,
        "contributed_to": contributed_to_last_year,
        "total_stars": stars,
        **streak,
    }


def fetch_stars() -> int:
    """Sum stargazers across owned repos (paginated, token-aware)."""
    stars = 0
    url = f"{REST_URL}/users/{LOGIN}/repos?per_page=100&page=1"
    while url:
        headers = {"User-Agent": "jp-ty-stats/1.0", "Accept": "application/vnd.github+json"}
        if TOKEN:
            headers["Authorization"] = f"Bearer {TOKEN}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            repos = json.loads(resp.read().decode())
            stars += sum(r.get("stargazers_count", 0) or 0 for r in repos)
            link = resp.headers.get("Link", "")
            nxt = None
            for part in link.split(","):
                if 'rel="next"' in part:
                    nxt = part.split(";")[0].strip().strip("<>")
            url = nxt
    return stars


def compute_streak(days: dict[str, int]) -> dict:
    """Current + longest streak evaluated with Asia/Manila day boundaries."""
    today = dt.datetime.now(TZ).date()
    get = lambda d: days.get(d.isoformat(), 0)

    # Streak is "alive" if today or yesterday has activity.
    end = today if get(today) > 0 else today - dt.timedelta(days=1)
    current_len = 0
    cur = end
    while get(cur) > 0:
        current_len += 1
        cur -= dt.timedelta(days=1)
    current_start = end - dt.timedelta(days=current_len - 1) if current_len else None

    # Longest run over the whole history.
    ordered = sorted(days)
    best_len, best_start, best_end = 0, None, None
    run_len, run_start = 0, None
    prev = None
    for iso in ordered:
        d = dt.date.fromisoformat(iso)
        if days[iso] > 0 and (prev is None or d == prev + dt.timedelta(days=1)):
            run_len += 1
            run_start = run_start or d
        elif days[iso] > 0:
            run_len, run_start = 1, d
        else:
            if run_len > best_len:
                best_len, best_start, best_end = run_len, run_start, prev
            run_len, run_start = 0, None
        prev = d
    if run_len > best_len:
        best_len, best_start, best_end = run_len, run_start, prev

    fmt = lambda d: d.strftime("%b %-d") if d else "—"
    rng = lambda a, b: f"{fmt(a)} - {fmt(b)}" if a and b else "—"
    # Total range label: first active day -> last active day
    active = [dt.date.fromisoformat(k) for k, v in days.items() if v > 0]
    total_range = rng(min(active), end) if active else "—"
    return {
        "current_streak": current_len,
        "current_range": rng(current_start, end) if current_len else "No streak",
        "longest_streak": best_len,
        "longest_range": rng(best_start, best_end) if best_len else "—",
        "total_range": total_range,
        "streak_alive_today": get(today) > 0,
    }


def esc(s: object) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def stats_svg(s: dict, updated: str) -> str:
    rows = [
        ("★", "Total Stars", s["total_stars"]),
        ("⛁", "Total Commits", s["total_commits"]),
        ("⑂", "Total PRs", s["total_prs"]),
        ("◉", "Total Issues", s["total_issues"]),
        ("▦", "Repos with commits (2026)", s["contributed_to"]),
    ]
    row_h, top = 26, 78
    row_svg = ""
    for i, (icon, label, val) in enumerate(rows):
        y = top + i * row_h
        row_svg += (
            f'<text x="28" y="{y}" font-family="Segoe UI, Ubuntu, sans-serif" '
            f'font-size="13" fill="{AMBER}">{icon}</text>'
            f'<text x="52" y="{y}" font-family="Segoe UI, Ubuntu, sans-serif" '
            f'font-size="13" fill="{CREAM}">{esc(label)}</text>'
            f'<text x="439" y="{y}" text-anchor="end" font-family="Segoe UI, Ubuntu, sans-serif" '
            f'font-size="13" font-weight="700" fill="{CREAM}">{esc(val)}</text>'
        )
    scope = "incl. private" if INCLUDE_PRIVATE and TOKEN else "public only"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="467" height="248" viewBox="0 0 467 248" role="img">
<title>{esc(LOGIN)}'s GitHub Stats: {s["total_commits"]} commits, {s["total_contributions"]} contributions</title>
<rect x="0.5" y="0.5" width="466" height="247" rx="4.5" fill="{BG}" stroke="{BORDER}"/>
<text x="25" y="34" font-family="Segoe UI, Ubuntu, sans-serif" font-size="17" font-weight="600" fill="{AMBER}">{esc(LOGIN)}'s GitHub Stats</text>
<text x="442" y="34" text-anchor="end" font-family="Segoe UI, Ubuntu, sans-serif" font-size="11" fill="{DIM}">{esc(scope)}</text>
{row_svg}
<text x="25" y="232" font-family="Segoe UI, Ubuntu, sans-serif" font-size="11" fill="{DIM}">Updated {esc(updated)} (PHT) · {esc(s["total_contributions"])} contributions {esc(s["total_range"])} · generated, not estimated</text>
</svg>
"""


def streak_svg(s: dict, updated: str) -> str:
    def col(x: int, big: object, label: str, sub: str) -> str:
        return (
            f"<g transform='translate({x}, 28)'>"
            f"<text x='0' y='34' text-anchor='middle' font-family='Segoe UI, Ubuntu, sans-serif' "
            f"font-size='28' font-weight='700' fill='{CREAM}'>{esc(big)}</text>"
            f"<text x='0' y='62' text-anchor='middle' font-family='Segoe UI, Ubuntu, sans-serif' "
            f"font-size='14' fill='{CREAM}'>{esc(label)}</text>"
            f"<text x='0' y='84' text-anchor='middle' font-family='Segoe UI, Ubuntu, sans-serif' "
            f"font-size='12' fill='{MUTED}'>{esc(sub)}</text>"
            f"</g>"
        )

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="495" height="195" viewBox="0 0 495 195" role="img">
<title>{esc(LOGIN)} streak: {s["current_streak"]} days (longest {s["longest_streak"]})</title>
<rect x="0.5" y="0.5" width="494" height="194" rx="4.5" fill="{BG}" stroke="{BORDER}"/>
<line x1="165" y1="28" x2="165" y2="170" stroke="{BORDER}"/>
<line x1="330" y1="28" x2="330" y2="170" stroke="{BORDER}"/>
{col(82, s["total_contributions"], "Total Contributions", s["total_range"])}
{col(247, s["current_streak"], "Current Streak", s["current_range"])}
{col(412, s["longest_streak"], "Longest Streak", s["longest_range"])}
<text x="247.5" y="186" text-anchor="middle" font-family="Segoe UI, Ubuntu, sans-serif" font-size="10" fill="{DIM}">Updated {esc(updated)} (PHT, Asia/Manila) · private included · self-generated</text>
</svg>
"""


def main() -> int:
    if not TOKEN:
        print("warning: no token in env (GH_STATS_TOKEN/GH_TOKEN/GITHUB_TOKEN); "
              "counts will be public-only and rate-limited", file=sys.stderr)
    stats = fetch_stats()
    updated = dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    stats["updated_pht"] = updated
    stats["include_private"] = bool(INCLUDE_PRIVATE and TOKEN)
    stats["timezone"] = "Asia/Manila"

    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "")
    assets = os.path.join(root, "assets")
    os.makedirs(assets, exist_ok=True)
    with open(os.path.join(assets, "stats.svg"), "w") as f:
        f.write(stats_svg(stats, updated))
    with open(os.path.join(assets, "streak.svg"), "w") as f:
        f.write(streak_svg(stats, updated))
    with open(os.path.join(assets, "stats.json"), "w") as f:
        json.dump(stats, f, indent=2)
    print(json.dumps(stats, indent=2))
    print(f"wrote assets/stats.svg, assets/streak.svg, assets/stats.json ({updated} PHT)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
