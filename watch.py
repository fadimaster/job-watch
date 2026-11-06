"""
job-watch: polls companies' official job feeds (Greenhouse, Lever, Ashby,
Workable, SmartRecruiters, Workday) and opens a GitHub issue for every new
matching job. Runs on GitHub Actions; state is kept in seen.json.
"""
import csv, glob, json, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
import requests, yaml

UA = {"User-Agent": "job-watch/1.0 (personal job alerts)"}
TIMEOUT = 20

# ---------- feed fetchers: each returns list of dicts {id, title, location, url} ----------

def _strip(html):
    return [p.strip() for p in re.sub(r"<[^>]+>", "\n", html).split("\n") if p.strip()]

def greenhouse_eu_html(board):
    # EU boards have no public JSON API; read the official board page instead
    out, ids = [], set()
    for page in range(1, 6):
        r = requests.get(f"https://job-boards.eu.greenhouse.io/{board}", params={"page": page},
                         headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        added = 0
        for m in re.finditer(r'<a[^>]+href="([^"]*/' + re.escape(board) + r'/jobs/(\d+)[^"]*)"[^>]*>(.*?)</a>',
                             r.text, re.S | re.I):
            url, jid, inner = m.group(1), m.group(2), m.group(3)
            if jid in ids:
                continue
            parts = _strip(inner)
            if not parts:
                continue
            ids.add(jid); added += 1
            if url.startswith("/"):
                url = "https://job-boards.eu.greenhouse.io" + url
            out.append({"id": jid, "title": parts[0], "location": " ".join(p for p in parts[1:4] if p.lower() not in ("new", "hot")), "url": url.split("?")[0]})
        if added == 0:
            break
    if not out and "/jobs/" not in r.text:
        raise RuntimeError("no jobs parsed from EU board page (board name wrong?)")
    return out

def greenhouse(t):
    if t.get("region") == "eu":
        # try the global API first (some EU boards answer there), else parse the EU board page
        try:
            r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{t['board']}/jobs", headers=UA, timeout=TIMEOUT)
            if r.ok and r.json().get("jobs") is not None:
                return [{"id": str(j["id"]), "title": j["title"],
                         "location": (j.get("location") or {}).get("name", ""),
                         "url": j["absolute_url"]} for j in r.json()["jobs"]]
        except Exception:
            pass
        return greenhouse_eu_html(t["board"])
    r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{t['board']}/jobs", headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return [{"id": str(j["id"]), "title": j["title"],
             "location": (j.get("location") or {}).get("name", ""),
             "url": j["absolute_url"]} for j in r.json().get("jobs", [])]

