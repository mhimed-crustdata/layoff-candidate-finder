# Layoff Candidate Finder (Open Source, Slack + Ashby-Ready CSV)

**An open-source Python tool that reads this week's layoff news, finds people who recently left those companies, keeps the ones who fit your open jobs, and drafts a note that names the exact role.**

The week a company announces layoffs is the best week to write to its people: they are open to a good offer even if they never say so, and the recruiter who writes first with a role that fits gets the reply. This tool runs that search every morning. [Crustdata](https://crustdata.com) is the web and people data layer ([API docs](https://docs.crustdata.com)).

## How it works

```mermaid
flowchart LR
    A[Layoff news<br/>POST /web/search/live] --> B[Company names<br/>from headlines]
    B --> C[Recent leavers<br/>POST /person/search]
    C --> D[Re-check in code<br/>left recently, not landed]
    D --> E[Fit to your open jobs<br/>role + seniority]
    E --> F[Note that names the role]
    F --> G[Slack + CSV for Ashby]
```

1. **Catch the news.** `POST /web/search/live` with `sources: ["news"]` and a unix-time window (last 7 days). Two queries by default. Roundup headlines ("Tech layoffs tracker") are dropped; headlines like "X to lay off 3,000" give the company name.
2. **Find who left.** One `POST /person/search` per company: past employer equals the company, a role end date since 60 days ago, and past seniority in the levels your jobs need.
3. **Re-check on one role.** The search filters can match different roles of the same person, so the code checks again: the past role *at that company* ended in the window, the person is not still there, and no newer job has already started (`already_landed`).
4. **Fit.** `examples/jobs.example.json` lists your open jobs with title keywords, seniority levels and function. A job fits when the seniority is allowed and the old title or function matches.
5. **Write the note.** A template note by default; with `--notes`, Claude writes two sentences that name the job.
6. **Hand off.** Slack message plus `out/candidates.csv` (name, profile URL, old title, job to pitch, note) ready to import into your ATS, for example Ashby.

## Quickstart

Requires Python 3.9+. No dependencies for the core tool.

```bash
git clone https://github.com/mhimed-crustdata/layoff-candidate-finder
cd layoff-candidate-finder
make setup        # creates .env, then add your CRUSTDATA_API_KEY
# edit examples/jobs.example.json with your open jobs
make demo         # runs the whole pipeline, never posts to Slack
make run          # posts new candidates to SLACK_WEBHOOK_URL
make test         # unit tests, no network
```

Run it every morning with cron (`0 8 * * * cd /path/to/repo && make run`) or a scheduled CI job.

Already know the company? Skip the news step: `python3 layoff_candidates.py --companies "Acme,Globex" --dry-run`.

## Configuration

All settings live in `.env` (copy from `.env.example`). Command-line flags override them.

| Variable | Default | What it does |
|---|---|---|
| `CRUSTDATA_API_KEY` | required | Your Crustdata API key. Read from the environment only |
| `JOBS_FILE` | `examples/jobs.example.json` | Your open jobs: `title`, `keywords`, `seniority`, `functions`, optional `url` and `pitch` |
| `NEWS_QUERY` | two layoff queries | Web search queries, separated by `;` |
| `NEWS_DAYS` | `7` | News window in days |
| `SINCE_DAYS` | `60` | Candidates who left in the last N days (compared by month) |
| `MAX_COMPANIES` | `8` | Companies searched per run |
| `PER_COMPANY` | `100` | Profiles read per company |
| `SENDER_NAME` | empty | Signature on the note |
| `SLACK_WEBHOOK_URL` | empty | Slack incoming webhook. Empty means no Slack post |
| `ANTHROPIC_API_KEY` | empty | Optional. With `--notes`, Claude writes the note |
| `ANTHROPIC_MODEL` | `claude-opus-5-5` | Model for the optional Claude step |

Useful flags: `--include-landed`, `--all`, `--dry-run`, `--out DIR`. Seniority values come from the Crustdata closed set: Entry Level, Senior, Experienced Manager, Director, Vice President, CXO, and others.

Output files in `out/` contain personal data and are git-ignored.

## Example output

A real run on 2026-10-26 against the live API: two news queries (20 headlines), then the people search for four large employers named in the headlines, with the default job file, 60-day window and 100 profiles per company. Aggregated:

```json
{
  "layoff_headlines": 20,
  "companies_found": 4,
  "profiles_searched": 400,
  "not_a_recent_exit": 376,
  "already_landed": 11,
  "no_fitting_job": 12,
  "candidates": 1
}
```

Read this honestly: the people search narrows by past employer and seniority, but its end-date filter matches any role a person held, not only the role at that company. For large employers about 94% of the profiles it returns are not recent exits at all, which is why step 3 re-checks every profile in code. The funnel above is what survives. For a real daily run, raise `PER_COMPANY`, add smaller companies with `--companies`, or point the tool at layoffs where you hire in volume. The tool never contacts anyone.

What the Slack alert looks like (people redacted):

```text
:briefcase: 1 candidates from 4 companies with layoff news
• [Name] ([old title], [company], left 2026-09-01) fits Senior Backend Engineer
   Note: Hi, I saw the news about [company]. We are hiring a Senior Backend Engineer and your background as [old title] looks like a close fit. Open to a short chat this week?
```

## How to build a layoff candidate agent

1. **Start from the news, not a list.** A web search over a short window gives you the companies that matter today. Extract the company from the headline and skip roundups.
2. **Search people by past employer.** Use `experience.employment_details.past.company_name` plus a seniority narrow on the same past role.
3. **Never trust a nested filter on its own.** Two conditions on `experience.employment_details` can match different roles. Re-check the role, the end month and the current job in code.
4. **Drop people who already landed.** A current role that started after the exit means they are not on the market.
5. **Match on role and seniority, not on company.** Keep a small jobs file; every candidate is tied to one open job and the note names it.
6. **Use the model for words only.** Claude writes the note. It never decides who is kept.
7. **Send the same day.** Slack for the alert, CSV for your ATS import. For real-time delivery, see Crustdata watches in the [docs](https://docs.crustdata.com).

## FAQ

**How to find candidates affected by layoffs?**
Search layoff news for the company, then search people whose past employer is that company with a recent role end date, and keep the ones who fit your open jobs. This tool does those steps with Crustdata's web search and person search, and re-checks each profile in code.

**Why write the week of the announcement?**
People who just left are reachable and often open to offers. A short note with a role that fits their background is more likely to get a reply than a generic message weeks later.

**How does it know a person already found a new job?**
If a current role started on or after the month the old role ended, the person is marked `landed` and dropped. Use `--include-landed` to keep them.

**Why are so few candidates returned for large companies?**
The search's end-date filter is not bound to the role at that company, so most returned profiles are filtered out by the re-check. See the note under Example output.

**Does it contact anyone?**
No. It only posts to your own Slack webhook when `SLACK_WEBHOOK_URL` is set and `--dry-run` is off. You send the notes.

**Which Crustdata endpoints does it use?**
`POST /web/search/live` and `POST /person/search` on API version `2025-11-01`. See the [Crustdata API docs](https://docs.crustdata.com).

## Project layout

```text
layoff_candidates.py   # the workflow: news, leavers, fit, notes, output
crustdata_api.py       # tiny Crustdata client (stdlib only)
notify.py              # optional Slack + Claude
examples/              # sample open jobs file
tests/                 # unit tests, no network
```

## License

MIT. See [LICENSE](LICENSE). Data layer: [Crustdata](https://crustdata.com) ([docs](https://docs.crustdata.com)).
