"""Core logic: compare billed seats (Stripe CSV) with active users (user CSV) by email domain.

Pure Python, no Flask dependency, nothing is stored.
"""
import csv
import io
import re
from datetime import date, timedelta

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 20000

BILLED_STATUSES = {"active", "trialing", "past_due", "pastdue"}
INACTIVE_VALUES = {
    "inactive", "disabled", "deactivated", "suspended", "deleted", "removed",
    "blocked", "archived", "banned", "false", "0", "no", "n", "f", "left",
}
GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com",
    "gmx.com", "mail.com", "yandex.com", "msn.com",
}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")

STRIPE_EMAIL = ["customeremail", "email", "billingemail", "customersemail"]
STRIPE_QTY = ["quantity", "seats", "seatcount", "qty", "licenses", "seatsquantity"]
STRIPE_STATUS = ["status", "subscriptionstatus"]
STRIPE_NAME = ["customername", "name", "company", "customer", "customerdescription", "description"]
STRIPE_PLAN = ["plan", "product", "productname", "planname", "priceid", "plannickname"]
USER_EMAIL = ["email", "emailaddress", "useremail", "mail", "workemail"]
USER_STATUS = ["status", "state", "accountstatus", "userstatus"]
USER_ACTIVE = ["active", "isactive", "enabled", "isenabled"]
USER_DELETED = ["deletedat", "deactivatedat", "disabledat"]


def _norm(h):
    return re.sub(r"[^a-z0-9]", "", str(h or "").lower())


def _find(headers, candidates):
    mapping = {_norm(h): h for h in headers if h is not None}
    for c in candidates:
        if c in mapping:
            return mapping[c]
    return None


def decode_upload(data, label):
    """Bytes -> text, with clear errors for binary / empty input."""
    if data is None or len(data.strip()) == 0:
        raise ValueError(f"The {label} is empty.")
    notes = []
    if len(data) > MAX_BYTES:
        data = data[:MAX_BYTES]
        cut = data.rfind(b"\n")
        if cut > 0:
            data = data[:cut]
        notes.append(f"The {label} is larger than 5 MB, so only the first part was analyzed.")
    if b"\x00" in data[:8192] or data[:4] == b"PK\x03\x04" or data[:4] == b"%PDF":
        raise ValueError(f"The {label} does not look like a text file. Please export it as CSV.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else data.decode("cp1252")
        except UnicodeError:
            raise ValueError(f"The {label} does not look like a text file. Please export it as CSV.")
    sample = text[:4000]
    bad = sum(1 for ch in sample if ord(ch) < 32 and ch not in "\r\n\t")
    if sample and bad / len(sample) > 0.02:
        raise ValueError(f"The {label} does not look like a text file. Please export it as CSV.")
    return text, notes


def parse_csv(text, label):
    """Return (headers, rows, notes)."""
    notes = []
    text = text.lstrip("﻿")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if len(lines) < 2:
        raise ValueError(f"The {label} needs a header row and at least one data row.")
    head = lines[0]
    delim = max([",", ";", "\t", "|"], key=head.count)
    if head.count(delim) == 0:
        delim = ","
    reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=delim)
    try:
        headers = [h.strip() for h in next(reader)]
        rows = []
        for i, rec in enumerate(reader):
            if i >= MAX_ROWS:
                notes.append(f"The {label} has more than {MAX_ROWS:,} rows; only the first {MAX_ROWS:,} were analyzed.")
                break
            rows.append({headers[j]: (rec[j].strip() if j < len(rec) else "") for j in range(len(headers))})
    except csv.Error:
        raise ValueError(f"The {label} could not be read as CSV.")
    return headers, rows, notes


def _domain(value):
    m = EMAIL_RE.search(value or "")
    if not m:
        return None, None
    return m.group(0).lower(), m.group(1).lower()


def _to_int(value):
    try:
        n = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if n < 0 or n != n:
        return None
    return int(round(n))


def _pl(n, one, many):
    return f"{n:,} {one if n == 1 else many}"


