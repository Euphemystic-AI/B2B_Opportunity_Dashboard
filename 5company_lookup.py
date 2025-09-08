#!/usr/bin/env python3
"""
4company_lookup_refactored.py — Prompt01 edition
------------------------------------------------
This refactor replaces the original GICS-only prompt/parse flow with the
Prompt01.txt schema so each company record includes company facts,
industry classification, AI/Opensearch scoring, and AFI metrics.

Key changes vs. the GICS edition:
- Reads the Prompt01.txt file and splits it into SYSTEM and USER messages.
- Renders the USER message by injecting company facts from member_index.json.
- Requests a strict JSON object response (response_format="json_object").
- Merges the AI JSON with the original company facts for indexing.
- Enforces AFI rounding and AFI band (High/Mid/Low) post-parse.
- Removes the GICS mapping requirement (kept doc structure simple).

Environment variables expected (no insecure defaults):
  OPENAI_API_KEY        : your API key (required)
  OPENAI_MODEL          : default "gpt-4o" (optional)
  OS_URL                : OpenSearch bulk URL, e.g. "https://host:9200/index/_bulk" (required)
  OS_USERNAME           : OpenSearch user (required)
  OS_PASSWORD           : OpenSearch password (required)
  OS_CA_CERT            : Path to CA cert file if TLS verify (optional/recommended)
  MEMBER_JSON_PATH      : Path to companies JSON (default: "/apps/chamber/member_index.json")
  PROMPT01_PATH         : Path to Prompt01.txt (default: "./Prompt01.txt")
  LOG_DIR               : Where to write logs (default: "./logs" if unwritable otherwise)
"""

import os, json, re
from datetime import datetime
import openai, requests
from tenacity import retry, wait_exponential, stop_after_attempt
from tqdm import tqdm

