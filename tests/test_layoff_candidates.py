import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import layoff_candidates as lc  # noqa: E402
from crustdata_api import API_VERSION, Crustdata  # noqa: E402

JOBS = [
    {"title": "Senior Backend Engineer", "keywords": ["software engineer", "backend"], "seniority": ["Senior"],
     "functions": ["Engineering"], "url": "https://jobs.example/be"},
    {"title": "Product Marketing Manager", "keywords": ["product marketing"], "seniority": ["Senior", "Director"],
     "functions": ["Marketing"]},
]
SINCE = "2026-08-27"


def role(name, title, end, seniority="Senior", function="Engineering"):
    return {"name": name, "title": title, "end_date": end + "T00:00:00", "seniority_level": seniority,
            "function_category": function}


def person(pid, past, current=None):
    return {"crustdata_person_id": pid, "basic_profile": {"name": f"Person {pid}"},
            "social_handles": {"professional_network_identifier": {"profile_url": f"https://example.com/in/{pid}"}},
            "experience": {"employment_details": {"past": past, "current": current or []}}}


class HeadlineTests(unittest.TestCase):
    def test_extracts_company(self):
        cases = {
            "Kroger to lay off 3,000 corporate employees over 3 years": "Kroger",
            "Netflix is laying off 6% of its workforce in biggest round": "Netflix",
            "American Express to Cut 3,500 Jobs": "American Express",
            "Pfizer layoffs announced at Rocky Mount, Sanford facilities in NC": "Pfizer",
            "Breaking: Acme lays off 200 people": "Acme",
        }
        for title, expected in cases.items():
            self.assertEqual(lc.company_from_headline(title), expected, title)

    def test_skips_roundups(self):
        for title in ["Tech layoffs tracker 2026: All the job losses", "Companies cutting jobs as investments shift",
                      "12 Firms Cut Jobs for AI", "Layoffs Tracker: Kroger, Pfizer, Netflix and Amazon Hit This Week",
                      "Why layoffs are rising"]:
            self.assertIsNone(lc.company_from_headline(title), title)


class ExitTests(unittest.TestCase):
    def test_recent_exit_passes(self):
        p = person(1, [role("Netflix", "Senior Software Engineer", "2026-09-01")])
        r = lc.recent_exit(p, "Netflix", SINCE)
        self.assertEqual(r["title"], "Senior Software Engineer")
        self.assertFalse(r["landed"])

    def test_old_exit_rejected(self):
        self.assertIsNone(lc.recent_exit(person(2, [role("Netflix", "Engineer", "2026-03-01")]), "Netflix", SINCE))

    def test_still_there_rejected(self):
        p = person(3, [role("Netflix", "Engineer", "2026-09-01")], [{"name": "Netflix", "title": "Engineer"}])
        self.assertIsNone(lc.recent_exit(p, "Netflix", SINCE))

    def test_filters_matching_different_roles_rejected(self):
        # Netflix is an old role; the recent end date belongs to a different employer
        p = person(4, [role("Netflix", "Engineer", "2024-01-01"), role("Other Co", "Engineer", "2026-09-01")])
        self.assertIsNone(lc.recent_exit(p, "Netflix", SINCE))

    def test_landed_flag(self):
        p = person(5, [role("Netflix", "Engineer", "2026-09-01")],
                   [{"name": "NewCo", "title": "Engineer", "start_date": "2026-10-01T00:00:00"}])
        self.assertTrue(lc.recent_exit(p, "Netflix", SINCE)["landed"])


class FitTests(unittest.TestCase):
    def test_fits_best_job(self):
        job = lc.fit_job(role("X", "Senior Software Engineer", "2026-09-01"), JOBS)
        self.assertEqual(job["title"], "Senior Backend Engineer")

    def test_wrong_seniority_rejected(self):
        self.assertIsNone(lc.fit_job(role("X", "Software Engineer", "2026-09-01", seniority="Entry Level"), JOBS))

    def test_wrong_function_rejected(self):
        self.assertIsNone(lc.fit_job(role("X", "Recruiter", "2026-09-01", function="Human Resources"), JOBS))

    def test_note_names_the_job(self):
        note = lc.default_note("Netflix", "Senior Software Engineer", JOBS[0], "Yad")
        self.assertIn("Senior Backend Engineer", note)
        self.assertIn("Netflix", note)


