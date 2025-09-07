import os
import re
import json
import base64
import logging
import warnings
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Optional

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Gmail API
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

warnings.filterwarnings("ignore", message="Parsing dates involving a day of month")
load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TZ = ZoneInfo(os.getenv("TIMEZONE", "America/Phoenix"))

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DB = os.getenv("NOTION_DATABASE_ID")
assert NOTION_TOKEN and NOTION_DB, "Set NOTION_TOKEN and NOTION_DATABASE_ID in .env"

# Gmail search for the schedule
GMAIL_QUERY = os.getenv("GMAIL_QUERY", "subject:(schedule OR shifts) newer_than:30d")

YOUR_NAME = os.getenv("YOUR_NAME", "Jeshad")
FILTER_BY_NAME = os.getenv("FILTER_BY_NAME", "true").lower() in ("1", "true", "yes")

OV_TITLE     = os.getenv("NOTION_TITLE_PROP") or None   # Title
OV_DATE      = os.getenv("NOTION_DATE_PROP") or None    # Date
OV_DAY       = os.getenv("NOTION_DAY_PROP") or None     # Day of Week
OV_TIME      = os.getenv("NOTION_TIME_PROP") or None    # Time
OV_LOCATION  = os.getenv("NOTION_LOCATION_PROP") or None# Location
OV_PEOPLE    = os.getenv("NOTION_PEOPLE_PROP") or None  # People

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

KNOWN_SITES = {"Aeroterra", "CTEC", "Guadalupe", "Tempe", "Chandler", "Mesa", "Superior", "Sierra Vista"}

# ---------------- Gmail helpers ----------------

def gmail_service():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and getattr(creds, "refresh_token", None) and creds.expired:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.json", "w") as token:
            token.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)

def fetch_latest_email(svc):
    resp = svc.users().messages().list(userId="me", q=GMAIL_QUERY, maxResults=10).execute()
    msgs = resp.get("messages", [])
    if not msgs:
        return None, None, ""
    msgs = list(reversed(sorted(msgs, key=lambda m: m["id"])))
    for m in msgs:
        full = svc.users().messages().get(userId="me", id=m["id"], format="full").execute()
        payload = full.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        subject = headers.get("subject", "")
        text = extract_body_text(payload)
        html = extract_raw_html(payload)
        if text:
            return subject, text, html
    return None, None, ""

def extract_body_text(payload) -> str:
    def _walk(part):
        mt = part.get("mimeType", "")
        if mt.startswith("text/"):
            data = part.get("body", {}).get("data")
            if data:
                raw = base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="ignore")
                return html_to_text(raw) if mt == "text/html" else raw
        for p in part.get("parts", []) or []:
            t = _walk(p)
            if t:
                return t
        return ""
    return _walk(payload) or ""

def extract_raw_html(payload) -> str:
    def _walk(part):
        if part.get("mimeType") == "text/html":
            data = part.get("body", {}).get("data")
            if data:
                return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="ignore")
        for p in part.get("parts", []) or []:
            h = _walk(p)
            if h:
                return h
        return ""
    return _walk(payload)

def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for br in soup.find_all(["br", "p", "li"]):
        br.append("\n")
    return soup.get_text(separator="\n")

# ---------------- Parsing ----------------

WINDOW_RE = re.compile(
    r"schedule\s+for\s+(?P<start>([A-Za-z]+\.?\s+)?[A-Za-z]+\s+\d{1,2}(st|nd|rd|th)?)\s*[–-]\s*(?P<end_day>\d{1,2}(st|nd|rd|th)?)",
    re.IGNORECASE,
)
DAY_HEADER_RE = re.compile(
    r"^\s*(?P<daynum>\d{1,2})\s+(?P<weekday>Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b(?P<rest>.*)$",
    re.IGNORECASE,
)
TIME_ROW_RE = re.compile(
    r"(?P<t1>\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm)?)\s*-\s*(?P<t2>\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm)?)\s+(?P<tail>.+)$"
)

MONTHS = {m.lower(): i for i, m in enumerate(
    ["January","February","March","April","May","June","July","August","September","October","November","December"], start=1
)}

def month_from_text(s: str) -> Optional[int]:
    sL = s.lower()
    for name, num in MONTHS.items():
        if name in sL:
            return num
    if "sept" in sL or "sep" in sL:
        return 9
    return None