def analyze_stripe(text):
    headers, rows, notes = parse_csv(text, "Stripe file")
    c_email = _find(headers, STRIPE_EMAIL)
    c_qty = _find(headers, STRIPE_QTY)
    if not c_email:
        raise ValueError("The Stripe file has no email column. Expected a header such as 'Customer Email'. Found columns: " + ", ".join(h for h in headers if h) + ".")
    if not c_qty:
        raise ValueError("The Stripe file has no seat quantity column. Expected a header such as 'Quantity'. Found columns: " + ", ".join(h for h in headers if h) + ".")
    c_status = _find(headers, STRIPE_STATUS)
    c_name = _find(headers, STRIPE_NAME)
    c_plan = _find(headers, STRIPE_PLAN)
    accounts = {}
    skipped_status = skipped_bad = 0
    generic = []
    for r in rows:
        email, dom = _domain(r.get(c_email, ""))
        qty = _to_int(r.get(c_qty, ""))
        if not dom or qty is None:
            skipped_bad += 1
            continue
        if c_status:
            st = _norm(r.get(c_status, ""))
            if st and st not in BILLED_STATUSES:
                skipped_status += 1
                continue
        if dom in GENERIC_DOMAINS:
            generic.append(email)
            continue
        a = accounts.setdefault(dom, {"billed": 0, "subs": 0, "name": "", "plan": ""})
        a["billed"] += qty
        a["subs"] += 1
        if c_name and not a["name"]:
            nm = r.get(c_name, "")
            if nm and not EMAIL_RE.fullmatch(nm):
                a["name"] = nm
        if c_plan and not a["plan"]:
            a["plan"] = r.get(c_plan, "")
    if not accounts:
        raise ValueError("No usable billed subscriptions were found in the Stripe file (check that rows have a business email, a quantity and an active/trialing/past_due status).")
    if skipped_status:
        notes.append(f"{_pl(skipped_status, 'Stripe row was', 'Stripe rows were')} ignored because the subscription is not active, trialing or past due.")
    if skipped_bad:
        notes.append(f"{_pl(skipped_bad, 'Stripe row was', 'Stripe rows were')} skipped because the email or quantity could not be read.")
    if generic:
        notes.append(f"{_pl(len(generic), 'Stripe subscription uses', 'Stripe subscriptions use')} a free email provider (e.g. gmail.com) and cannot be matched by domain.")
    return accounts, notes


def _user_is_active(r, c_status, c_active, c_deleted):
    if c_deleted and (r.get(c_deleted) or "").strip():
        return False
    if c_active:
        v = (r.get(c_active) or "").strip().lower()
        if v in INACTIVE_VALUES:
            return False
    if c_status:
        v = (r.get(c_status) or "").strip().lower()
        if v in INACTIVE_VALUES:
            return False
    return True


def analyze_users(text):
    headers, rows, notes = parse_csv(text, "user file")
    c_email = _find(headers, USER_EMAIL)
    if not c_email:
        for h in headers:
            vals = [r.get(h, "") for r in rows[:50]]
            if vals and sum(1 for v in vals if _domain(v)[0]) >= max(1, len(vals) // 2):
                c_email = h
                break
    if not c_email:
        raise ValueError("The user file has no email column. Expected a header such as 'email'. Found columns: " + ", ".join(h for h in headers if h) + ".")
    c_status = _find(headers, USER_STATUS)
    c_active = _find(headers, USER_ACTIVE)
    c_deleted = _find(headers, USER_DELETED)
    seen = set()
    domains = {}
    inactive = bad = generic = dup = 0
    for r in rows:
        email, dom = _domain(r.get(c_email, ""))
        if not dom:
            bad += 1
            continue
        if email in seen:
            dup += 1
            continue
        seen.add(email)
        if not _user_is_active(r, c_status, c_active, c_deleted):
            inactive += 1
            continue
        if dom in GENERIC_DOMAINS:
            generic += 1
            continue
        domains[dom] = domains.get(dom, 0) + 1
    if not seen:
        raise ValueError("No readable email addresses were found in the user file.")
    if inactive:
        notes.append(f"{_pl(inactive, 'user was', 'users were')} not counted because they are marked inactive or deleted.")
    if bad:
        notes.append(f"{_pl(bad, 'user row was', 'user rows were')} skipped because no valid email was found.")
    if dup:
        notes.append(f"{_pl(dup, 'duplicate email was', 'duplicate emails were')} counted once.")
    if generic:
        notes.append(f"{_pl(generic, 'user has', 'users have')} a free email provider (e.g. gmail.com) and cannot be matched by domain.")
    if not domains:
        raise ValueError("No active users with a company email domain were found in the user file.")
    return domains, notes


def parse_price(value):
    value = (value or "").strip().replace("$", "").replace(",", "")
    if not value:
        return None
    try:
        p = float(value)
    except ValueError:
        raise ValueError("Price per seat must be a number, for example 15 or 12.50.")
    if p < 0 or p > 1000000 or p != p:
        raise ValueError("Price per seat must be a positive number.")
    return p


def compare(accounts, domains, price=None):
    rows = []
    for dom in set(accounts) | set(domains):
        acc = accounts.get(dom)
        billed = acc["billed"] if acc else 0
        actual = domains.get(dom, 0)
        diff = actual - billed
        if not acc:
            status = "nosub"
        elif diff > 0:
            status = "under"
        elif diff < 0:
            status = "over"
        else:
            status = "ok"
        rows.append({
            "domain": dom,
            "name": (acc or {}).get("name") or "",
            "plan": (acc or {}).get("plan") or "",
            "billed": billed,
            "actual": actual,
            "diff": diff,
            "status": status,
            "revenue": (diff * price) if (price is not None and diff > 0 and status == "under") else None,
        })
    order = {"under": 0, "nosub": 1, "over": 2, "ok": 3}
    rows.sort(key=lambda x: (order[x["status"]], -abs(x["diff"]), x["domain"]))
    return rows


def summarize(rows, price):
    under = [r for r in rows if r["status"] == "under"]
    s = {
        "under": len(under),
        "missing_seats": sum(r["diff"] for r in under),
        "over": sum(1 for r in rows if r["status"] == "over"),
        "ok": sum(1 for r in rows if r["status"] == "ok"),
        "nosub": sum(1 for r in rows if r["status"] == "nosub"),
        "total": len(rows),
        "revenue": (sum(r["revenue"] for r in under) if price is not None else None),
    }
    return s


def export_csv(rows, price):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["domain", "account", "plan", "billed_seats", "active_users", "missing_seats", "price_per_seat", "estimated_extra_revenue_per_period"])
    for r in rows:
        if r["status"] != "under":
            continue
        w.writerow([
            r["domain"], r["name"], r["plan"], r["billed"], r["actual"], r["diff"],
            "" if price is None else f"{price:.2f}",
            "" if r["revenue"] is None else f"{r['revenue']:.2f}",
        ])
    return out.getvalue()