class FakeTransport:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url, body, headers, timeout):
        self.calls.append((url, body, headers))
        for suffix, resp in self.responses.items():
            if url.endswith(suffix):
                return resp
        raise AssertionError(url)


class PipelineTests(unittest.TestCase):
    def test_news_then_people_endpoints(self):
        profiles = [
            person(1, [role("Netflix", "Senior Software Engineer", "2026-09-01")]),
            person(2, [role("Netflix", "Senior Software Engineer", "2026-09-01")],
                   [{"name": "NewCo", "start_date": "2026-10-01T00:00:00"}]),
            person(3, [role("Netflix", "Recruiter", "2026-09-01", function="Human Resources")]),
            person(4, [role("Netflix", "Senior Software Engineer", "2025-01-01")]),
        ]
        t = FakeTransport({
            "/web/search/live": {"results": [
                {"title": "Netflix is laying off 6% of its workforce", "url": "https://n.example/1"},
                {"title": "Tech layoffs tracker 2026", "url": "https://n.example/2"},
                {"title": "Netflix to cut more jobs", "url": "https://n.example/3"}]},
            "/person/search": {"profiles": profiles},
        })
        client = Crustdata(api_key="k", transport=t)
        news = lc.find_layoff_news(client, "layoffs", 7)
        self.assertEqual([n["company"] for n in news], ["Netflix"])
        found = lc.find_candidates(client, "Netflix", SINCE, JOBS, 50)
        self.assertEqual([c["person_id"] for c in found["candidates"]], [1])
        self.assertEqual(found["stats"], {"searched": 4, "already_landed": 1, "no_fitting_job": 1,
                                          "not_a_recent_exit": 1, "kept": 1})
        paths = [c[0] for c in t.calls]
        self.assertEqual(paths, ["https://api.crustdata.com/web/search/live", "https://api.crustdata.com/person/search"])
        self.assertTrue(all(c[2]["x-api-version"] == API_VERSION for c in t.calls))
        self.assertEqual(t.calls[0][1]["sources"], ["news"])
        self.assertIsInstance(t.calls[0][1]["start_date"], int)
        fields = [c["field"] for c in t.calls[1][1]["filters"]["conditions"]]
        self.assertEqual(fields, ["experience.employment_details.past.company_name",
                                  "experience.employment_details.end_date",
                                  "experience.employment_details.past.seniority_level"])
        self.assertEqual(t.calls[1][1]["filters"]["conditions"][2]["value"], ["Director", "Senior"])

    def test_include_landed(self):
        profiles = [person(2, [role("Netflix", "Senior Software Engineer", "2026-09-01")],
                           [{"name": "NewCo", "start_date": "2026-10-01T00:00:00"}])]
        client = Crustdata(api_key="k", transport=FakeTransport({"/person/search": {"profiles": profiles}}))
        found = lc.find_candidates(client, "Netflix", SINCE, JOBS, 10, include_landed=True)
        self.assertEqual(len(found["candidates"]), 1)


class OutputTests(unittest.TestCase):
    CAND = {"person_id": 1, "name": "Person 1", "profile_url": "u", "company": "Netflix", "old_title": "Senior Engineer",
            "seniority": "Senior", "function": "Engineering", "left": "2026-09-01", "landed": False,
            "job": "Senior Backend Engineer", "job_url": "", "fit_score": 2, "note": "Hi"}

    def result(self):
        return {"headlines": 3, "companies": ["Netflix"], "news": [], "candidates": [dict(self.CAND)],
                "per_company": {"Netflix": {"searched": 4, "kept": 1}}}

    def test_state_dedupes(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s", "seen.json")
            self.assertEqual(len(lc.only_new([self.CAND], path)), 1)
            lc.save_state([self.CAND], path)
            self.assertEqual(lc.only_new([self.CAND], path), [])

    def test_slack_text_and_outputs(self):
        text = lc.slack_text(self.result())
        self.assertIn("1 candidates from 1 companies", text)
        self.assertIn("Senior Backend Engineer", text)
        with tempfile.TemporaryDirectory() as d:
            lc.write_outputs(self.result(), d)
            data = json.load(open(os.path.join(d, "candidates.json")))
            self.assertEqual(data["summary"]["candidates"], 1)
            self.assertTrue(os.path.exists(os.path.join(d, "candidates.csv")))


if __name__ == "__main__":
    unittest.main()
