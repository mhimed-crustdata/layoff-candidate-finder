#!/usr/bin/env python3
"""Layoff Candidate Finder.

Reads layoff news, finds the people who recently left each company that
announced cuts, keeps the ones whose role and seniority fit your open jobs,
and writes a short note per candidate that names the exact role.

Data layer: Crustdata POST /web/search/live and POST /person/search
(https://docs.crustdata.com).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
from collections import Counter
from typing import Any, Dict, List, Optional

from crustdata_api import Crustdata, day, load_env
from notify import ask_claude, claude_available, send_slack

# several queries separated by ";" (one web search each)
DEFAULT_QUERY = "layoffs announced company cuts jobs; company to lay off employees workforce reduction"

# "Kroger to lay off 3,000", "Netflix is laying off 6%", "American Express to Cut 3,500 Jobs"
_VERB = r"(?:lay(?:ing|s)?\s*off|layoffs?|cuts?|cutting|slash(?:es|ing)?|eliminat(?:es|ing)|axes|axing)"
HEADLINE_PATTERNS = [
    re.compile(r"^(?P<c>.+?)\s+(?:(?:is|are|will|plans?|set|to|announces?|confirms?|begins?|starts?)\s+)+" + _VERB + r"\b", re.I),
    re.compile(r"^(?P<c>.+?)\s+(?:lays\s+off|layoffs?|job\s+cuts)\b", re.I),
]
GENERIC_FIRST_WORDS = {
    "tech", "companies", "company", "layoffs", "layoff", "job", "jobs", "firms", "why", "how", "what", "here",
    "more", "another", "big", "major", "the", "these", "more", "workers", "employees", "us", "new", "more",
    "latest", "this", "mass", "wave", "round", "ai", "breaking", "report", "live", "update", "list",
}

SENIORITY_LEVELS = ["Entry Level", "Entry Level Manager", "Senior", "Experienced Manager", "Director",
                    "Vice President", "CXO", "Strategic", "Owner / Partner", "In Training"]

PERSON_FIELDS = [
    "crustdata_person_id",
    "basic_profile.name",
    "basic_profile.current_title",
    "social_handles.professional_network_identifier",
    "experience.employment_details.past",
    "experience.employment_details.current",
]


# --- step 1: layoff news ---------------------------------------------------

def company_from_headline(title: str) -> Optional[str]:
    """Pull the company name out of a layoff headline, or None when it is a roundup."""
    title = re.sub(r"^(breaking|exclusive|update|report)\s*:\s*", "", (title or "").strip(), flags=re.I)
    for pattern in HEADLINE_PATTERNS:
        m = pattern.match(title)
        if not m:
            continue
        name = m.group("c").strip(" ,:-")
        words = name.split()
        if not 1 <= len(words) <= 4 or "," in name or ":" in name or not name[0].isupper():
            continue
        if words[0].lower() in GENERIC_FIRST_WORDS or words[0][0].isdigit():
            continue
        return name
    return None


def find_layoff_news(client: Crustdata, query: str, days: int, limit: int = 30) -> List[Dict[str, Any]]:
    now = int(time.time())
    results: List[Dict[str, Any]] = []
    for q in [x.strip() for x in query.split(";") if x.strip()]:
        results.extend(client.web_search(q, start_ts=now - days * 86400, end_ts=now, sources=["news"])[:limit])
    out: List[Dict[str, Any]] = []
    seen = set()
    for r in results:
        name = company_from_headline(r.get("title", ""))
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append({"company": name, "headline": r.get("title", ""), "url": r.get("url", "")})
    return out


# --- step 2: people who recently left ----------------------------------------

def month_start(date_iso: str) -> str:
    """Role dates are stored per month, so compare on the first of the month."""
    return (date_iso[:7] + "-01") if date_iso else ""


def leaver_filters(company: str, since: str, seniority: Optional[List[str]] = None) -> Dict[str, Any]:
    """Past employer + a role that ended since `since`, narrowed to the seniority levels your jobs need.

    The seniority condition sits on the same `past.*` role as the company name. The end-date
    condition can match any role of the person, so `recent_exit` re-checks it in code.
    """
    conds: List[Dict[str, Any]] = [
        {"field": "experience.employment_details.past.company_name", "type": "=", "value": company},
        {"field": "experience.employment_details.end_date", "type": "=>", "value": month_start(since)},
    ]
    if seniority:
        conds.append({"field": "experience.employment_details.past.seniority_level", "type": "in", "value": seniority})
    return {"op": "and", "conditions": conds}


def _same_company(a: str, b: str) -> bool:
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    return bool(a) and a == b


def recent_exit(profile: Dict[str, Any], company: str, since: str) -> Optional[Dict[str, Any]]:
    """Re-check on ONE role: the past role at `company` ended since `since` and nothing there is current.

    The search filters can match different roles of the same person, so this is checked again here.
    Returns the role, or None. Also sets role["landed"] when a newer job already started elsewhere.
    """
    emp = (profile.get("experience") or {}).get("employment_details") or {}
    if any(_same_company(c.get("name"), company) for c in emp.get("current") or []):
        return None
    best = None
    for r in emp.get("past") or []:
        ended = day(r.get("end_date"))
        if _same_company(r.get("name"), company) and ended and ended >= month_start(since):
            if best is None or ended > day(best.get("end_date")):
                best = r
    if best is None:
        return None
    left = day(best.get("end_date"))
    landed = any(day(c.get("start_date")) >= left for c in emp.get("current") or [] if c.get("start_date"))
    return dict(best, landed=landed)


# --- step 3: fit to open jobs ------------------------------------------------

def load_jobs(path: str) -> List[Dict[str, Any]]:
    with open(path, encoding="utf-8") as fh:
        jobs = json.load(fh)
    for j in jobs:
        j.setdefault("keywords", [])
        j.setdefault("seniority", [])
        j.setdefault("functions", [])
    return jobs


def fit_job(role: Dict[str, Any], jobs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Best open job for a past role, or None.

    A job fits when the person's seniority is in the job's list AND either the job's
    keywords appear in their old title or their function matches. Keyword hits count double.
    """
    title = (role.get("title") or "").lower()
    seniority = role.get("seniority_level") or ""
    function = (role.get("function_category") or "").lower()
    best, best_score = None, 0
    for job in jobs:
        if job["seniority"] and seniority not in job["seniority"]:
            continue
        score = 2 * sum(1 for k in job["keywords"] if k.lower() in title)
        score += 1 if function and function in [f.lower() for f in job["functions"]] else 0
        if score > best_score:
            best, best_score = job, score
    return dict(best, score=best_score) if best else None