def run(stripe_text, users_text, price_text=""):
    price = parse_price(price_text)
    accounts, n1 = analyze_stripe(stripe_text)
    domains, n2 = analyze_users(users_text)
    rows = compare(accounts, domains, price)
    return {
        "rows": rows,
        "summary": summarize(rows, price),
        "notes": n1 + n2,
        "price": price,
        "csv": export_csv(rows, price),
    }


def demo_data(today=None):
    """Embedded demo data with dates relative to today."""
    today = today or date.today()
    plan = [
        ("acme.io", "Acme Robotics", 5, 8, "active", 210),
        ("globex.com", "Globex", 10, 10, "active", 340),
        ("initech.co", "Initech", 3, 7, "active", 95),
        ("umbrella.dev", "Umbrella Labs", 20, 14, "active", 400),
        ("hooli.com", "Hooli", 12, 15, "trialing", 12),
        ("wayne.com", "Wayne Tech", 6, 2, "canceled", 280),
        ("soylent.app", "Soylent", 4, 4, "active", 150),
    ]
    names = ["ana", "ben", "chloe", "dev", "emma", "finn", "gus", "hana", "ivan", "jo",
             "kai", "lena", "max", "nora", "omar", "pia", "quinn", "ravi", "sara", "tom"]
    s = io.StringIO()
    w = csv.writer(s)
    w.writerow(["id", "Customer Email", "Customer Name", "Status", "Plan", "Quantity", "Created (UTC)"])
    for i, (dom, comp, billed, _, st, ago) in enumerate(plan, 1):
        w.writerow([f"sub_{1000 + i}", f"billing@{dom}", comp, st, "Team", billed, (today - timedelta(days=ago)).isoformat()])
    w.writerow(["sub_2001", "solo.founder@gmail.com", "Solo Founder", "active", "Team", 1, (today - timedelta(days=30)).isoformat()])
    stripe_csv = s.getvalue()
    u = io.StringIO()
    w = csv.writer(u)
    w.writerow(["user_id", "email", "status", "last_login"])
    uid = 1
    for dom, _, _, real, _, _ in plan:
        for k in range(real):
            w.writerow([uid, f"{names[k]}@{dom}", "active", (today - timedelta(days=k % 9)).isoformat()])
            uid += 1
    for k in range(4):
        w.writerow([uid, f"{names[k]}@stark.ai", "active", (today - timedelta(days=k)).isoformat()])
        uid += 1
    w.writerow([uid, "old.user@globex.com", "deactivated", (today - timedelta(days=120)).isoformat()])
    users_csv = u.getvalue()
    return stripe_csv, users_csv