def lever(t):
    host = "api.eu.lever.co" if t.get("region") == "eu" else "api.lever.co"
    r = requests.get(f"https://{host}/v0/postings/{t['board']}?mode=json", headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json():
        c = j.get("categories") or {}
        loc = ", ".join(filter(None, [c.get("location"), *(c.get("allLocations") or [])]))
        out.append({"id": j["id"], "title": j["text"], "location": loc or j.get("workplaceType", ""),
                    "url": j["hostedUrl"]})
    return out

def ashby(t):
    r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{t['board']}", headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        if j.get("isRemote"):
            locs.append("Remote")
        out.append({"id": j["id"], "title": j["title"], "location": ", ".join(filter(None, locs)),
                    "url": j.get("jobUrl") or j.get("applyUrl")})
    return out

def workable(t):
    r = requests.get(f"https://apply.workable.com/api/v1/widget/accounts/{t['board']}", headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        loc = ", ".join(filter(None, [j.get("city"), j.get("country")]))
        if j.get("telecommuting"):
            loc += " (Remote)"
        out.append({"id": j["shortcode"], "title": j["title"], "location": loc, "url": j["url"]})
    return out

def smartrecruiters(t):
    out, offset = [], 0
    while True:
        r = requests.get(f"https://api.smartrecruiters.com/v1/companies/{t['board']}/postings",
                         params={"limit": 100, "offset": offset}, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
        for j in d.get("content", []):
            l = j.get("location") or {}
            loc = ", ".join(filter(None, [l.get("city"), l.get("country")])) + (" (Remote)" if l.get("remote") else "")
            out.append({"id": j["id"], "title": j["name"], "location": loc,
                        "url": f"https://jobs.smartrecruiters.com/{t['board']}/{j['id']}"})
        offset += 100
        if offset >= d.get("totalFound", 0):
            return out

def workday(t):
    # t: host (e.g. nvidia.wd5.myworkdayjobs.com), tenant, site
    base = f"https://{t['host']}/wday/cxs/{t['tenant']}/{t['site']}"
    out = []
    for q in t.get("searches", ["IT Manager", "Head of IT", "Director IT", "Information Technology"]):
        r = requests.post(f"{base}/jobs", json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": q},
                          headers={**UA, "Content-Type": "application/json"}, timeout=TIMEOUT)
        r.raise_for_status()
        for j in r.json().get("jobPostings", []):
            path = j.get("externalPath", "")
            out.append({"id": path, "title": j.get("title", ""), "location": j.get("locationsText", ""),
                        "url": f"https://{t['host']}/{t['site']}{path}"})
    return out

def comeet_hosted(t):
    # no token: read Comeet's public hosted careers page (comeet.com/jobs/<slug>/<uid>)
    base = f"https://www.comeet.com/jobs/{t['slug']}/{t['uid']}"
    r = requests.get(base, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    out, ids = [], set()
    m = re.search(r"COMPANY_POSITIONS_DATA\s*=\s*(\[.*?\]);\s*\n", r.text, re.S)
    if m:
        try:
            for j in json.loads(m.group(1)):
                l = j.get("location") or {}
                loc = ", ".join(filter(None, [l.get("city"), l.get("country"), l.get("name")]))
                url = j.get("url_comeet_hosted_page") or j.get("url_active_page") or ""
                out.append({"id": j.get("uid") or url, "title": j.get("name", ""), "location": loc,
                            "url": url.split("?")[0]})
            return out
        except ValueError:
            pass
    # fallback: job links on the page (title from the link text or URL slug)
    pat = r'href="([^"]*/jobs/' + re.escape(t["slug"]) + "/" + re.escape(t["uid"]) + r'/([^/"]+)/([0-9A-Fa-f]{2}\.[0-9A-Fa-f]{3})[^"]*)"'
    for m in re.finditer(pat, r.text):
        url, slug, pid = m.groups()
        if pid in ids:
            continue
        ids.add(pid)
        if url.startswith("/"):
            url = "https://www.comeet.com" + url
        out.append({"id": pid, "title": slug.replace("-", " "), "location": "", "url": url.split("?")[0]})
    if not out and "comeet" not in r.text.lower():
        raise RuntimeError("Comeet page had no positions")
    return out

def breezy(t):
    r = requests.get(f"https://{t['board']}.breezy.hr/json", headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return [{"id": j.get("id") or j.get("friendly_id"), "title": j.get("name", ""),
             "location": (j.get("location") or {}).get("name", ""), "url": j.get("url", "")} for j in r.json()]

def comeet(t):
    if not t.get("token"):
        return comeet_hosted(t)
    # uid + token are in the company's careers page source (search for "comeet" / "token")
    r = requests.get(f"https://www.comeet.co/careers-api/2.0/company/{t['uid']}/positions",
                     params={"token": t["token"]}, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json():
        l = j.get("location") or {}
        loc = ", ".join(filter(None, [l.get("city"), l.get("country"), l.get("name")]))
        if l.get("is_remote") or j.get("workplace_type") == "Remote":
            loc += " (Remote)"
        out.append({"id": j["uid"], "title": j["name"], "location": loc,
                    "url": j.get("url_active_page") or j.get("url_comeet_hosted_page") or ""})
    return out

FETCHERS = {"comeet": comeet, "breezy": breezy, "greenhouse": greenhouse, "lever": lever, "ashby": ashby, "workable": workable,
            "smartrecruiters": smartrecruiters, "workday": workday}

# ---------- Israeli Tech Map (github.com/mluggy/techmap), cloned by the workflow into ./techmap ----------

def techmap_targets(manual_names):
    out = []
    for f in glob.glob("techmap/companies/*.json"):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        if not d.get("isActive", True) or d.get("name", "").lower() in manual_names:
            continue
        base = {"name": d["name"], "auto": True, "default_location": "Israel"}
        if d.get("greenhouseId"):
            out.append({**base, "ats": "greenhouse", "board": d["greenhouseId"]})
        elif d.get("leverId"):
            out.append({**base, "ats": "lever", "board": d["leverId"]})
        elif d.get("comeetId") and "/" in d["comeetId"]:
            slug, uid = d["comeetId"].split("/", 1)
            out.append({**base, "ats": "comeet", "slug": slug, "uid": uid})
        elif d.get("breezyId"):
            out.append({**base, "ats": "breezy", "board": d["breezyId"]})
    return out

def techmap_jobs(max_age_days):
    cutoff = (date.today() - timedelta(days=max_age_days)).isoformat()
    out = []
    for f in glob.glob("techmap/jobs/*.csv"):
        for row in csv.DictReader(open(f, encoding="utf-8-sig")):
            if row.get("updated") and row["updated"] < cutoff:
                continue
            url = (row.get("url") or "").split("?")[0]
            out.append((row.get("company", "?"), {"id": url, "title": row.get("title", ""),
                        "location": (row.get("city") or "") + ", Israel", "url": url}))
    return out

# ---------- filtering ----------

def compile_list(words):
    return re.compile("|".join(words), re.I) if words else None

def matches(job, cfg, remote_ok):
    title, loc = job["title"] or "", job["location"] or ""
    if not cfg["title_re"].search(title):
        return False
    if cfg["exclude_re"] and cfg["exclude_re"].search(title):
        return False
    if cfg["location_re"].search(loc):
        return True
    if remote_ok and cfg["remote_re"].search(loc) and not cfg["remote_block_re"].search(loc):
        return True
    return False

# ---------- GitHub issues ----------

def gh(method, path, **kw):
    repo, token = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"]
    return requests.request(method, f"https://api.github.com/repos/{repo}{path}",
                            headers={"Authorization": f"Bearer {token}",
                                     "Accept": "application/vnd.github+json"}, timeout=TIMEOUT, **kw)

def existing_issue_urls():
    """Apply-links of every job issue ever opened (open or closed): a second, durable dedupe."""
    urls, page = set(), 1
    while page <= 20:
        r = gh("GET", "/issues", params={"labels": "job", "state": "all", "per_page": 100, "page": page})
        if not r.ok:
            break
        batch = r.json()
        for i in batch:
            m = re.search(r"Apply: (\S+)", i.get("body") or "")
            if m:
                urls.add(m.group(1))
        if len(batch) < 100:
            break
        page += 1
    return urls

def ensure_labels():
    for name, color in [("job", "1f6feb"), ("feed-error", "d73a4a")]:
        gh("POST", "/labels", json={"name": name, "color": color})  # 422 if exists: fine

def open_issue(title, body, label, ping=False):
    data = {"title": title[:250], "body": body, "labels": [label]}
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER")
    if ping and owner:
        # GitHub Mobile only pushes mentions/assignments, so mention + assign the owner
        data["body"] = f"@{owner} new match\n\n" + body
        data["assignees"] = [owner]
    r = gh("POST", "/issues", json=data)
    if r.status_code >= 300:
        print(f"!! issue create failed {r.status_code}: {r.text[:200]}")
    time.sleep(1)  # be gentle with the API

# ---------- main ----------

def main():
    conf = yaml.safe_load(open("targets.yaml"))
    f = conf["filters"]
    cfg = {"title_re": compile_list(f["titles"]), "exclude_re": compile_list(f.get("exclude", [])),
           "location_re": compile_list(f["locations"]), "remote_re": compile_list(f["remote"]),
           "remote_block_re": compile_list(f["remote_block"])}
    seen = json.load(open("seen.json")) if os.path.exists("seen.json") else {}
    dry = os.environ.get("DRY_RUN") == "1"
    known_urls = set()
    if not dry:
        ensure_labels()
        known_urls = existing_issue_urls()

    companies = list(conf["companies"])
    tm = conf.get("techmap") or {}
    if tm.get("enabled") and os.path.isdir("techmap"):
        companies += techmap_targets({c["name"].lower() for c in companies})

    def fetch(t):
        try:
            return t, FETCHERS[t["ats"]](t), None
        except Exception as e:
            return t, [], f"{type(e).__name__}: {str(e)[:120]}"

    results = []
    with ThreadPoolExecutor(max_workers=24) as pool:
        for t, jobs, err in pool.map(fetch, companies):
            results.append((t, jobs, err))

    feed = []
    broken, auto_broken = [], 0
    for t, jobs, err in results:
        if err:
            if t.get("auto"):
                auto_broken += 1
            else:
                broken.append(f"{t['name']} ({t['ats']}:{t.get('board', t.get('host', t.get('slug')))}) -> {err}")
            continue
        for j in jobs:
            if not j.get("location") and t.get("default_location"):
                j["location"] = t["default_location"]
            feed.append((t["name"], j, t.get("remote_ok", True), False))
    if tm.get("enabled") and os.path.isdir("techmap"):
        for name, j in techmap_jobs(tm.get("max_age_days", 21)):
            feed.append((name, j, False, tm.get("all_israel", True)))

    new, urls_this_run = [], set()
    for name, j, remote_ok, any_israel in feed:
        key = f"{name}:{j['id']}"
        if key in seen or j["url"] in urls_this_run:
            continue
        ok = matches(j, cfg, remote_ok) or (any_israel and cfg["title_re"].search(j["title"] or "")
                                             and not (cfg["exclude_re"] and cfg["exclude_re"].search(j["title"])))
        if not ok:
            continue
        seen[key] = time.strftime("%Y-%m-%d")
        urls_this_run.add(j["url"])
        if j["url"] in known_urls:
            continue
        new.append((name, j))

    print(f"checked {len(companies)} company feeds (+Tech Map jobs list), {len(new)} new matches, "
          f"{len(broken)} broken feeds, {auto_broken} auto-discovered feeds unreachable")
    for name, j in new:
        print(f"NEW  {name} | {j['title']} | {j['location']} | {j['url']}")
        if not dry:
            open_issue(f"{name}: {j['title']}",
                       f"**{j['title']}**\n\nCompany: {name}\nLocation: {j['location'] or 'n/a'}\n\nApply: {j['url']}",
                       "job", ping=True)

    # report broken feeds at most once a week so they don't spam you
    if broken:
        for b in broken:
            print("BROKEN", b)
        week = time.strftime("%G-W%V")
        if not dry and seen.get("_broken_reported") != week:
            open_issue(f"Feed check: {len(broken)} feeds failed ({week})",
                       "These feeds returned errors. Usually the board name is wrong or the company changed ATS. "
                       "Fix or remove them in targets.yaml.\n\n" + "\n".join(f"- {b}" for b in broken),
                       "feed-error")
            seen["_broken_reported"] = week

    json.dump(seen, open("seen.json", "w"), indent=1, sort_keys=True)

if __name__ == "__main__":
    sys.exit(main())