def parse_window(text: str, now: datetime) -> tuple[date, date]:
    m = WINDOW_RE.search(text)
    if not m:
        ws = get_week_start(now).date()
        return ws, ws + timedelta(days=6)
    start_str = m.group("start")
    end_day_str = m.group("end_day")
    month = month_from_text(start_str) or now.month
    start_day = int(re.sub(r"\D", "", start_str))
    start = date(now.year, month, start_day)
    if (start - now.date()).days > 120:
        start = date(now.year - 1, month, start_day)
    if (now.date() - start).days > 250:
        start = date(now.year + 1, month, start_day)
    end_day = int(re.sub(r"\D", "", end_day_str))
    try:
        end = date(start.year, month, end_day)
    except ValueError:
        last = (date(start.year, month, 1) + timedelta(days=40)).replace(day=1) - timedelta(days=1)
        end = last
    if end < start:
        nm_year = start.year + (1 if month == 12 else 0)
        nm_month = 1 if month == 12 else month + 1
        try:
            end = date(nm_year, nm_month, end_day)
        except ValueError:
            last = (date(nm_year, nm_month, 1) + timedelta(days=40)).replace(day=1) - timedelta(days=1)
            end = last
    return start, end

def get_week_start(dt: datetime) -> datetime:
    return (dt - timedelta(days=dt.weekday())).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=TZ)

def first_site_token(tail: str) -> tuple[str, str]:
    for site in sorted(KNOWN_SITES, key=len, reverse=True):
        m = re.search(rf"\b{re.escape(site)}\b", tail)
        if m and m.start() < 40:
            pre = tail[:m.start()].strip()
            post = tail[m.end():].strip()
            rest = " ".join(t for t in [pre, post] if t).strip()
            return site, rest
    parts = tail.split()
    if parts:
        loc = parts[0].strip(",")
        rest = " ".join(parts[1:]).strip()
        return loc, rest
    return "", ""

TASK_KEYWORDS_RE = re.compile(
    r"(WORKSHOP|closed|closure|popup|pop-up|job\s*fair|shuttle|event|debrief|keys?)",
    re.IGNORECASE,
)

def split_people_task(rest: str) -> tuple[str, str]:
    if not rest:
        return "", ""
    u = rest.upper()
    idx = u.find("WORKSHOP")
    if idx != -1:
        return rest[:idx].strip(" ,;-"), rest[idx:].strip(" ,;-")
    m = TASK_KEYWORDS_RE.search(rest)
    if m:
        kstart = m.start()
        return rest[:kstart].strip(" ,;-"), rest[kstart:].strip(" ,;-")
    return rest.strip(), ""

def normalize_time(t: str) -> str:
    t = t.strip().upper().replace(" ", "")
    t = re.sub(r"(AM|PM)$", r" \1", t)
    return t

def parse_rows(subject: str, body: str) -> list[dict]:
    now = datetime.now(TZ)
    window_start, window_end = parse_window(body or subject or "", now)
    lines = [ln.strip() for ln in body.splitlines()]
    current_date: Optional[date] = None
    current_weekday: Optional[str] = None

    rows = []
    for raw in lines:
        line = " ".join(raw.split())
        if not line:
            continue

        mday = DAY_HEADER_RE.match(line)
        if mday:
            daynum = int(mday.group("daynum"))
            current_weekday = mday.group("weekday").capitalize()
            try:
                candidate = date(window_start.year, window_start.month, daynum)
            except ValueError:
                y = window_start.year + (1 if window_start.month == 12 else 0)
                mth = 1 if window_start.month == 12 else window_start.month + 1
                last = (date(y, mth, 1) + timedelta(days=40)).replace(day=1) - timedelta(days=1)
                candidate = last
            current_date = candidate

            rest = mday.group("rest").strip()
            mt = TIME_ROW_RE.search(rest)
            if mt:
                t1, t2, tail = mt.group("t1"), mt.group("t2"), mt.group("tail")
                location, tail_after_loc = first_site_token(tail)
                people, task = split_people_task(tail_after_loc)
                add_row(rows, current_date, current_weekday, t1, t2, location, people, task)
            continue

        mt = TIME_ROW_RE.search(line)
        if mt and current_date is not None:
            t1, t2, tail = mt.group("t1"), mt.group("t2"), mt.group("tail")
            location, tail_after_loc = first_site_token(tail)
            people, task = split_people_task(tail_after_loc)
            add_row(rows, current_date, current_weekday, t1, t2, location, people, task)
            continue

    if FILTER_BY_NAME:
        rows = [r for r in rows if re.search(rf"\b{re.escape(YOUR_NAME)}\b", r["People"], re.IGNORECASE)]

    rows = [r for r in rows if window_start <= r["_date"] <= window_end]

    seen = set()
    unique = []
    for r in rows:
        key = (r["_date"].isoformat(), r["Time"], r["Location"], r["People"], r["Task"])
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique

def add_row(results, d: date, weekday: Optional[str], t1: str, t2: str, location: str, people: str, task: str):
    t1n, t2n = normalize_time(t1), normalize_time(t2)
    day_name = weekday or datetime(d.year, d.month, d.day).strftime("%A")
    title_text = task.strip() if task.strip() else f"{location or 'Shift'} {t1n}-{t2n}".strip()
    results.append({
        "_title_content": title_text,
        "Day of the Week": day_name,
        "Date": d,
        "Time": f"{t1n} - {t2n}",
        "Location": location,
        "People": people.strip(),
        "Task": task.strip(),
        "_date": d,
    })