def default_note(first_company: str, role_title: str, job: Dict[str, Any], sender: str) -> str:
    return (f"Hi, I saw the news about {first_company}. We are hiring a {job['title']} and your background as "
            f"{role_title or 'a ' + job.get('functions', ['team member'])[0]} looks like a close fit. "
            f"Open to a short chat this week? {sender}").strip()


def claude_note(company: str, role_title: str, job: Dict[str, Any], sender: str) -> Optional[str]:
    return ask_claude(
        f"Write a 2 sentence note from a recruiter ({sender or 'the recruiter'}) to a candidate who used to work as "
        f"'{role_title}' at {company}, which just announced layoffs. We are hiring a {job['title']}"
        f"{' (' + job['pitch'] + ')' if job.get('pitch') else ''}. Name the exact role, be warm and specific, "
        "no hype, no emojis, no mention of their name. Return only the note.")


def find_candidates(client: Crustdata, company: str, since: str, jobs: List[Dict[str, Any]],
                    per_company: int, include_landed: bool = False) -> Dict[str, Any]:
    stats = Counter()
    out: List[Dict[str, Any]] = []
    levels = sorted({s for j in jobs for s in j["seniority"]}) if all(j["seniority"] for j in jobs) else []
    for profile in client.person_search(leaver_filters(company, since, levels), PERSON_FIELDS, max_results=per_company):
        stats["searched"] += 1
        role = recent_exit(profile, company, since)
        if role is None:
            stats["not_a_recent_exit"] += 1
            continue
        if role["landed"] and not include_landed:
            stats["already_landed"] += 1
            continue
        job = fit_job(role, jobs)
        if job is None:
            stats["no_fitting_job"] += 1
            continue
        out.append({
            "person_id": profile.get("crustdata_person_id"),
            "name": (profile.get("basic_profile") or {}).get("name", ""),
            "profile_url": (((profile.get("social_handles") or {}).get("professional_network_identifier")) or {}).get("profile_url", ""),
            "company": company,
            "old_title": role.get("title", ""),
            "seniority": role.get("seniority_level", ""),
            "function": role.get("function_category", ""),
            "left": day(role.get("end_date")),
            "landed": role["landed"],
            "job": job["title"],
            "job_url": job.get("url", ""),
            "fit_score": job["score"],
        })
    stats["kept"] = len(out)
    return {"candidates": out, "stats": dict(stats)}


# --- output ------------------------------------------------------------------

def summary(result: Dict[str, Any]) -> Dict[str, Any]:
    cands = result["candidates"]
    totals: Counter = Counter()
    for s in result["per_company"].values():
        totals.update(s)
    return {
        "layoff_headlines": result["headlines"],
        "companies_found": len(result["companies"]),
        "profiles_searched": totals["searched"],
        "not_a_recent_exit": totals["not_a_recent_exit"],
        "already_landed": totals["already_landed"],
        "no_fitting_job": totals["no_fitting_job"],
        "candidates": len(cands),
        "by_open_job": dict(Counter(c["job"] for c in cands).most_common()),
        "by_seniority": dict(Counter(c["seniority"] or "unknown" for c in cands).most_common()),
    }