# ─────────────── CONFIG ───────────────
def getenv_required(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise SystemExit(f"Missing required environment variable: {name}")
    return val

OPENAI_MODEL     = os.getenv("OPENAI_MODEL", "gpt-4o")

OS_URL           = getenv_required("OS_URL")
OS_USERNAME      = getenv_required("OS_USERNAME")
OS_PASSWORD      = getenv_required("OS_PASSWORD")
OS_CA_CERT       = os.getenv("OS_CA_CERT")  # optional but recommended

MEMBER_JSON_PATH = os.getenv("MEMBER_JSON_PATH", "/apps/chamber/member_index.json")
PROMPT01_PATH    = os.getenv("PROMPT01_PATH", "./Prompt01.txt")

LOG_DIR = os.getenv("LOG_DIR", "/apps/chamber")
if not os.access(LOG_DIR, os.W_OK):
    LOG_DIR = "./logs"
os.makedirs(LOG_DIR, exist_ok=True)
logfile = os.path.join(LOG_DIR, f"run_{datetime.now():%Y%m%d_%H%M%S}.log")

def log(msg, also_print=True):
    ts = datetime.now().isoformat(timespec="seconds")
    with open(logfile, "a", encoding="utf-8") as f:
        f.write(f"{ts} {msg}\n")
    if also_print:
        print(f"{ts} {msg}")

log("===== Script Started (Prompt01 mode) =====")

# ─────────────── UTILITIES ───────────────
def slurp(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()

def stringify(x):
    """Convert elements to printable strings suitable for prompt injection."""
    if isinstance(x, dict):
        # prefer human-friendly keys if present
        for key in ("url", "platform", "name", "value"):
            if key in x and x[key]:
                return str(x[key])
        return json.dumps(x, ensure_ascii=False)
    if isinstance(x, list):
        return ", ".join(stringify(i) for i in x)
    if x is None:
        return ""
    return str(x)

def render_placeholders(template: str, company: dict) -> str:
    """Replace {field} placeholders with company values; remove any leftovers."""
    out = template
    # Perform simple {key} replacement using string keys in the company dict
    for k, v in company.items():
        out = out.replace("{" + k + "}", stringify(v))
    # Remove any unreplaced placeholders
    out = re.sub(r"\{[a-zA-Z0-9_]+\}", "", out)
    return out

def unify_company_record(rec: dict) -> dict:
    """Normalize differing input schemas into the expected fields.
    - Supports both member_index.json and boulder_chamber_directory_compiled.json
    - Populates: company_name, primary_address, website_url, social_links, about_html
    - Preserves original keys as-is where possible
    """
    out = dict(rec)

    # company_name: fall back to 'name'
    if not out.get("company_name") and out.get("name"):
        out["company_name"] = out.get("name")

    # primary_address: handle nested address object or string
    if not out.get("primary_address"):
        addr = out.get("address")
        if isinstance(addr, dict):
            parts = [addr.get("street"), addr.get("city"), addr.get("region"), addr.get("postal_code")]
            out["primary_address"] = ", ".join([p for p in parts if p])
        elif isinstance(addr, str):
            out["primary_address"] = addr
        else:
            out.setdefault("primary_address", "")

    # website_url: common synonyms
    if not out.get("website_url") and out.get("website"):
        out["website_url"] = out.get("website")

    # source_url: map detail_url if present (kept for traceability)
    if not out.get("source_url") and out.get("detail_url"):
        out["source_url"] = out.get("detail_url")

    # social_links: ensure list type even if missing
    if out.get("social_links") is None:
        out["social_links"] = []
    if "social_links" not in out:
        # try common alternatives; default to empty list
        links = out.get("social") or out.get("socials") or []
        out["social_links"] = links if isinstance(links, list) else ([links] if links else [])

    # about_html: fall back to 'about' or 'description'
    if not out.get("about_html"):
        about = out.get("about") or out.get("description") or ""
        out["about_html"] = about

    return out

def split_prompt01(prompt_text: str) -> tuple[str, str]:
    """
    Split the Prompt01.txt content into SYSTEM and USER blocks.
    If markers are missing, treat entire text as USER content.
    """
    # Use case-insensitive markers "SYSTEM:" and "USER:"
    m = re.search(r"(?is)SYSTEM:\s*(.*?)\n\s*USER:\s*(.*)\Z", prompt_text)
    if m:
        system_text = m.group(1).strip()
        user_text   = m.group(2).strip()
    else:
        system_text = ""
        user_text = prompt_text.strip()
    return system_text, user_text

# ─────────────── LOAD DATA ───────────────
try:
    companies = json.load(open(MEMBER_JSON_PATH, encoding="utf-8"))
    if not isinstance(companies, list):
        raise ValueError("member_index.json must be a list of company objects")
    # Normalize records to a common schema so either file format works
    companies = [unify_company_record(c) for c in companies]
    log(f"Loaded {len(companies):,} companies from {MEMBER_JSON_PATH}")
except Exception as e:
    raise SystemExit(f"Cannot load companies: {e}")

prompt01_text = slurp(PROMPT01_PATH)
SYSTEM_TEXT, USER_TEMPLATE = split_prompt01(prompt01_text)
if SYSTEM_TEXT:
    log("Parsed Prompt01.txt into SYSTEM and USER messages")
else:
    log("Prompt01.txt has no explicit SYSTEM block; using single USER message")

# ─────────────── OPENAI ───────────────
openai.api_key = getenv_required("OPENAI_API_KEY")

@retry(wait=wait_exponential(multiplier=2, min=2, max=20), stop=stop_after_attempt(3))
def ask_openai(system_text: str, user_text: str, name: str) -> str:
    log(f"→ OpenAI request for {name}")
    messages = []
    if system_text:
        messages.append({"role": "system", "content": system_text})
    messages.append({"role": "user", "content": user_text})

    r = openai.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.2,
        timeout=120,
    )
    return r.choices[0].message.content

# ─────────────── ENFORCERS / NORMALIZERS ───────────────
def normalize_afi(doc: dict):
    """Round afi_score and ensure afi_band matches thresholds."""
    try:
        # Coerce afi_score to float and round to one decimal
        score = float(doc.get("afi_score"))
        score = round(score, 1)
        doc["afi_score"] = score
        # Determine band if missing or inconsistent
        band = doc.get("afi_band", "")
        def band_for(x: float) -> str:
            if x >= 1.0: return "High"
            if x >= 0.5: return "Mid"
            return "Low"
        correct_band = band_for(score)
        if band not in ("High", "Mid", "Low") or band != correct_band:
            doc["afi_band"] = correct_band
    except Exception:
        # If afi_score missing or invalid, leave as-is; add a soft warning
        doc.setdefault("validation_warning", "afi_score_invalid")

def ensure_required_keys(doc: dict):
    """Make sure required keys from Prompt01 exist, even if null."""
    required = [
        "company_name", "primary_address", "website_url", "social_links", "about_html",
        "main_industry",
        "ai_benefit_score", "ai_benefit_reason",
        "data_volume_score", "data_volume_reason",
        "opensearch_score", "opensearch_reason",
        "ai_initiative_maturity_score", "ai_initiative_maturity_reason",
        "afi_score", "afi_band", "afi_reason"
    ]
    for k in required:
        doc.setdefault(k, None)

# ─────────────── BULK PIPELINE ───────────────
bulk = []

def add(company: dict, ai_json: str):
    """Merge company facts with AI JSON and add to bulk payload."""
    doc_id = ((company.get("company_name") or company.get("name") or "noid")
              .replace(" ", "_").replace("/", "").lower())

    doc = dict(company)  # start with existing facts
    try:
        ai_obj = json.loads(ai_json)
        if not isinstance(ai_obj, dict):
            raise ValueError("AI response was not a JSON object")
        doc.update(ai_obj)
    except Exception as e:
        log(f"JSON error for {doc_id}: {e}")
        doc["validation_warning"] = "json_decode_failure"

    # Enforce Prompt01 invariants
    ensure_required_keys(doc)
    normalize_afi(doc)

    # Add to NDJSON bulk body
    bulk.extend([
        json.dumps({"index": {"_id": doc_id}}),
        json.dumps(doc, ensure_ascii=False)
    ])

def flush():
    if not bulk:
        return
    body = "\n".join(bulk) + "\n"
    try:
        r = requests.post(
            OS_URL,
            headers={"Content-Type": "application/x-ndjson"},
            data=body.encode("utf-8"),
            auth=(OS_USERNAME, OS_PASSWORD),
            verify=OS_CA_CERT if OS_CA_CERT else True,
            timeout=180,
        )
        if not r.ok or '"errors":true' in r.text:
            log(f"OpenSearch bulk error: {r.status_code} {r.text[:600]}")
        else:
            log(f"Bulk ok – {len(bulk)//2} docs")
    finally:
        bulk.clear()

# ─────────────── MAIN LOOP ───────────────
for idx, company in enumerate(tqdm(companies, desc="Collecting Prompt01 fields")):
    name = company.get("company_name", f"idx_{idx}")
    try:
        user_msg = render_placeholders(USER_TEMPLATE, company)
        ai_json = ask_openai(SYSTEM_TEXT, user_msg, name)
        add(company, ai_json)
        # Flush roughly every 50 docs (100 lines): keep payloads modest
        if len(bulk) >= 100:
            flush()
    except Exception as e:
        log(f"⚠️ {name}: {e}")

flush()
log("===== Script Finished =====")
