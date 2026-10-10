#!/usr/bin/env python3
"""Pull LinkedIn job listings (public, logged-out feed) and build a clean HTML report.

Usage: python3 jobs.py            # fetch, update history, write report.html
       python3 jobs.py --open     # same, then open the report in the browser
"""
import datetime as dt
import html
import json
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
HISTORY = HERE / "seen.json"
REPORT = HERE / "index.html"
KEEP_DAYS = 21
DETAIL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/"  # how long a job stays in the report after first being seen

API = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
POSTED = {"day": "r86400", "week": "r604800", "month": "r2592000"}
LEVELS = {"internship": "1", "entry": "2", "associate": "3",
          "mid-senior": "4", "director": "5", "executive": "6"}


def fetch(url, tries=4):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # 429 = LinkedIn rate limit: back off properly before retrying
            if i == tries - 1:
                print(f"  ! giving up on {url}: {e}", file=sys.stderr)
                return ""
            time.sleep((30 if "429" in str(e) else 5) * (i + 1))


def text(pattern, chunk):
    m = re.search(pattern, chunk, re.S)
    return html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() if m else ""


def parse(page):
    jobs = []
    for chunk in page.split("<li>")[1:]:
        jid = re.search(r"jobPosting:(\d+)", chunk)
        if not jid:
            continue
        logo = re.search(r'data-delayed-url="(https://media\.licdn\.com/[^"]+)"', chunk)
        posted = re.search(r'<time[^>]*datetime="([\d-]+)"', chunk)
        jobs.append({
            "id": jid.group(1),
            "title": text(r'base-search-card__title">(.*?)</h3>', chunk),
            "company": text(r'base-search-card__subtitle">(.*?)</h4>', chunk),
            "location": text(r'job-search-card__location">(.*?)</span>', chunk),
            "salary": text(r'job-search-card__salary-info">(.*?)</span>', chunk),
            "posted": posted.group(1) if posted else "",
            "logo": html.unescape(logo.group(1)) if logo else "",
            "url": f"https://www.linkedin.com/jobs/view/{jid.group(1)}/",
        })
    return jobs


def search(s, loc, cfg):
    params = {"keywords": s["keywords"], "location": loc["location"],
              "f_TPR": POSTED.get(cfg.get("posted_within", "week"), "r604800"),
              "sortBy": "R"}  # relevance, same default ordering LinkedIn shows
    levels = [LEVELS[l] for l in s.get("experience_levels", cfg.get("experience_levels", [])) if l in LEVELS]
    if levels:
        params["f_E"] = ",".join(levels)
    if loc.get("remote"):
        params["f_WT"] = "2"
    elif loc.get("onsite_or_hybrid"):
        params["f_WT"] = "1,3"
    out = []
    for page in range(loc.get("max_pages", cfg.get("max_pages_per_search", 4))):
        params["start"] = page * 10
        batch = parse(fetch(API + "?" + urllib.parse.urlencode(params)))
        for rank, j in enumerate(batch):
            j["rank"] = page * 10 + rank  # LinkedIn's own relevance position
            j["search"], j["label"], j["where"] = s["keywords"], s.get("label", s["keywords"]), loc["name"]
        out += batch
        if len(batch) < 10:
            break
        time.sleep(1.5)
    return out


def has(words, txt):
    """True if any word/phrase appears in txt as a whole word (prefix match, so 'communication' hits 'communications')."""
    return sum(bool(re.search(r"\b" + re.escape(w.lower()), txt.lower())) for w in words)


def keep(j, cfg):
    must = cfg.get("title_must_include_any", [])
    if must and not has(must, j["title"]):
        return False
    if has(cfg.get("title_exclude", []), j["title"]):
        return False
    return not has(cfg.get("company_exclude", []), j["company"])


def details(jid):
    """Read the full posting once: industry, seniority, applicants, salary, years required, description."""
    page = fetch(DETAIL + jid)
    if not page:
        return None
    crit = dict(re.findall(r'criteria-subheader">\s*(.*?)\s*</h3>\s*<span[^>]*>\s*(.*?)\s*</span>', page, re.S))
    desc = text(r'show-more-less-html__markup[^>]*>(.*?)</div>', page)
    pay = re.search(r"\$\s?\d[\d,.]*\s?[kK]?(?:\s*(?:-|–|—|to)\s*\$?\s?\d[\d,.]*\s?[kK]?)?(?:\s*(?:per|/|an)\s*(?:year|yr|hour|hr|annually))?", desc)
    years = [int(y) for y in re.findall(r"(\d{1,2})\+?\s*(?:-|–|to)?\s*(?:\d{1,2}\s*)?\+?\s*years", desc) if 0 < int(y) < 25]
    apps = re.search(r"([\d,]+|Over \d+)\s+applicants", page)
    return {"industry": html.unescape(crit.get("Industries", "")), "seniority": crit.get("Seniority level", ""),
            "salary": pay.group(0).strip() if pay and re.search(r"\d{2}", pay.group(0)) else "",
            "years": min(years) if years else None, "applicants": apps.group(1) if apps else "",
            "desc": desc.lower()}