def slack_text(result: Dict[str, Any], limit: int = 15) -> str:
    cands = sorted(result["candidates"], key=lambda c: (c["fit_score"], c["left"]), reverse=True)
    lines = [f":briefcase: {len(cands)} candidates from {len(result['companies'])} companies with layoff news"]
    for c in cands[:limit]:
        lines.append(f"• *{c['name']}* ({c['old_title']}, {c['company']}, left {c['left']}) fits *{c['job']}*")
        if c.get("note"):
            lines.append(f"   _Note:_ {c['note']}")
    if len(cands) > limit:
        lines.append(f"…and {len(cands) - limit} more in out/candidates.csv")
    return "\n".join(lines)


def write_outputs(result: Dict[str, Any], out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "candidates.json"), "w") as fh:
        json.dump({"summary": summary(result), **result}, fh, indent=1)
    cols = ["name", "profile_url", "company", "old_title", "seniority", "left", "job", "job_url", "fit_score", "note"]
    with open(os.path.join(out_dir, "candidates.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["Name", "Profile URL", "Previous company", "Previous title", "Seniority", "Left",
                    "Job to pitch", "Job URL", "Fit score", "Note"])
        for c in result["candidates"]:
            w.writerow([c.get(k, "") for k in cols])


def only_new(cands: List[Dict[str, Any]], state_path: str) -> List[Dict[str, Any]]:
    seen = set()
    if os.path.exists(state_path):
        with open(state_path) as fh:
            seen = set(json.load(fh))
    return [c for c in cands if f"{c['person_id']}:{c['company']}" not in seen]


def save_state(cands: List[Dict[str, Any]], state_path: str) -> None:
    seen = set()
    if os.path.exists(state_path):
        with open(state_path) as fh:
            seen = set(json.load(fh))
    seen.update(f"{c['person_id']}:{c['company']}" for c in cands)
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    with open(state_path, "w") as fh:
        json.dump(sorted(seen), fh, indent=1)


def split(value: str) -> List[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def main(argv: Optional[List[str]] = None) -> int:
    load_env()
    p = argparse.ArgumentParser(description="Layoff Candidate Finder (Crustdata data layer)")
    p.add_argument("--jobs", default=os.environ.get("JOBS_FILE", "examples/jobs.example.json"), help="your open jobs (JSON)")
    p.add_argument("--companies", default="", help="skip the news step: comma list of company names")
    p.add_argument("--news-query", default=os.environ.get("NEWS_QUERY", DEFAULT_QUERY))
    p.add_argument("--news-days", type=int, default=int(os.environ.get("NEWS_DAYS", "7")))
    p.add_argument("--max-companies", type=int, default=int(os.environ.get("MAX_COMPANIES", "8")))
    p.add_argument("--since-days", type=int, default=int(os.environ.get("SINCE_DAYS", "60")), help="left in the last N days")
    p.add_argument("--per-company", type=int, default=int(os.environ.get("PER_COMPANY", "100")), help="profiles read per company")
    p.add_argument("--include-landed", action="store_true", help="keep people who already started a new job")
    p.add_argument("--notes", action="store_true", help="write the note with Claude (needs ANTHROPIC_API_KEY)")
    p.add_argument("--sender", default=os.environ.get("SENDER_NAME", ""), help="signature for the note")
    p.add_argument("--dry-run", action="store_true", help="never send to Slack")
    p.add_argument("--all", action="store_true", help="include candidates already alerted on")
    p.add_argument("--out", default="out")
    p.add_argument("--state", default=".state/seen.json")
    args = p.parse_args(argv)

    jobs = load_jobs(args.jobs)
    since = (dt.date.today() - dt.timedelta(days=args.since_days)).isoformat()
    client = Crustdata()

    if args.companies:
        news = [{"company": c, "headline": "", "url": ""} for c in split(args.companies)]
        headlines = 0
    else:
        news = find_layoff_news(client, args.news_query, args.news_days)
        headlines = len(news)
    news = news[:args.max_companies]

    result: Dict[str, Any] = {"headlines": headlines, "companies": [n["company"] for n in news],
                              "news": news, "candidates": [], "per_company": {}}
    for n in news:
        found = find_candidates(client, n["company"], since, jobs, args.per_company, args.include_landed)
        result["candidates"].extend(found["candidates"])
        result["per_company"][n["company"]] = found["stats"]
    if not args.all:
        result["candidates"] = only_new(result["candidates"], args.state)

    use_claude = args.notes and claude_available()
    if args.notes and not use_claude:
        print("Claude not configured (set ANTHROPIC_API_KEY and pip install anthropic): using the template note", file=sys.stderr)
    for c in result["candidates"]:
        job = next(j for j in jobs if j["title"] == c["job"])
        c["note"] = (claude_note(c["company"], c["old_title"], job, args.sender) if use_claude else None) \
            or default_note(c["company"], c["old_title"], job, args.sender)

    write_outputs(result, args.out)
    print(json.dumps(summary(result), indent=1))
    if result["candidates"]:
        print(send_slack(slack_text(result), dry_run=args.dry_run))
        if not args.dry_run:
            save_state(result["candidates"], args.state)
    else:
        print("No new candidates.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
