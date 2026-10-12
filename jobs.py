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
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
HISTORY = HERE / "seen.json"
PRIORITY_SEEN = HERE / "priority.json"
REPORT = HERE / "index.html"
KEEP_DAYS = 21
TZ = ZoneInfo("America/Los_Angeles")  # her time zone, not the server's
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
    """How many of `words` appear in txt. Whole words (plural s/es allowed); a trailing * means prefix,
    e.g. "brand*" also matches "branding". So "pr" matches "PR" but not "product"."""
    def pat(w):
        w = w.lower()
        if w.endswith("*"):
            return r"(?<![a-z0-9])" + re.escape(w[:-1])
        return r"(?<![a-z0-9])" + re.escape(w) + r"(?:s|es)?(?![a-z0-9])"
    txt = txt.lower()
    return sum(bool(re.search(pat(w), txt)) for w in words)


def keep(j, cfg):
    must = cfg.get("title_must_include_any", [])
    if must and not has(must, j["title"]):
        return False
    if has(cfg.get("title_exclude", []), j["title"]):
        return False
    return not has(cfg.get("company_exclude", []), j["company"])


def salary_in(desc):
    m = re.search(r"\$\s?\d[\d,.]*\s?[kK]?(?:\s*(?:-|–|—|to)\s*\$?\s?\d[\d,.]*\s?[kK]?)?(?:\s*(?:per|/|an)\s*(?:year|yr|hour|hr|annually))?", desc)
    return m.group(0).strip() if m and re.search(r"\d{2}", m.group(0)) else ""


def details(jid):
    """Read the full posting once: industry, seniority, applicants, salary, years required, description."""
    page = fetch(DETAIL + jid)
    if not page:
        return None
    crit = dict(re.findall(r'criteria-subheader">\s*(.*?)\s*</h3>\s*<span[^>]*>\s*(.*?)\s*</span>', page, re.S))
    desc = text(r'show-more-less-html__markup[^>]*>(.*?)</div>', page)
    years = [int(y) for y in re.findall(r"(\d{1,2})\+?\s*(?:-|–|to)?\s*(?:\d{1,2}\s*)?\+?\s*years", desc) if 0 < int(y) < 25]
    apps = re.search(r"([\d,]+|Over \d+)\s+applicants", page)
    return {"industry": html.unescape(crit.get("Industries", "")), "seniority": crit.get("Seniority level", ""),
            "salary": salary_in(desc),
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


def get_json(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept": "application/json",
                                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def ashby(co, wanted):
    board = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{co['board']}?includeCompensation=true")
    for x in board["jobs"]:
        if not x.get("isListed", True) or not wanted(x["title"]):
            continue
        pay = ((x.get("compensation") or {}).get("compensationTierSummary") or "").split("•")[0].strip()
        yield {"id": "ashby:" + x["id"], "title": x["title"], "url": x["jobUrl"],
               "location": x.get("location", "") + (" · Remote" if x.get("isRemote") else ""),
               "posted": (x.get("publishedAt") or "")[:10], "salary": pay,
               "desc": (x.get("descriptionPlain") or "").lower()}


def workday(co, wanted):
    base = f"https://{co['host']}/wday/cxs/{co['tenant']}/{co['site']}"
    offset, total = 0, 1
    while offset < total:
        page = get_json(base + "/jobs", {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""})
        total = page.get("total", 0) if offset == 0 else total
        offset += 20
        for x in page.get("jobPostings", []):
            if not wanted(x["title"]):
                continue
            info = get_json(base + x["externalPath"]).get("jobPostingInfo", {})
            desc = html.unescape(re.sub(r"<[^>]+>", " ", info.get("jobDescription", "")))
            days = re.search(r"(\d+)\+? Days? Ago", x.get("postedOn", ""))
            ago = int(days.group(1)) if days else 1 if "Yesterday" in x.get("postedOn", "") else 0
            yield {"id": "wd:" + x["externalPath"], "title": x["title"], "url": f"https://{co['host']}/{co['site']}{x['externalPath']}",
                   "location": x.get("locationsText", ""), "salary": salary_in(desc),
                   "posted": (dt.datetime.now(TZ).date() - dt.timedelta(days=ago)).isoformat(),
                   "desc": desc.lower()}


def priority_jobs(cfg, today):
    """Brand/comms roles at companies where she has contacts, read from their own job sites."""
    seen = json.loads(PRIORITY_SEEN.read_text()) if PRIORITY_SEEN.exists() else {}
    wanted = lambda title: has(cfg.get("priority_title_any", []), title) > 0
    out = []
    for co in cfg.get("priority_companies", []):
        print(f"Checking {co['name']} careers site")
        try:
            found = list({"ashby": ashby, "workday": workday}[co["ats"]](co, wanted))
        except Exception as e:  # one site being down shouldn't break the whole digest
            print(f"  ! couldn't read {co['name']}: {e}", file=sys.stderr)
            continue
        for j in found:
            d = {"industry": "", "desc": j.pop("desc")}
            j["company"] = co["name"]
            j.update(logo="", priority=True, first_seen=seen.get(j["id"], today),
                     roles=[r for r, words in cfg["roles"].items() if has(words, j["title"])],
                     interests=interests(d, j, cfg), detail={"salary": j.pop("salary")})
            out.append(j)
    PRIORITY_SEEN.write_text(json.dumps({j["id"]: j["first_seen"] for j in out}, indent=1))
    return out


def main():
    cfg = json.loads(CONFIG.read_text())
    today = dt.datetime.now(TZ).date().isoformat()
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
    cutoff = (dt.datetime.now(TZ).date() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    history = {k: v for k, v in history.items() if v["first_seen"] >= cutoff}
    HISTORY.write_text(json.dumps(history, indent=1))

    # Outside Seattle/Chicago/remote, only show roles that hit one of her interest areas.
    jobs = [j for j in history.values() if j["wheres"] != ["Other US cities"] or j.get("interests")]
    jobs.sort(key=lambda j: (j["first_seen"], j["score"]), reverse=True)
    new = sum(j["first_seen"] == today for j in jobs)
    priority = priority_jobs(cfg, today)
    REPORT.write_text(render(jobs, priority, today, cfg))
    print(f"\n{len(found)} matching jobs this run, {new} new today, {len(jobs)} in report -> {REPORT}")
    if "--open" in sys.argv:
        subprocess.run(["open", str(REPORT)])


def render(jobs, priority, today, cfg):
    slim = [{**j, "detail": {k: v for k, v in (j.get("detail") or {}).items() if k != "desc"}} for j in jobs]
    data = json.dumps(slim).replace("</", "<\\/")
    tpl = (HERE / "template.html").read_text()
    stamp = dt.datetime.now(TZ).strftime("%a %b %-d, %-I:%M %p Pacific")
    return (tpl.replace("__JOBS__", data).replace("__PRIORITY__", json.dumps(priority).replace("</", "<\\/"))
               .replace("__TODAY__", today)
               .replace("__UPDATED__", stamp))


if __name__ == "__main__":
    main()