# Write to Notion

def get_db_schema(db_id: str):
    url = f"https://api.notion.com/v1/databases/{db_id}"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
    }
    r = requests.get(url, headers=headers)
    if r.status_code != 200:
        raise RuntimeError(f"Failed to read database schema: {r.status_code} {r.text}")
    return r.json()

def find_title_prop(props: dict) -> str:
    if OV_TITLE:
        return OV_TITLE
    for name, spec in props.items():
        if spec.get("type") == "title":
            return name
    raise RuntimeError("No title property found in Notion database. Set NOTION_TITLE_PROP in .env")

def fuzzy_get(props: dict, candidates: list[str], expected_types: list[str]) -> Optional[str]:
    lowered = {k.lower(): (k, v) for k, v in props.items()}
    for cand in candidates:
        x = lowered.get(cand.lower())
        if x:
            name, spec = x
            if spec.get("type") in expected_types:
                return name
    for cand in candidates:
        x = lowered.get(cand.lower())
        if x:
            return x[0]
    return None

def notion_create(rows: list[dict]):
    if not rows:
        logging.info("No shifts to write.")
        return

    schema = get_db_schema(NOTION_DB)
    props = schema.get("properties", {})

    title_prop    = find_title_prop(props)
    date_prop     = OV_DATE     or fuzzy_get(props, ["Date", "Shift", "When"], ["date"]) or "Date"
    day_prop      = OV_DAY      or fuzzy_get(props, ["Day of Week", "Day of the Week", "Day", "Weekday"], ["formula", "rich_text", "select"])
    time_prop     = OV_TIME     or fuzzy_get(props, ["Time", "Hours"], ["rich_text"]) or "Time"
    location_prop = OV_LOCATION or fuzzy_get(props, ["Location", "Site"], ["select", "rich_text"]) or "Location"
    people_prop   = OV_PEOPLE   or fuzzy_get(props, ["People", "Notes"], ["rich_text"]) or "People"

    prop_types = {
        "title":    props.get(title_prop, {}).get("type", "title"),
        "date":     props.get(date_prop, {}).get("type", "date"),
        "day":      props.get(day_prop, {}).get("type") if day_prop in props else None,
        "time":     props.get(time_prop, {}).get("type", "rich_text"),
        "location": props.get(location_prop, {}).get("type", "rich_text"),
        "people":   props.get(people_prop, {}).get("type", "rich_text"),
    }

    logging.info("Using Notion props -> title:%s date:%s day:%s time:%s location:%s people:%s",
                 title_prop, date_prop, day_prop, time_prop, location_prop, people_prop)

    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    url = "https://api.notion.com/v1/pages"

    def prop_title(val: str):
        return {"title": [{"text": {"content": val}}]}

    def prop_date(d: date):
        return {"date": {"start": d.isoformat()}}

    def prop_rich(val: str):
        return {"rich_text": [{"text": {"content": val}}]} if val else {"rich_text": []}

    def prop_select(val: str):
        return {"select": {"name": val}} if val else {"select": None}

    created = 0
    for r in rows:
        payload_props = {
            title_prop: prop_title(r["_title_content"]),
            date_prop:  prop_date(r["Date"]),
            time_prop:  prop_rich(r["Time"]),
            people_prop: prop_rich(r["People"]),
        }

        if prop_types["location"] == "select":
            payload_props[location_prop] = prop_select(r["Location"])
        else:
            payload_props[location_prop] = prop_rich(r["Location"])

        if day_prop and prop_types["day"] not in ("formula", None):
            if prop_types["day"] == "select":
                payload_props[day_prop] = prop_select(r["Day of the Week"])
            else:
                payload_props[day_prop] = prop_rich(r["Day of the Week"])

        payload = {"parent": {"database_id": NOTION_DB}, "properties": payload_props}
        resp = requests.post(url, headers=headers, data=json.dumps(payload))
        if resp.status_code in (200, 201):
            created += 1
            logging.info("Notion created page titled: %s", r["_title_content"])
        else:
            logging.error("Notion error %s: %s", resp.status_code, resp.text)

    logging.info("Done. Created %d pages.", created)

# ---------------- main ----------------

def main():
    svc = gmail_service()
    subject, body, html = fetch_latest_email(svc)
    if not body:
        logging.warning("No email body found. Check your query or sender.")
        return

    with open("last_email.txt", "w", encoding="utf-8") as f:
        f.write(body)
    if html:
        with open("last_email.html", "w", encoding="utf-8") as f:
            f.write(html)

    logging.info("Parsing schedule...")
    rows = parse_rows(subject or "", body)
    if not rows:
        logging.warning("Parsed zero rows. If you only want your shifts, confirm YOUR_NAME or set FILTER_BY_NAME=false.")
        return

    logging.info("Parsed %d row(s). Example: %s", len(rows), rows[0])
    notion_create(rows)

if __name__ == "__main__":
    main()