def interests(d, j, cfg):
    return [name for name, rule in cfg["interests"].items()
            if has(rule["industries"], d["industry"])
            or has(rule["description"], d["desc"] + " " + j["company"].lower()) >= rule.get("min_hits", 1)]


def score(j, d, cfg):
    s = 60 - j["rank"]                                  # LinkedIn's own relevance position
    s += 15 * len(j["roles"]) + 25 * len(j["interests"])
    s += 8 * len(j["searches"])                         # surfaced by several searches
    if d:
        s += 4 * min(d["skills"], 10)
        if d["seniority"] in ("Director", "Executive"):
            s -= 30
        if d["years"] and d["years"] >= 8:              # she has ~5 years
            s -= 40
        elif d["years"] and d["years"] <= 2:
            s -= 15
    if has(["coordinator", "assistant", "principal", "intern"], j["title"]):
        s -= 30
    if j["wheres"] == ["Other US cities"]:              # outside her main targets: needs to be a strong fit
        s -= 25
    return s


def main():
    cfg = json.loads(CONFIG.read_text())
    today = dt.date.today().isoformat()
    history = json.loads(HISTORY.read_text()) if HISTORY.exists() else {}

    found = {}
    for loc in cfg["locations"]:
        for s in cfg["searches"]:
            if "only_labels" in loc and s.get("label") not in loc["only_labels"]:
                continue
            print(f"Searching: {s['keywords']} ({loc['name']})")
            for j in search(s, loc, cfg):
                if not keep(j, cfg):
                    continue
                if loc.get("strong_match_only") and not has(["communication", "brand", "public relations", "pr", "media"], j["title"]):
                    continue
                f = found.setdefault(j["id"], {**j, "searches": set(), "labels": set(), "wheres": set()})
                f["searches"].add(j["search"]); f["labels"].add(j["label"]); f["wheres"].add(j["where"])
                f["rank"] = min(f["rank"], j["rank"])
            time.sleep(3)

    for jid, j in found.items():
        for k in ("searches", "labels", "wheres"):
            j[k] = sorted(j[k])
        for k in ("search", "label", "where"):
            j.pop(k, None)
        old = history.get(jid, {})
        j["first_seen"] = old.get("first_seen", today)
        j["detail"] = old.get("detail")
        history[jid] = j

    todo = [j for j in found.values() if not j["detail"]]
    for i, j in enumerate(todo, 1):
        print(f"\rReading new postings: {i}/{len(todo)}", end="", flush=True)
        d = details(j["id"])
        if d:
            j["detail"] = d
        if i % 50 == 0:  # save progress so an interrupted run doesn't redo work
            HISTORY.write_text(json.dumps(history, indent=1))
        time.sleep(0.8)
    print()
    # Tag and score every job from its stored description, so config edits apply on the next run.
    for j in history.values():
        d = j.get("detail")
        if d:
            d["skills"] = has(cfg["her_skills"], d["desc"])
        j["roles"] = [r for r, words in cfg["roles"].items() if has(words, j["title"])]
        j["interests"] = interests(d, j, cfg) if d else []
        j["score"] = score(j, d, cfg)

    history = {k: v for k, v in history.items() if keep(v, cfg)}
    cutoff = (dt.date.today() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    history = {k: v for k, v in history.items() if v["first_seen"] >= cutoff}
    HISTORY.write_text(json.dumps(history, indent=1))

    # Outside Seattle/Chicago/remote, only show roles that hit one of her interest areas.
    jobs = [j for j in history.values() if j["wheres"] != ["Other US cities"] or j.get("interests")]
    jobs.sort(key=lambda j: (j["first_seen"], j["score"]), reverse=True)
    new = sum(j["first_seen"] == today for j in jobs)
    REPORT.write_text(render(jobs, today, cfg))
    print(f"\n{len(found)} matching jobs this run, {new} new today, {len(jobs)} in report -> {REPORT}")
    if "--open" in sys.argv:
        subprocess.run(["open", str(REPORT)])


def render(jobs, today, cfg):
    slim = [{**j, "detail": {k: v for k, v in (j.get("detail") or {}).items() if k != "desc"}} for j in jobs]
    data = json.dumps(slim).replace("</", "<\\/")
    tpl = (HERE / "template.html").read_text()
    stamp = dt.datetime.now().strftime("%a %b %-d, %-I:%M %p")
    return (tpl.replace("__JOBS__", data).replace("__TODAY__", today)
               .replace("__UPDATED__", stamp))


if __name__ == "__main__":
    main()
