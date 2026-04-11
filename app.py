import csv
import html as _html
import io
import time
from urllib.parse import quote as _url_quote
import streamlit as st
import httpx
import anthropic
from datetime import date as date_type, datetime as datetime_type
from supabase import create_client, Client

st.set_page_config(
    page_title="Backlog Refinement Advisor",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Hide Streamlit chrome (runs on every render, including login) ──────────────
st.markdown("""
<style>
#MainMenu  { visibility: hidden; }
footer     { visibility: hidden; }
header     { display: none !important; }
[data-testid="stSidebar"]        { display: none !important; }
[data-testid="collapsedControl"] { display: none !important; }
[data-testid="stToolbar"]        { display: none !important; }
[data-testid="stDecoration"]     { display: none !important; }
[data-testid="stStatusWidget"]   { display: none !important; }
.stApp { background-color: #f4f6f9 !important; }

/* ── Global button overrides ── */
button[data-testid="stBaseButton-primary"] {
    background-color: #1565C0 !important;
    border-color:     #1565C0 !important;
    color: #fff !important;
}
button[data-testid="stBaseButton-primary"]:hover {
    background-color: #1251a3 !important;
    border-color:     #1251a3 !important;
}
button[data-testid="stBaseButton-secondary"] {
    background-color: #fff !important;
    border-color:     #d0d4db !important;
    color: #1e2a3a !important;
}
button[data-testid="stBaseButton-secondary"]:hover {
    background-color: #f8f9fb !important;
}
button[data-testid="stBaseButton-primaryFormSubmit"] {
    background-color: #1565C0 !important;
    border-color:     #1565C0 !important;
    color: #fff !important;
}
button[data-testid="stBaseButton-primaryFormSubmit"]:hover {
    background-color: #1251a3 !important;
    border-color:     #1251a3 !important;
}
</style>
""", unsafe_allow_html=True)


# ── Constants ─────────────────────────────────────────────────────────────────
OUTCOME_OPTIONS = [
    ("Ready for Sprint",        "#27ae60"),
    ("Needs More Refinement",   "#2980b9"),
    ("Return to Product Owner", "#e67e22"),
    ("Defer",                   "#7f8c8d"),
    ("Split Required",          "#8e44ad"),
]

# ── Supabase client ────────────────────────────────────────────────────────────
def get_supabase() -> Client:
    if "supabase_client" not in st.session_state:
        st.session_state["supabase_client"] = create_client(
            st.secrets["supabase_url"],
            st.secrets["supabase_anon_key"],
        )
    return st.session_state["supabase_client"]


# ── Token refresh helpers ──────────────────────────────────────────────────────
def _raw_token_refresh(refresh_token: str) -> dict | None:
    try:
        url  = f"{st.secrets['supabase_url']}/auth/v1/token?grant_type=refresh_token"
        hdrs = {"apikey": st.secrets["supabase_anon_key"], "Content-Type": "application/json"}
        resp = httpx.post(url, json={"refresh_token": refresh_token}, headers=hdrs, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        st.session_state["debug_refresh_detail"] = f"HTTP {resp.status_code}: {resp.text[:300]}"
    except Exception as e:
        st.session_state["debug_refresh_detail"] = f"Exception: {str(e)[:300]}"
    return None


def _parse_expires_at(data: dict) -> float:
    expires_in = data.get("expires_in", 3600)
    return time.time() + float(expires_in or 3600)


def restore_session() -> bool:
    if not st.session_state.get("access_token"):
        return False

    expires_at = st.session_state.get("expires_at", 0)

    if time.time() >= expires_at - 60:
        data = _raw_token_refresh(st.session_state.get("refresh_token", ""))
        if not (data and data.get("access_token")):
            clear_session()
            return False
        st.session_state["access_token"]  = data["access_token"]
        st.session_state["refresh_token"] = data.get("refresh_token", st.session_state["refresh_token"])
        st.session_state["expires_at"]    = _parse_expires_at(data)
        st.session_state.pop("supabase_client", None)

    try:
        get_supabase().auth.set_session(
            st.session_state["access_token"],
            st.session_state["refresh_token"],
        )
    except Exception:
        data = _raw_token_refresh(st.session_state.get("refresh_token", ""))
        if data and data.get("access_token"):
            st.session_state["access_token"]  = data["access_token"]
            st.session_state["refresh_token"] = data.get("refresh_token", st.session_state["refresh_token"])
            st.session_state["expires_at"]    = _parse_expires_at(data)
            st.session_state.pop("supabase_client", None)
            try:
                get_supabase().auth.set_session(data["access_token"], data.get("refresh_token", ""))
            except Exception:
                pass
        else:
            clear_session()
            return False
    return True


def clear_session():
    try:
        if "sid" in st.query_params:
            del st.query_params["sid"]
    except Exception:
        pass
    for key in ["access_token", "refresh_token", "expires_at", "user_id", "user_email",
                "current_team_id", "current_team_name",
                "current_session_id", "current_session_name",
                "page", "supabase_client", "session_id"]:
        st.session_state.pop(key, None)
    for key in list(st.session_state.keys()):
        if key.startswith("_buf_"):
            del st.session_state[key]


# ── Server-side session store ──────────────────────────────────────────────────
def create_server_session() -> str | None:
    try:
        result = db().table("user_sessions").insert({
            "user_id":       st.session_state["user_id"],
            "access_token":  st.session_state["access_token"],
            "refresh_token": st.session_state["refresh_token"],
        }).execute()
        return result.data[0]["id"]
    except Exception:
        return None


def load_server_session(sid: str) -> bool:
    try:
        result = get_supabase().table("user_sessions").select("*").eq("id", sid).execute()
        if result.data:
            row = result.data[0]
            st.session_state["access_token"]  = row["access_token"]
            st.session_state["refresh_token"] = row["refresh_token"]
            st.session_state["user_id"]       = row["user_id"]
            st.session_state["session_id"]    = sid
            if row.get("current_page"):
                st.session_state["page"] = row["current_page"]
            if row.get("current_team_id"):
                st.session_state["current_team_id"] = row["current_team_id"]
            if row.get("current_team_name"):
                st.session_state["current_team_name"] = row["current_team_name"]
            if row.get("current_session_id"):
                st.session_state["current_session_id"] = row["current_session_id"]
            if row.get("current_session_name"):
                st.session_state["current_session_name"] = row["current_session_name"]
            return True
        return False
    except Exception:
        return False


def update_server_session():
    sid = st.session_state.get("session_id")
    if not sid:
        return
    try:
        db().table("user_sessions").update({
            "access_token":         st.session_state["access_token"],
            "refresh_token":        st.session_state["refresh_token"],
            "current_page":         st.session_state.get("page", "teams"),
            "current_team_id":      st.session_state.get("current_team_id"),
            "current_team_name":    st.session_state.get("current_team_name"),
            "current_session_id":   st.session_state.get("current_session_id"),
            "current_session_name": st.session_state.get("current_session_name"),
        }).eq("id", sid).execute()
    except Exception:
        pass


def delete_server_session():
    sid = st.session_state.get("session_id")
    if not sid:
        return
    try:
        db().table("user_sessions").delete().eq("id", sid).execute()
    except Exception:
        pass


# ── Auth helpers ───────────────────────────────────────────────────────────────
def is_authenticated() -> bool:
    return bool(st.session_state.get("access_token"))


def is_auth_error(e: Exception) -> bool:
    msg = str(e).lower()
    return any(k in msg for k in ["jwt expired", "invalid jwt", "token expired",
                                   "not authenticated", "refresh token"])


def do_login(email: str, password: str):
    try:
        r = get_supabase().auth.sign_in_with_password({"email": email, "password": password})
        st.session_state["access_token"]  = r.session.access_token
        st.session_state["refresh_token"] = r.session.refresh_token
        st.session_state["expires_at"]    = time.time() + 3600
        st.session_state["user_id"]       = r.user.id
        st.session_state["user_email"]    = r.user.email
        st.session_state["page"]          = "teams"
        sid = create_server_session()
        if sid:
            st.session_state["session_id"] = sid
            st.query_params["sid"] = sid
        return None
    except Exception as e:
        return str(e)


def do_signup(email: str, password: str):
    allowed_domain = st.secrets.get("allowed_email_domain", "")
    if allowed_domain and not email.lower().endswith(f"@{allowed_domain.lower()}"):
        return f"Sign up is restricted to @{allowed_domain} email addresses.", None
    try:
        get_supabase().auth.sign_up({"email": email, "password": password})
        return None, "Account created. Check your email to confirm before logging in."
    except Exception as e:
        return str(e), None


def do_logout():
    try:
        get_supabase().auth.sign_out()
    except Exception:
        pass
    delete_server_session()
    clear_session()


def handle_password_recovery(token_hash: str = None, code: str = None,
                             access_token: str = None, refresh_token: str = None):
    st.title("Reset Your Password")

    if "recovery_session_set" not in st.session_state:
        try:
            if token_hash:
                r = get_supabase().auth.verify_otp({"token_hash": token_hash, "type": "recovery"})
            elif code:
                r = get_supabase().auth.exchange_code_for_session({"auth_code": code})
            elif access_token:
                r = get_supabase().auth.set_session(access_token, refresh_token or "")
            else:
                st.error("Invalid recovery link.")
                return
            st.session_state["recovery_access_token"]  = r.session.access_token
            st.session_state["recovery_refresh_token"] = r.session.refresh_token
            st.session_state["recovery_session_set"]   = True
        except Exception as e:
            st.error(f"Recovery link is invalid or expired. Please request a new one. ({e})")
            return

    with st.form("reset_password_form"):
        new_password = st.text_input("New Password", type="password")
        confirm      = st.text_input("Confirm Password", type="password")
        if st.form_submit_button("Set New Password"):
            if not new_password:
                st.warning("Please enter a password.")
            elif new_password != confirm:
                st.error("Passwords do not match.")
            else:
                try:
                    get_supabase().auth.set_session(
                        st.session_state["recovery_access_token"],
                        st.session_state["recovery_refresh_token"],
                    )
                    get_supabase().auth.update_user({"password": new_password})
                    for k in ["recovery_access_token", "recovery_refresh_token", "recovery_session_set"]:
                        st.session_state.pop(k, None)
                    st.success("Password updated. You can now log in.")
                except Exception as e:
                    st.error(f"Failed to update password: {e}")


# ── Database helpers ───────────────────────────────────────────────────────────
def db():
    return get_supabase()


def get_teams() -> list:
    try:
        r = db().table("teams").select("id, name").eq(
            "user_id", st.session_state["user_id"]
        ).order("created_at").execute()
        return r.data or []
    except Exception:
        return []


def get_teams_with_counts() -> list:
    try:
        teams = db().table("teams").select("id, name").eq(
            "user_id", st.session_state["user_id"]
        ).order("created_at").execute().data or []
        if not teams:
            return []
        team_ids = [t["id"] for t in teams]
        sessions = db().table("refinement_sessions").select("team_id").in_(
            "team_id", team_ids
        ).execute().data or []
        count_map: dict = {}
        for s in sessions:
            tid = s["team_id"]
            count_map[tid] = count_map.get(tid, 0) + 1
        for t in teams:
            t["session_count"] = count_map.get(t["id"], 0)
        return teams
    except Exception:
        return []


def create_team(name: str):
    db().table("teams").insert({
        "user_id": st.session_state["user_id"],
        "name":    name,
    }).execute()


def update_team(team_id: str, name: str):
    db().table("teams").update({"name": name}).eq("id", team_id).execute()


def delete_team(team_id: str):
    db().table("teams").delete().eq("id", team_id).execute()


def get_refinement_sessions(team_id: str) -> list:
    try:
        r = db().table("refinement_sessions").select("id, name, status, created_at").eq(
            "team_id", team_id
        ).order("created_at", desc=True).execute()
        return r.data or []
    except Exception:
        return []


def get_refinement_sessions_with_counts(team_id: str) -> list:
    try:
        sessions = db().table("refinement_sessions").select(
            "id, name, status, created_at"
        ).eq("team_id", team_id).order("created_at", desc=True).execute().data or []
        if not sessions:
            return []
        session_ids = [s["id"] for s in sessions]
        items = db().table("backlog_items").select("session_id, outcome").in_(
            "session_id", session_ids
        ).execute().data or []
        total_map:  dict = {}
        tagged_map: dict = {}
        for item in items:
            sid = item["session_id"]
            total_map[sid]  = total_map.get(sid, 0) + 1
            if item.get("outcome"):
                tagged_map[sid] = tagged_map.get(sid, 0) + 1
        for s in sessions:
            s["item_total"]  = total_map.get(s["id"], 0)
            s["item_tagged"] = tagged_map.get(s["id"], 0)
        return sessions
    except Exception:
        return []


def create_refinement_session(team_id: str, name: str):
    db().table("refinement_sessions").insert({
        "team_id": team_id,
        "name":    name,
    }).execute()


def update_refinement_session(session_id: str, name: str):
    db().table("refinement_sessions").update({"name": name}).eq("id", session_id).execute()


def delete_refinement_session(session_id: str):
    db().table("refinement_sessions").delete().eq("id", session_id).execute()


def get_backlog_items(session_id: str) -> list:
    try:
        r = db().table("backlog_items").select("*").eq(
            "session_id", session_id
        ).order("created_at", desc=True).execute()
        return r.data or []
    except Exception:
        return []


def create_backlog_item(session_id, title, description, acceptance_criteria,
                        dependencies, assumptions, notes, clarity, zone, gemini_output):
    db().table("backlog_items").insert({
        "session_id":          session_id,
        "title":               title,
        "description":         description or None,
        "acceptance_criteria": acceptance_criteria or None,
        "dependencies":        dependencies or None,
        "assumptions":         assumptions or None,
        "notes":               notes or None,
        "clarity_gradient":    clarity,
        "threshold_zone":      zone,
        "gemini_output":       gemini_output,
    }).execute()


def delete_backlog_item(item_id: str):
    db().table("backlog_items").delete().eq("id", item_id).execute()


def update_backlog_item_outcome(item_id: str, outcome: str, outcome_notes: str):
    try:
        db().table("backlog_items").update({
            "outcome":       outcome or None,
            "outcome_notes": outcome_notes or None,
        }).eq("id", item_id).execute()
    except Exception as e:
        st.error(f"Failed to save outcome: {e}")


def get_session(session_id: str) -> dict | None:
    try:
        r = db().table("refinement_sessions").select("*").eq("id", session_id).execute()
        return r.data[0] if r.data else None
    except Exception:
        return None


def update_session_status(session_id: str, status: str):
    try:
        db().table("refinement_sessions").update({"status": status}).eq("id", session_id).execute()
    except Exception as e:
        st.error(f"Failed to update session status: {e}")


# ── Claude evaluation ─────────────────────────────────────────────────────────
def run_claude_evaluation(title, description, acceptance_criteria,
                          dependencies, assumptions, notes) -> tuple[str, str, str]:
    client = anthropic.Anthropic(api_key=st.secrets["anthropic_api_key"])

    fields = [f"Title: {title}"]
    if description:          fields.append(f"Description:\n{description}")
    if acceptance_criteria:  fields.append(f"Acceptance Criteria:\n{acceptance_criteria}")
    if dependencies:         fields.append(f"Dependencies: {dependencies}")
    if assumptions:          fields.append(f"Assumptions: {assumptions}")
    if notes:                fields.append(f"Notes:\n{notes}")
    item_text = "\n\n".join(fields)

    prompt = f"""You are an expert Agile coach evaluating a backlog item for sprint readiness.

Evaluate the following backlog item against Mike Cohn's Product Backlog Refinement Checklist.

BACKLOG ITEM:
{item_text}

CHECKLIST (14 items across 4 groups):

1. Shared Understanding
   a. The team can explain the item in their own words
   b. The team understands the problem this item is intended to solve
   c. The team understands what must be true for this item to be considered complete
   d. The team agrees on what is included and what is not included

2. Acceptance Boundaries
   a. The major acceptance criteria have been identified
   b. Obvious edge cases have been discussed
   c. The team understands the key assumptions behind this item

3. Size and Sprint Fit
   a. The item is small enough to be completed within a sprint
   b. If the item is too large, the team knows how it will be split
   c. The item is comparable in size to work the team has successfully completed before
   d. The team feels comfortable committing to this item

4. Risks and Unknowns
   a. Unknowns that could significantly increase scope have been resolved
   b. Dependencies have been identified
   c. Remaining uncertainty feels manageable within the sprint

CLARITY GRADIENT — choose one based on these criteria:
- High Clarity: next sprint or two, clear purpose, acceptance criteria defined, sprint-threatening unknowns resolved, small enough to fit in a sprint
- Moderate Clarity: rough understanding, likely candidate for refinement soon, some unknowns still open
- Low Clarity: rough ideas, minimal descriptions, may change or disappear, not yet refined

REFINEMENT THRESHOLD — choose one based on these criteria:
- Too Vague: major unanswered questions, high likelihood of surprises, team cannot commit responsibly
- Ideal: enough clarity to fit in a sprint, major risks addressed, minor unknowns acceptable
- Over-Refined: trying to eliminate all uncertainty, refining work far in advance, spending more time refining than delivering

COMMON MISTAKES — only flag if clearly evident from the submission:
1. Refining Until Nothing Is Uncertain
2. Refining Too Far Ahead
3. Refining Too Late
4. Turning Refinement Into Design
5. Including Everyone Every Time

Respond using EXACTLY this format (do not deviate):

CLARITY_GRADIENT: [High Clarity / Moderate Clarity / Low Clarity]
THRESHOLD_ZONE: [Too Vague / Ideal / Over-Refined]

---

## Overall Assessment

[2-3 sentences summarising the item's readiness for sprint commitment]

## Clarity: [High / Moderate / Low]

[Explain why this rating was assigned, referencing specific aspects of the submission]

## Refinement: [Too Vague / Ideal / Over-Refined]

[Explain why this zone was assigned]

## Checklist Analysis

### 1. Shared Understanding
[For each of the 4 items, start the line with ✔ (satisfied), ✗ (gap), or ? (uncertain), followed by the checklist item text. For any ✗ or ? items, add 1-2 clarifying questions on indented lines beneath. Example:
✔ Team can explain the item in their own words
✗ Major acceptance criteria have been identified
    What specific acceptance criteria should be defined?
? Item is small enough to complete within a sprint
    Has the team estimated the effort involved?]

### 2. Acceptance Boundaries
[Same format as above]

### 3. Size and Sprint Fit
[Same format as above]

### 4. Risks and Unknowns
[Same format as above]

## Common Mistakes Detected

[Only include content here if any of the 5 mistakes are clearly evident. For each detected mistake: state the mistake name and one sentence explaining why it was flagged. If none are detected, write: None detected.]"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    text = message.content[0].text.strip()

    clarity = "Unknown"
    zone    = "Unknown"
    for line in text.split("\n")[:8]:
        if line.startswith("CLARITY_GRADIENT:"):
            clarity = line.replace("CLARITY_GRADIENT:", "").strip()
        elif line.startswith("THRESHOLD_ZONE:"):
            zone = line.replace("THRESHOLD_ZONE:", "").strip()

    display_text = text
    if "---" in text:
        display_text = text[text.index("---") + 3:].strip()

    return clarity, zone, display_text


# ── Display helpers ───────────────────────────────────────────────────────────
def _clarity_badge(clarity: str) -> str:
    styles = {
        "High":     "background:#27ae6022;color:#27ae60;border:1px solid #27ae6044",
        "Moderate": "background:#e67e2222;color:#e67e22;border:1px solid #e67e2244",
        "Low":      "background:#e74c3c22;color:#e74c3c;border:1px solid #e74c3c44",
    }
    style = styles.get(clarity, "background:#7f8c8d22;color:#7f8c8d;border:1px solid #7f8c8d44")
    return (f'<span style="{style};display:inline-block;padding:2px 10px;'
            f'border-radius:10px;font-size:11px;font-weight:600;white-space:nowrap">{clarity}</span>')


def _zone_badge(zone: str) -> str:
    styles = {
        "Too Vague":    "background:#e74c3c22;color:#e74c3c;border:1px solid #e74c3c44",
        "Ideal":        "background:#27ae6022;color:#27ae60;border:1px solid #27ae6044",
        "Over-Refined": "background:#8e44ad22;color:#8e44ad;border:1px solid #8e44ad44",
    }
    style = styles.get(zone, "background:#7f8c8d22;color:#7f8c8d;border:1px solid #7f8c8d44")
    return (f'<span style="{style};display:inline-block;padding:2px 10px;'
            f'border-radius:10px;font-size:11px;font-weight:600;white-space:nowrap">{zone}</span>')


def _status_badge(status: str) -> str:
    styles = {
        "preparing":   "background:#f2f3f4;color:#546E7A;border:1px solid #cfd8dc",
        "in_progress": "background:#fff3e0;color:#e65100;border:1px solid #ffcc80",
        "complete":    "background:#e8f5e9;color:#2e7d32;border:1px solid #a5d6a7",
    }
    labels = {"preparing": "Preparing", "in_progress": "In Progress", "complete": "Complete"}
    label  = labels.get(status, status.replace("_", " ").title())
    style  = styles.get(status, "background:#f2f3f4;color:#546E7A;border:1px solid #cfd8dc")
    return (f'<span style="{style};display:inline-block;padding:2px 9px;'
            f'border-radius:10px;font-size:11px;font-weight:600;white-space:nowrap">'
            f'{label}</span>')


def _gaps_badge_html(count: int, kind: str = "gap") -> str:
    if count == 0:
        color, bg = "#2e7d32", "#e8f5e9"
    elif kind == "gap":
        color, bg = "#c62828", "#fce4ec"
    else:
        color, bg = "#e65100", "#fff8e1"
    return (f'<span style="display:inline-block;background:{bg};color:{color};'
            f'border-radius:10px;padding:2px 8px;font-size:11px;font-weight:700">{count}</span>')


def _parse_assessment(text: str) -> dict:
    """Split stored Claude markdown into named sections."""
    sections     = {}
    current_key  = None
    current_lines = []

    for line in text.split("\n"):
        if line.startswith("## "):
            if current_key:
                sections[current_key] = "\n".join(current_lines).strip()
            heading = line[3:].strip().lower()
            if heading.startswith("overall"):
                current_key = "overall"
            elif heading.startswith("clarity"):
                current_key = "clarity_reasoning"
            elif heading.startswith("refinement"):
                current_key = "refinement_reasoning"
            elif heading.startswith("checklist"):
                current_key = "checklist"
            elif heading.startswith("common"):
                current_key = "common_mistakes"
            else:
                current_key = heading.replace(" ", "_")
            current_lines = []
        elif current_key is not None:
            current_lines.append(line)

    if current_key:
        sections[current_key] = "\n".join(current_lines).strip()
    return sections


def _first_para(text: str, max_chars: int = 300) -> str:
    """Return the first non-empty paragraph, truncated if needed."""
    for para in text.split("\n\n"):
        para = para.strip()
        if para:
            return para[:max_chars] + ("…" if len(para) > max_chars else "")
    return (text or "")[:max_chars]


def _render_rating_cards_html(clarity_short: str, zone: str,
                               clarity_reasoning: str, refinement_reasoning: str) -> str:
    clarity_colors = {"High": "#27ae60", "Moderate": "#e67e22", "Low": "#e74c3c"}
    zone_colors    = {"Too Vague": "#e74c3c", "Ideal": "#27ae60", "Over-Refined": "#8e44ad"}
    c_color = clarity_colors.get(clarity_short, "#7f8c8d")
    z_color = zone_colors.get(zone, "#7f8c8d")
    c_ctx   = _first_para(clarity_reasoning)
    z_ctx   = _first_para(refinement_reasoning)
    return f"""
<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px">
  <div style="background:#fff;border-radius:8px;padding:14px 16px;
              border:1px solid #e0e3e8;border-top:4px solid {c_color}">
    <div style="font-size:10px;text-transform:uppercase;letter-spacing:0.6px;
                color:#999;margin-bottom:5px">Clarity</div>
    <div style="font-size:18px;font-weight:700;color:{c_color};margin-bottom:8px">{clarity_short}</div>
    <div style="font-size:13px;color:#555;line-height:1.5">{c_ctx}</div>
  </div>
  <div style="background:#fff;border-radius:8px;padding:14px 16px;
              border:1px solid #e0e3e8;border-top:4px solid {z_color}">
    <div style="font-size:10px;text-transform:uppercase;letter-spacing:0.6px;
                color:#999;margin-bottom:5px">Refinement</div>
    <div style="font-size:18px;font-weight:700;color:{z_color};margin-bottom:8px">{zone}</div>
    <div style="font-size:13px;color:#555;line-height:1.5">{z_ctx}</div>
  </div>
</div>"""


def _render_overall_callout_html(text: str) -> str:
    return f"""
<div style="background:#EBF5FB;border-left:4px solid #2980b9;padding:12px 16px;
            border-radius:0 6px 6px 0;font-size:14px;color:#333;
            line-height:1.6;margin-bottom:16px">
  <div style="font-size:10px;text-transform:uppercase;letter-spacing:0.6px;
              color:#2980b9;font-weight:700;margin-bottom:6px">Overall</div>
  {text}
</div>"""


def _render_mistakes_callout_html(text: str) -> str:
    if not text or text.strip().lower().startswith("none detected"):
        return ""
    return f"""
<div style="background:#fffde7;border-left:4px solid #f9a825;padding:12px 16px;
            border-radius:0 6px 6px 0;font-size:13px;color:#555;
            line-height:1.6;margin-bottom:16px">
  <strong style="color:#e65100">Common Mistakes Detected:</strong><br>{text}
</div>"""


def _render_outcome_count_bar_html(items: list) -> str:
    outcome_config = [
        ("Ready for Sprint",        "#27ae60", "#e8f8f0"),
        ("Needs More Refinement",   "#2980b9", "#e8f4fd"),
        ("Return to Product Owner", "#e67e22", "#fef3e8"),
        ("Defer",                   "#7f8c8d", "#f2f3f4"),
        ("Split Required",          "#8e44ad", "#f5eef8"),
    ]
    counts = {}
    for item in items:
        key = item.get("outcome") or ""
        counts[key] = counts.get(key, 0) + 1

    html = '<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:4px">'
    for label, color, bg in outcome_config:
        n = counts.get(label, 0)
        html += (
            f'<div style="display:flex;align-items:center;gap:8px;background:{bg};'
            f'border-radius:8px;padding:10px 16px;border:1px solid {color}40;min-width:150px">'
            f'<div style="width:10px;height:10px;border-radius:50%;background:{color};flex-shrink:0"></div>'
            f'<div><div style="font-size:20px;font-weight:700;color:{color}">{n}</div>'
            f'<div style="font-size:11px;color:#888">{label}</div></div></div>'
        )
    pending = counts.get("", 0)
    if pending:
        html += (
            f'<div style="display:flex;align-items:center;gap:8px;background:#f8f9fa;'
            f'border-radius:8px;padding:10px 16px;border:1px solid #e0e3e8;min-width:100px">'
            f'<div style="width:10px;height:10px;border-radius:50%;background:#bdc3c7;flex-shrink:0"></div>'
            f'<div><div style="font-size:20px;font-weight:700;color:#aaa">{pending}</div>'
            f'<div style="font-size:11px;color:#888">Pending</div></div></div>'
        )
    html += '</div>'
    return html


def _render_summary_table_html(items: list) -> str:
    clarity_colors = {"High": "#27ae60", "Moderate": "#e67e22", "Low": "#e74c3c"}
    zone_colors    = {"Too Vague": "#e74c3c", "Ideal": "#27ae60", "Over-Refined": "#8e44ad"}
    outcome_colors = {label: color for label, color in OUTCOME_OPTIONS}

    def badge(text, color):
        return (f'<span style="display:inline-block;padding:3px 10px;border-radius:12px;'
                f'font-size:11px;font-weight:600;white-space:nowrap;'
                f'background:{color}22;color:{color};border:1px solid {color}44">{text}</span>')

    rows = ""
    for item in items:
        clarity_full  = item.get("clarity_gradient", "") or ""
        zone          = item.get("threshold_zone", "") or ""
        outcome       = item.get("outcome", "") or ""
        notes         = item.get("outcome_notes", "") or ""
        clarity_short = clarity_full.replace(" Clarity", "")
        assessed_str  = _format_assessed_date(item.get("created_at", ""))

        c_badge = badge(clarity_short, clarity_colors.get(clarity_short, "#7f8c8d")) if clarity_short else ""
        z_badge = badge(zone,          zone_colors.get(zone, "#7f8c8d"))             if zone          else ""
        o_badge = (badge(outcome, outcome_colors.get(outcome, "#7f8c8d"))
                   if outcome else badge("Pending", "#bdc3c7"))

        notes         = item.get("outcome_notes", "") or ""
        notes_cell    = (
            f'<span style="font-size:12px;color:#777;font-style:italic">'
            f'{_html.escape(notes)}</span>'
            if notes else '<span style="color:#ddd">—</span>'
        )

        rows += (
            f'<tr>'
            f'<td style="padding:11px 14px;border-bottom:1px solid #eef0f3;font-size:13px">'
            f'<strong>{_html.escape(item["title"])}</strong></td>'
            f'<td style="padding:11px 14px;border-bottom:1px solid #eef0f3">{c_badge}</td>'
            f'<td style="padding:11px 14px;border-bottom:1px solid #eef0f3">{z_badge}</td>'
            f'<td style="padding:11px 14px;border-bottom:1px solid #eef0f3">{o_badge}</td>'
            f'<td style="padding:11px 14px;border-bottom:1px solid #eef0f3">{notes_cell}</td>'
            f'<td style="padding:11px 14px;border-bottom:1px solid #eef0f3;'
            f'font-size:12px;color:#888">{assessed_str}</td>'
            f'</tr>'
        )

    th = ('background:#1e2a3a;color:#fff;text-align:left;padding:11px 14px;'
          'font-size:12px;font-weight:600;letter-spacing:0.4px')
    return (
        f'<table style="width:100%;border-collapse:collapse;background:#fff;'
        f'border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.1)">'
        f'<thead><tr>'
        f'<th style="{th};width:28%">Backlog Item</th>'
        f'<th style="{th}">Clarity</th>'
        f'<th style="{th}">Refinement</th>'
        f'<th style="{th}">Outcome</th>'
        f'<th style="{th}">Notes</th>'
        f'<th style="{th}">Assessed</th>'
        f'</tr></thead>'
        f'<tbody>{rows}</tbody></table>'
    )


def _generate_summary_csv(items: list) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Title", "Clarity", "Refinement", "Outcome", "Notes", "Assessed"])
    for item in items:
        clarity_full  = item.get("clarity_gradient", "") or ""
        writer.writerow([
            item.get("title", ""),
            clarity_full.replace(" Clarity", ""),
            item.get("threshold_zone", "") or "",
            item.get("outcome", "") or "",
            item.get("outcome_notes", "") or "",
            _format_assessed_date(item.get("created_at", "")),
        ])
    return output.getvalue()


def _adf_to_text(node, depth: int = 0) -> str:
    """Recursively convert Atlassian Document Format to plain text."""
    if not node or not isinstance(node, dict):
        return ""
    ntype   = node.get("type", "")
    content = node.get("content", [])

    if ntype == "text":
        return node.get("text", "")
    if ntype == "hardBreak":
        return "\n"
    if ntype == "rule":
        return "\n---\n"
    if ntype in ("media", "mediaSingle", "mediaGroup", "embed", "inlineCard"):
        return ""
    if ntype == "mention":
        return node.get("attrs", {}).get("text", "")
    if ntype == "emoji":
        return node.get("attrs", {}).get("text", "")
    if ntype == "paragraph":
        inner = "".join(_adf_to_text(c) for c in content)
        return inner.strip() + "\n"
    if ntype == "heading":
        inner = "".join(_adf_to_text(c) for c in content)
        return inner.strip() + "\n"
    if ntype == "bulletList":
        lines = ["  " * depth + "- " + _adf_to_text(i, depth + 1).strip()
                 for i in content]
        return "\n".join(lines) + "\n"
    if ntype == "orderedList":
        lines = [f"{'  ' * depth}{n}. " + _adf_to_text(i, depth + 1).strip()
                 for n, i in enumerate(content, 1)]
        return "\n".join(lines) + "\n"
    if ntype == "listItem":
        parts = [_adf_to_text(c, depth) for c in content]
        return " ".join(p.strip() for p in parts if p.strip())
    if ntype == "codeBlock":
        inner = "".join(_adf_to_text(c) for c in content)
        return f"```\n{inner}\n```\n"
    if ntype == "blockquote":
        inner = "".join(_adf_to_text(c) for c in content)
        return "> " + inner.strip() + "\n"
    # doc and everything else — recurse into children
    parts = [_adf_to_text(c, depth) for c in content]
    return "\n".join(p for p in parts if p.strip())


def _count_checklist_gaps(gemini_output: str) -> tuple[int, int]:
    """Return (gap_count, uncertain_count) by scanning ✗ and ? lines in the checklist."""
    if not gemini_output:
        return 0, 0
    sections  = _parse_assessment(gemini_output)
    checklist = sections.get("checklist", "")
    gaps = uncertain = 0
    for line in checklist.split("\n"):
        s = line.strip()
        if s.startswith("✗"):
            gaps += 1
        elif s.startswith("?"):
            uncertain += 1
    return gaps, uncertain


def _split_checklist_groups(checklist_text: str) -> list:
    """Split checklist markdown into individual group strings by ### headings."""
    groups  = []
    current = []
    for line in checklist_text.split("\n"):
        if line.startswith("### ") and current:
            groups.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        groups.append("\n".join(current))
    return groups


def _format_assessed_date(iso_str: str) -> str:
    try:
        dt  = datetime_type.fromisoformat(iso_str.replace("Z", "+00:00"))
        return f"{dt.day} {dt.strftime('%b %Y')}"
    except Exception:
        return iso_str[:10] if iso_str else ""


# ── Dialogs ────────────────────────────────────────────────────────────────────
@st.dialog("Rename Team")
def _dialog_rename_team(team: dict):
    new_name = st.text_input("New name", value=team["name"])
    c1, c2 = st.columns(2)
    if c1.button("Save", use_container_width=True):
        if new_name.strip():
            update_team(team["id"], new_name.strip())
            if st.session_state.get("current_team_id") == team["id"]:
                st.session_state["current_team_name"] = new_name.strip()
            st.session_state["team_renamed_success"] = True
            st.rerun()
        else:
            st.warning("Name cannot be empty.")
    if c2.button("Cancel", use_container_width=True):
        st.rerun()


@st.dialog("Rename Session")
def _dialog_rename_session(session: dict):
    new_name = st.text_input("New name", value=session["name"])
    c1, c2 = st.columns(2)
    if c1.button("Save", use_container_width=True):
        if new_name.strip():
            update_refinement_session(session["id"], new_name.strip())
            if st.session_state.get("current_session_id") == session["id"]:
                st.session_state["current_session_name"] = new_name.strip()
            st.session_state["session_renamed"] = True
            st.rerun()
        else:
            st.warning("Name cannot be empty.")
    if c2.button("Cancel", use_container_width=True):
        st.rerun()


@st.dialog("Delete Team")
def _dialog_delete_team(team: dict):
    st.write(f"Delete **{team['name']}**? This will permanently remove the team and all its data.")
    c1, c2 = st.columns(2)
    if c1.button("Yes, delete", use_container_width=True):
        delete_team(team["id"])
        if st.session_state.get("current_team_id") == team["id"]:
            st.session_state.pop("current_team_id",      None)
            st.session_state.pop("current_team_name",    None)
            st.session_state.pop("current_session_id",   None)
            st.session_state.pop("current_session_name", None)
            st.session_state["page"] = "teams"
        st.session_state["team_deleted_success"] = True
        st.session_state["team_deleted_name"]    = f"Team '{team['name']}' deleted."
        st.rerun()
    if c2.button("Cancel", use_container_width=True):
        st.rerun()


@st.dialog("Delete Session")
def _dialog_delete_session(session: dict):
    st.write(f"Delete **{session['name']}**? This will permanently remove the session and all its assessed items.")
    c1, c2 = st.columns(2)
    if c1.button("Yes, delete", use_container_width=True):
        delete_refinement_session(session["id"])
        if st.session_state.get("current_session_id") == session["id"]:
            st.session_state.pop("current_session_id",   None)
            st.session_state.pop("current_session_name", None)
            st.session_state["page"] = "sessions"
        st.session_state["session_deleted"]      = True
        st.session_state["session_deleted_name"] = f"Session '{session['name']}' deleted."
        st.rerun()
    if c2.button("Cancel", use_container_width=True):
        st.rerun()


@st.dialog("Delete Item")
def _dialog_delete_item(item: dict):
    st.write(f"Delete **{item['title']}**? This will permanently remove the item and its assessment.")
    c1, c2 = st.columns(2)
    if c1.button("Yes, delete", use_container_width=True):
        delete_backlog_item(item["id"])
        st.session_state["item_deleted"]      = True
        st.session_state["item_deleted_name"] = f"'{item['title']}' deleted."
        st.rerun()
    if c2.button("Cancel", use_container_width=True):
        st.rerun()


# ── Pages ──────────────────────────────────────────────────────────────────────
def page_login():
    st.title("Backlog Refinement Advisor")
    st.write("AI-powered backlog item readiness assessment.")
    st.divider()

    tab_login, tab_signup = st.tabs(["Log In", "Sign Up"])

    with tab_login:
        with st.form("login_form"):
            email     = st.text_input("Email")
            password  = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Log In")
        if submitted:
            if not email or not password:
                st.warning("Please enter your email and password.")
            else:
                err = do_login(email, password)
                if err:
                    st.error(f"Login failed: {err}")
                else:
                    st.rerun()
        st.markdown("---")
        if st.button("Forgot your password?"):
            st.session_state["show_forgot"] = True
            st.rerun()
        if st.session_state.get("show_forgot"):
            with st.form("forgot_form"):
                reset_email = st.text_input("Enter your email")
                send = st.form_submit_button("Send Reset Email")
            if send:
                if reset_email:
                    try:
                        get_supabase().auth.reset_password_email(
                            reset_email,
                            {"redirect_to": f"{st.secrets['app_url']}?type=recovery"},
                        )
                        st.success("Reset email sent. Check your inbox.")
                    except Exception as e:
                        st.error(f"Failed to send reset email: {e}")
                else:
                    st.warning("Please enter your email.")

    with tab_signup:
        with st.form("signup_form"):
            new_email    = st.text_input("Email",            key="su_email")
            new_password = st.text_input("Password",         type="password", key="su_pass")
            confirm      = st.text_input("Confirm Password", type="password", key="su_confirm")
            submitted_su = st.form_submit_button("Sign Up")
        if submitted_su:
            if not new_email or not new_password:
                st.warning("Please fill in all fields.")
            elif new_password != confirm:
                st.error("Passwords do not match.")
            else:
                err, msg = do_signup(new_email, new_password)
                if err:
                    st.error(f"Sign up failed: {err}")
                else:
                    st.success(msg)


def page_teams():
    if st.session_state.pop("team_created_success", None):
        st.success(st.session_state.pop("team_created_name", "Team created."))
    if st.session_state.pop("team_deleted_success", None):
        st.success(st.session_state.pop("team_deleted_name", "Team deleted."))
    if st.session_state.pop("team_renamed_success", None):
        st.success("Team renamed.")

    teams = get_teams_with_counts()
    sid   = st.session_state.get("session_id", "")
    q     = f"sid={sid}&" if sid else ""

    # ── Handle pending dialogs (triggered via HTML card links) ───────────────
    if pending_rename := st.session_state.pop("pending_team_rename_id", None):
        team_obj = next((t for t in teams if str(t["id"]) == str(pending_rename)), None)
        if team_obj:
            _dialog_rename_team(team_obj)

    if pending_delete := st.session_state.pop("pending_team_delete_id", None):
        team_obj = next((t for t in teams if str(t["id"]) == str(pending_delete)), None)
        if team_obj:
            _dialog_delete_team(team_obj)

    # ── Page header ──────────────────────────────────────────────────────────
    st.markdown(
        '<h1 style="margin:0 0 4px 0;color:#1e2a3a;font-size:26px;font-weight:700">'
        'Your Teams</h1>'
        '<p style="margin:0 0 20px 0;color:#888;font-size:13px">'
        'Select a team to manage its refinement sessions</p>',
        unsafe_allow_html=True,
    )

    # ── Add Team form — Streamlit form only for new users with no teams ───────
    if not teams:
        with st.container(border=True):
            with st.form("add_team"):
                name = st.text_input("Team Name", placeholder="e.g. Platform Engineering")
                c1, c2 = st.columns([3, 1])
                submitted = c1.form_submit_button("Add Team", type="primary",
                                                  use_container_width=True)
                cancelled = c2.form_submit_button("Cancel", use_container_width=True)
            if submitted:
                if name.strip():
                    create_team(name.strip())
                    st.session_state["team_created_success"] = True
                    st.session_state["team_created_name"]    = f"Team '{name.strip()}' created."
                    st.rerun()
                else:
                    st.warning("Please enter a team name.")
            if cancelled:
                st.rerun()
        return

    # ── Team card grid — single CSS grid block for equal-height cards ────────
    grid_html = (
        '<div style="display:grid;grid-template-columns:repeat(3,1fr);'
        'gap:16px;align-items:stretch">'
    )
    for team in teams:
        count       = team.get("session_count", 0)
        tid         = _html.escape(str(team["id"]))
        tname       = _html.escape(team["name"])
        count_label = f'{count} session{"s" if count != 1 else ""}'
        grid_html += (
            f'<div style="background:#fff;border:1px solid #e0e3e8;border-radius:10px;'
            f'box-shadow:0 1px 4px rgba(0,0,0,0.07);padding:20px;display:flex;'
            f'flex-direction:column">'
            f'<div style="font-size:16px;font-weight:700;color:#1e2a3a;margin-bottom:4px">'
            f'{tname}</div>'
            f'<div style="font-size:12px;color:#aaa;margin-bottom:16px">{count_label}</div>'
            f'<a href="?{q}_team={tid}" target="_self"'
            f' style="display:block;text-align:center;background:#1565C0;color:#fff;'
            f'text-decoration:none;padding:9px 0;border-radius:6px;'
            f'font-size:13px;font-weight:600;margin-bottom:10px">Open</a>'
            f'<div style="display:flex;gap:8px">'
            f'<a href="?{q}_team_action=rename_team&tid={tid}" target="_self"'
            f' style="flex:1;text-align:center;background:#fff;color:#1e2a3a;'
            f'text-decoration:none;padding:8px 0;border-radius:6px;'
            f'font-size:13px;font-weight:600;border:1px solid #d0d4db">Rename</a>'
            f'<a href="?{q}_team_action=delete_team&tid={tid}" target="_self"'
            f' style="flex:1;text-align:center;background:#fff;color:#c62828;'
            f'text-decoration:none;padding:8px 0;border-radius:6px;'
            f'font-size:13px;font-weight:600;border:1px solid #ef9a9a">Delete</a>'
            f'</div>'
            f'</div>'
        )
    # Last grid slot: inline form card or dashed card
    if st.session_state.get("show_add_team"):
        grid_html += (
            f'<div style="background:#fff;border:1px solid #e0e3e8;border-radius:10px;'
            f'box-shadow:0 1px 4px rgba(0,0,0,0.07);padding:20px;'
            f'display:flex;flex-direction:column;justify-content:center">'
            f'<div style="font-size:13px;font-weight:600;color:#1e2a3a;margin-bottom:12px">'
            f'New Team</div>'
            f'<form method="get" action="" style="margin:0;padding:0">'
            f'<input type="hidden" name="sid" value="{sid}">'
            f'<input type="hidden" name="_team_action" value="submit_add_team">'
            f'<input type="text" name="team_name" required maxlength="80"'
            f' placeholder="e.g. Platform Engineering" autofocus'
            f' style="width:100%;padding:8px 10px;border:1px solid #d0d4db;'
            f'border-radius:6px;font-size:13px;color:#1e2a3a;'
            f'box-sizing:border-box;margin-bottom:10px;outline:none;'
            f'font-family:inherit">'
            f'<button type="submit"'
            f' style="display:block;width:100%;background:#1565C0;color:#fff;'
            f'border:none;padding:9px 0;border-radius:6px;font-size:13px;'
            f'font-weight:600;cursor:pointer;margin-bottom:8px;font-family:inherit">'
            f'Add Team</button>'
            f'<a href="?{q}_team_action=cancel_add_team" target="_self"'
            f' style="display:block;text-align:center;color:#888;'
            f'font-size:12px;text-decoration:none">Cancel</a>'
            f'</form>'
            f'</div>'
        )
    else:
        grid_html += (
            f'<a href="?{q}_team_action=add_team" target="_self"'
            f' style="display:flex;align-items:center;justify-content:center;'
            f'background:#f8f9fb;border:2px dashed #d0d4db;border-radius:10px;'
            f'text-decoration:none;color:#1565C0;font-size:14px;font-weight:600;'
            f'min-height:130px">'
            f'+ Add New Team</a>'
        )
    grid_html += '</div>'
    st.markdown(grid_html, unsafe_allow_html=True)


def page_sessions():
    team_name = st.session_state.get("current_team_name", "Team")

    if st.session_state.pop("session_created", None):
        st.success(st.session_state.pop("session_created_name", "Session created."))
    if st.session_state.pop("session_deleted", None):
        st.success(st.session_state.pop("session_deleted_name", "Session deleted."))
    if st.session_state.pop("session_renamed", None):
        st.success("Session renamed.")

    team_id  = st.session_state["current_team_id"]
    sessions = get_refinement_sessions_with_counts(team_id)

    today        = date_type.today()
    default_name = f"Session — {today.strftime('%b')} {today.day} {today.year}"
    sid          = st.session_state.get("session_id", "")
    q            = f"sid={sid}&" if sid else ""

    # ── Handle pending dialogs ────────────────────────────────────────────────
    if pending_rename := st.session_state.pop("pending_session_rename_id", None):
        sess_obj = next((s for s in sessions if str(s["id"]) == str(pending_rename)), None)
        if sess_obj:
            _dialog_rename_session(sess_obj)

    if pending_delete := st.session_state.pop("pending_session_delete_id", None):
        sess_obj = next((s for s in sessions if str(s["id"]) == str(pending_delete)), None)
        if sess_obj:
            _dialog_delete_session(sess_obj)

    # ── Row hover CSS ─────────────────────────────────────────────────────────
    st.markdown("""
<style>
.sess-row:hover { background: #f8f9fb !important; }
</style>
""", unsafe_allow_html=True)

    # ── Page header ──────────────────────────────────────────────────────────
    hcol, bcol = st.columns([7, 2])
    hcol.markdown(
        f'<h1 style="margin:0 0 4px 0;color:#1e2a3a;font-size:26px;font-weight:700">'
        f'Refinement Sessions</h1>'
        f'<p style="margin:0 0 20px 0;color:#888;font-size:13px">'
        f'{_html.escape(team_name)}</p>',
        unsafe_allow_html=True,
    )
    bcol.write("")
    if bcol.button("+ New Session", use_container_width=True, type="primary",
                   key="btn_show_add_sess"):
        st.session_state["show_add_session"] = not st.session_state.get("show_add_session", False)
        st.rerun()

    # ── Add Session form ──────────────────────────────────────────────────────
    if st.session_state.get("show_add_session") or not sessions:
        with st.container(border=True):
            with st.form("add_session"):
                name = st.text_input("Session Name", value=default_name)
                c1, c2 = st.columns([3, 1])
                submitted = c1.form_submit_button("Add Session", type="primary",
                                                  use_container_width=True)
                cancelled = c2.form_submit_button("Cancel", use_container_width=True)
            if submitted:
                if name.strip():
                    create_refinement_session(team_id, name.strip())
                    st.session_state["session_created"]      = True
                    st.session_state["session_created_name"] = f"Session '{name.strip()}' created."
                    st.session_state.pop("show_add_session", None)
                    st.rerun()
                else:
                    st.warning("Please enter a session name.")
            if cancelled:
                st.session_state.pop("show_add_session", None)
                st.rerun()

    if not sessions:
        st.markdown(
            '<p style="color:#aaa;margin-top:8px">No sessions yet. '
            'Add your first session above.</p>',
            unsafe_allow_html=True,
        )
        return

    # ── Sessions table — pure HTML for reliable styling ───────────────────────
    tbl = (
        '<div style="background:#fff;border-radius:10px;'
        'box-shadow:0 1px 4px rgba(0,0,0,0.08);overflow:hidden">'
        '<div style="background:#1e2a3a;color:#fff;padding:10px 16px;'
        'font-size:11px;font-weight:700;letter-spacing:0.5px;text-transform:uppercase;'
        'display:grid;grid-template-columns:4fr 2fr 1.2fr 2fr 1.5fr 1.5fr 1.5fr;gap:8px">'
        '<div>Session</div><div>Status</div><div>Items</div><div>Created</div>'
        '<div></div><div></div><div></div>'
        '</div>'
    )

    btn_base  = ('text-decoration:none;display:inline-block;border-radius:5px;'
                 'padding:5px 10px;font-size:12px;font-weight:600;white-space:nowrap')
    btn_open  = f'{btn_base};background:#1565C0;color:#fff;border:1px solid #1565C0'
    btn_sec   = f'{btn_base};background:#fff;color:#1e2a3a;border:1px solid #d0d4db'
    btn_del   = f'{btn_base};background:#fff;color:#c62828;border:1px solid #ef9a9a'

    for i, session in enumerate(sessions):
        status      = session.get("status", "preparing")
        total       = session.get("item_total",  0)
        tagged      = session.get("item_tagged", 0)
        created_str = _format_assessed_date(session.get("created_at", ""))
        sid_s       = _html.escape(str(session["id"]))
        sname       = _html.escape(session["name"])
        border      = "" if i == len(sessions) - 1 else "border-bottom:1px solid #eef0f3"

        open_href   = f"?{q}_sess_action=open_session&sess_id={sid_s}&sess_status={status}"
        rename_href = f"?{q}_sess_action=rename_session&sess_id={sid_s}"
        delete_href = f"?{q}_sess_action=delete_session&sess_id={sid_s}"

        tbl += (
            f'<div class="sess-row" style="display:grid;'
            f'grid-template-columns:4fr 2fr 1.2fr 2fr 1.5fr 1.5fr 1.5fr;'
            f'padding:13px 16px;{border};align-items:center;font-size:13px">'
            f'<div style="font-weight:700;color:#1e2a3a">{sname}</div>'
            f'<div>{_status_badge(status)}</div>'
            f'<div style="color:#555">{tagged}/{total}</div>'
            f'<div style="color:#aaa;font-size:12px">{created_str}</div>'
            f'<div><a href="{open_href}" target="_self" style="{btn_open}">Open</a></div>'
            f'<div><a href="{rename_href}" target="_self" style="{btn_sec}">Rename</a></div>'
            f'<div><a href="{delete_href}" target="_self" style="{btn_del}">Delete</a></div>'
            f'</div>'
        )

    tbl += '</div>'
    st.markdown(tbl, unsafe_allow_html=True)


def page_prepare():
    session_name = st.session_state.get("current_session_name", "Session")
    team_name    = st.session_state.get("current_team_name", "Team")
    session_id   = st.session_state["current_session_id"]

    items = get_backlog_items(session_id)

    # ── Handle pending item delete dialog ────────────────────────────────────
    if pending_del_id := st.session_state.pop("pending_item_delete_id", None):
        item_to_del = next((i for i in items if str(i["id"]) == str(pending_del_id)), None)
        if item_to_del:
            _dialog_delete_item(item_to_del)

    if st.session_state.pop("item_submitted", None):
        st.success("Item assessed and added.")
    if st.session_state.pop("item_deleted", None):
        st.success(st.session_state.pop("item_deleted_name", "Item deleted."))
    jira_done = st.session_state.pop("jira_import_done", 0)
    if jira_done:
        st.success(f"{jira_done} item(s) imported from Jira and assessed.")
    for err in st.session_state.pop("jira_import_errors", []):
        st.error(f"Assessment failed: {err}")

    # ── Header row ────────────────────────────────────────────────────────────
    sid_param      = st.session_state.get("session_id", "")
    q              = f"sid={sid_param}&" if sid_param else ""
    start_disabled = len(items) == 0
    start_href     = f"?{q}_sess_action=start_session&sess_id={_html.escape(str(session_id))}"

    col_hdr, col_start = st.columns([7, 2])
    col_hdr.markdown(
        f'<h1 style="margin:0 0 4px 0;color:#1e2a3a;font-size:26px;font-weight:700">'
        f'{_html.escape(session_name)}</h1>'
        f'<p style="margin:0 0 20px 0;color:#888;font-size:13px">'
        f'Team: {_html.escape(team_name)}&nbsp;|&nbsp;Status: Preparing</p>',
        unsafe_allow_html=True,
    )
    if start_disabled:
        col_start.markdown(
            '<div style="text-align:right">'
            '<span style="display:inline-block;background:#2e7d32;color:#fff;opacity:0.5;'
            'font-size:14px;font-weight:600;padding:10px 22px;border-radius:6px;'
            'white-space:nowrap;cursor:not-allowed">Start Session →</span>'
            '<div style="font-size:12px;color:#aaa;margin-top:6px">'
            'Requires at least one assessed item.</div>'
            '</div>',
            unsafe_allow_html=True,
        )
    else:
        col_start.markdown(
            f'<div style="text-align:right">'
            f'<a href="{start_href}" target="_self" style="display:inline-block;'
            f'background:#2e7d32;color:#fff;text-decoration:none;font-size:14px;'
            f'font-weight:600;padding:10px 22px;border-radius:6px;white-space:nowrap">'
            f'Start Session →</a></div>',
            unsafe_allow_html=True,
        )

    # ── Apply pending select-all flags before any widgets render ─────────────
    if st.session_state.pop("jira_select_all_flag", False):
        for iss in st.session_state.get("jira_issues", []):
            st.session_state[f"jira_cb_{iss['key']}"] = True
    if st.session_state.pop("jira_deselect_all_flag", False):
        for iss in st.session_state.get("jira_issues", []):
            st.session_state[f"jira_cb_{iss['key']}"] = False

    # ── Toolbar ───────────────────────────────────────────────────────────────
    show_add  = st.session_state.get("show_add_item",  False)
    show_jira = st.session_state.get("show_jira_panel", False)

    n_items     = len(items)
    count_label = f'{n_items} item{"s" if n_items != 1 else ""}'

    if not show_add and not show_jira:
        _btn_base = (
            'text-decoration:none;display:inline-block;border-radius:6px;'
            'padding:8px 16px;font-size:13px;font-weight:600;white-space:nowrap'
        )
        _btn_sec  = f'{_btn_base};background:#fff;color:#1e2a3a;border:1px solid #d0d4db'
        add_href  = f'?{q}_prep_action=toggle_add'
        jira_href = f'?{q}_prep_action=toggle_jira'

        st.markdown(
            f'<div style="display:flex;align-items:center;justify-content:space-between;'
            f'margin-bottom:12px">'
            f'<div style="font-size:12px;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:0.5px;color:#999">'
            f'Assessed Items&nbsp;·&nbsp;{count_label}</div>'
            f'<div style="display:flex;gap:8px">'
            f'<a href="{add_href}" target="_self" style="{_btn_sec}">+ Add Item</a>'
            f'<a href="{jira_href}" target="_self" style="{_btn_sec}">Import from Jira</a>'
            f'</div></div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f'<div style="font-size:12px;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:0.5px;color:#999;margin-bottom:12px">'
            f'Assessed Items&nbsp;·&nbsp;{count_label}</div>',
            unsafe_allow_html=True,
        )

    # ── Add Item panel ────────────────────────────────────────────────────────
    if st.session_state.get("show_add_item"):
        with st.container(border=True):
            st.subheader("Add Backlog Item")
            with st.form("add_item_form"):
                title               = st.text_input("Title *")
                description         = st.text_area("Description", height=100,
                                                   placeholder="What needs to be done and why")
                acceptance_criteria = st.text_area("Acceptance Criteria", height=100,
                                                   placeholder="What must be true for this item to be considered complete")
                dependencies        = st.text_input("Dependencies",
                                                   placeholder="Other items or systems this relies on")
                assumptions         = st.text_input("Assumptions",
                                                   placeholder="What the team is taking as given")
                notes               = st.text_area("Notes", height=80,
                                                  placeholder="Anything else relevant")
                btn_col1, btn_col2 = st.columns([3, 1])
                submitted = btn_col1.form_submit_button("Run Assessment", type="primary",
                                                        use_container_width=True)
                cancelled = btn_col2.form_submit_button("Cancel", use_container_width=True)

            if cancelled:
                st.session_state["show_add_item"] = False
                st.rerun()

            if submitted:
                if not title.strip():
                    st.warning("Title is required.")
                else:
                    with st.spinner("Evaluating with Claude..."):
                        try:
                            clarity, zone, output = run_claude_evaluation(
                                title.strip(), description, acceptance_criteria,
                                dependencies, assumptions, notes,
                            )
                            create_backlog_item(
                                session_id, title.strip(), description,
                                acceptance_criteria, dependencies, assumptions,
                                notes, clarity, zone, output,
                            )
                            st.session_state["item_submitted"] = True
                            st.session_state["show_add_item"]  = False
                            st.rerun()
                        except Exception as e:
                            st.error(f"Assessment failed: {e}")

    # ── Jira import panel ─────────────────────────────────────────────────────
    if st.session_state.get("show_jira_panel"):
        with st.container(border=True):
            st.subheader("Import from Jira")
            jira_issues = st.session_state.get("jira_issues")

            if jira_issues is None:
                # ── Step 1: JQL query ─────────────────────────────────────
                jql = st.text_input(
                    "JQL Query",
                    value='project = "" AND issuetype = Story ORDER BY created DESC',
                    key="jira_jql_input",
                    help='Replace the project key and adjust the query as needed. Example: project = PAY AND issuetype = Story AND sprint = "Sprint 24"',
                )
                if st.button("Fetch Issues", key="btn_jira_fetch", type="primary"):
                    if not jql.strip():
                        st.warning("Enter a JQL query.")
                    else:
                        with st.spinner("Fetching from Jira..."):
                            try:
                                resp = httpx.get(
                                    f"{st.secrets['jira_url']}/rest/api/3/search/jql",
                                    auth=(st.secrets["jira_email"], st.secrets["jira_api_token"]),
                                    params={
                                        "jql":        jql.strip(),
                                        "maxResults": 50,
                                        "fields":     "summary,description,issuetype",
                                    },
                                    timeout=15,
                                )
                                if resp.status_code == 200:
                                    issues = resp.json().get("issues", [])
                                    if issues:
                                        st.session_state["jira_issues"] = issues
                                        st.rerun()
                                    else:
                                        st.info("No issues found for that query.")
                                else:
                                    st.error(f"Jira query failed — HTTP {resp.status_code}: {resp.text[:300]}")
                            except Exception as e:
                                st.error(f"Error fetching from Jira: {e}")

            else:
                # ── Step 2: Select issues ─────────────────────────────────
                st.caption(f"{len(jira_issues)} issues found. Select the ones to import.")

                sel_col1, sel_col2, _ = st.columns([2, 2, 6])
                if sel_col1.button("Select All", key="btn_jira_sel_all"):
                    st.session_state["jira_select_all_flag"] = True
                    st.rerun()
                if sel_col2.button("Deselect All", key="btn_jira_desel_all"):
                    st.session_state["jira_deselect_all_flag"] = True
                    st.rerun()

                st.write("")
                for issue in jira_issues:
                    ikey    = issue["key"]
                    fields  = issue.get("fields", {})
                    summary = fields.get("summary", "(no summary)")
                    itype   = fields.get("issuetype", {}).get("name", "")
                    cb_col, info_col = st.columns([1, 11])
                    cb_col.checkbox("", key=f"jira_cb_{ikey}")
                    info_col.markdown(f"**{ikey}** — {summary}  `{itype}`")

                selected_keys = [
                    i["key"] for i in jira_issues
                    if st.session_state.get(f"jira_cb_{i['key']}", False)
                ]
                n_sel = len(selected_keys)

                st.write("")
                imp_col, back_col, cancel_col, _ = st.columns([3, 2, 2, 3])

                if imp_col.button(
                    f"Import & Assess Selected ({n_sel})",
                    type="primary",
                    disabled=(n_sel == 0),
                    key="btn_jira_import",
                    use_container_width=True,
                ):
                    selected = [i for i in jira_issues if i["key"] in selected_keys]
                    progress_bar = st.progress(0)
                    status_text  = st.empty()
                    errors       = []

                    for n, issue in enumerate(selected):
                        fields   = issue.get("fields", {})
                        summary  = fields.get("summary", "")
                        desc_adf = fields.get("description")
                        desc     = _adf_to_text(desc_adf) if desc_adf else ""

                        status_text.text(f"Assessing {n + 1} of {len(selected)}: {summary[:60]}...")
                        progress_bar.progress((n + 1) / len(selected))

                        try:
                            clarity, zone, output = run_claude_evaluation(
                                summary, desc, "", "", "", "",
                            )
                            create_backlog_item(
                                session_id, summary, desc,
                                "", "", "", "", clarity, zone, output,
                            )
                        except Exception as e:
                            errors.append(f"{issue['key']}: {e}")

                    st.session_state.pop("jira_issues", None)
                    st.session_state["show_jira_panel"]  = False
                    st.session_state["jira_import_done"] = len(selected) - len(errors)
                    if errors:
                        st.session_state["jira_import_errors"] = errors
                    st.rerun()

                if back_col.button("← Back", key="btn_jira_back", use_container_width=True):
                    st.session_state.pop("jira_issues", None)
                    st.rerun()

                if cancel_col.button("Cancel", key="btn_jira_cancel", use_container_width=True):
                    st.session_state.pop("jira_issues", None)
                    st.session_state["show_jira_panel"] = False
                    st.rerun()

    # ── Items table — pure HTML ───────────────────────────────────────────────
    if not items:
        st.markdown(
            '<p style="color:#aaa;margin-top:16px">No items yet. '
            'Use "+ Add Item" or "Import from Jira" to add and assess backlog items.</p>',
            unsafe_allow_html=True,
        )
        return

    st.markdown(
        '<style>.item-row:hover { background:#f8f9fb !important; }</style>',
        unsafe_allow_html=True,
    )

    btn_del = (
        'text-decoration:none;display:inline-block;border-radius:5px;'
        'padding:5px 10px;font-size:12px;font-weight:600;white-space:nowrap;'
        'background:#fff;color:#c62828;border:1px solid #ef9a9a'
    )

    tbl = (
        '<div style="background:#fff;border-radius:10px;'
        'box-shadow:0 1px 4px rgba(0,0,0,0.08);overflow:hidden;margin-top:8px">'
        '<div style="background:#1e2a3a;color:#fff;padding:10px 16px;'
        'font-size:11px;font-weight:700;letter-spacing:0.5px;text-transform:uppercase;'
        'display:grid;grid-template-columns:5fr 2fr 2fr 1fr 2fr 2fr 1.5fr;gap:8px">'
        '<div>Item</div><div>Clarity</div><div>Refinement</div>'
        '<div>Gaps</div><div>Uncertain</div><div>Assessed</div><div></div>'
        '</div>'
    )

    for i, item in enumerate(items):
        clarity_full    = item.get("clarity_gradient", "")
        zone            = item.get("threshold_zone", "")
        clarity_short   = clarity_full.replace(" Clarity", "")
        assessed_str    = _format_assessed_date(item.get("created_at", ""))
        gaps, uncertain = _count_checklist_gaps(item.get("gemini_output", ""))
        item_id_s       = _html.escape(str(item["id"]))
        title_s         = _html.escape(item["title"])
        border          = "" if i == len(items) - 1 else "border-bottom:1px solid #eef0f3"
        delete_href     = f"?{q}_item_action=delete_item&item_id={item_id_s}"

        tbl += (
            f'<div class="item-row" style="display:grid;'
            f'grid-template-columns:5fr 2fr 2fr 1fr 2fr 2fr 1.5fr;'
            f'padding:12px 16px;{border};align-items:center;font-size:13px">'
            f'<div style="font-weight:700;color:#1e2a3a">{title_s}</div>'
            f'<div>{_clarity_badge(clarity_short)}</div>'
            f'<div>{_zone_badge(zone)}</div>'
            f'<div>{_gaps_badge_html(gaps, "gap")}</div>'
            f'<div>{_gaps_badge_html(uncertain, "uncertain")}</div>'
            f'<div style="color:#aaa;font-size:12px">{assessed_str}</div>'
            f'<div><a href="{delete_href}" target="_self" style="{btn_del}">Delete</a></div>'
            f'</div>'
        )

    tbl += '</div>'
    st.markdown(tbl, unsafe_allow_html=True)



def page_run_session():
    session_name = st.session_state.get("current_session_name", "Session")
    team_name    = st.session_state.get("current_team_name", "Team")
    session_id   = st.session_state["current_session_id"]

    session      = get_session(session_id)
    raw_status   = (session.get("status", "in_progress") if session else "in_progress")
    status_labels = {"in_progress": "In Progress", "complete": "Complete"}
    status_label  = status_labels.get(raw_status, raw_status.replace("_", " ").title())

    # Oldest-first so items are reviewed in the order they were added
    all_items = list(reversed(get_backlog_items(session_id)))

    if not all_items:
        st.warning("No items in this session.")
        if st.button("Back to Prepare"):
            st.session_state["page"] = "prepare"
            st.rerun()
        return

    total = len(all_items)
    idx   = st.session_state.get("run_item_index", 0)
    idx   = max(0, min(idx, total - 1))
    item  = all_items[idx]

    if st.session_state.pop("outcome_saved", None):
        st.success("Outcome saved.")

    # ── Page header ───────────────────────────────────────────────────────────
    sid = st.session_state.get("session_id", "")
    q   = f"sid={sid}&" if sid else ""

    outcome_color_map = {label: color for label, color in OUTCOME_OPTIONS}
    dots_html = ""
    for i, it in enumerate(all_items):
        color = outcome_color_map.get(it.get("outcome", ""), "#bdc3c7")
        if i == idx:
            dot_style = (
                f"width:14px;height:14px;border-radius:50%;background:#fff;"
                f"border:3px solid #2c3e50;box-shadow:0 0 0 1px #2c3e50;"
                f"display:inline-block"
            )
        else:
            dot_style = (
                f"width:14px;height:14px;border-radius:50%;background:{color};"
                f"border:3px solid {color};display:inline-block"
            )
        dots_html += f'<span style="{dot_style};margin-right:6px"></span>'

    hcol, bcol = st.columns([7, 2])
    hcol.markdown(
        f'<h1 style="margin:0 0 4px 0;color:#1e2a3a;font-size:26px;font-weight:700">'
        f'{_html.escape(session_name)}</h1>'
        f'<p style="margin:0 0 10px 0;color:#888;font-size:13px">'
        f'Team: {_html.escape(team_name)}&nbsp;|&nbsp;Item {idx + 1} of {total}</p>'
        f'<div style="display:flex;gap:0;align-items:center">{dots_html}</div>',
        unsafe_allow_html=True,
    )
    bcol.markdown(
        f'<div style="text-align:right;padding-top:6px">'
        f'<a href="?{q}_nav=view_summary" target="_self"'
        f' style="display:inline-block;background:#1565C0;color:#fff;'
        f'text-decoration:none;font-size:13px;font-weight:600;'
        f'padding:9px 20px;border-radius:6px">View Summary</a></div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<hr style="border:none;border-top:1px solid #dde1e7;margin:16px 0">',
        unsafe_allow_html=True,
    )

    # ── Item title ────────────────────────────────────────────────────────────
    st.markdown(
        f'<div style="font-size:20px;font-weight:700;color:#1e2a3a;margin:0 0 16px 0">'
        f'{_html.escape(item["title"])}</div>',
        unsafe_allow_html=True,
    )

    # ── Rating cards + overall callout ────────────────────────────────────────
    clarity_short = item.get("clarity_gradient", "").replace(" Clarity", "")
    zone          = item.get("threshold_zone", "")
    raw_output    = item.get("gemini_output", "") or ""
    sections      = _parse_assessment(raw_output)

    st.markdown(
        _render_rating_cards_html(
            clarity_short, zone,
            sections.get("clarity_reasoning", ""),
            sections.get("refinement_reasoning", ""),
        ),
        unsafe_allow_html=True,
    )

    if sections.get("overall"):
        st.markdown(_render_overall_callout_html(sections["overall"]), unsafe_allow_html=True)

    mistakes_html = _render_mistakes_callout_html(sections.get("common_mistakes", ""))
    if mistakes_html:
        st.markdown(mistakes_html, unsafe_allow_html=True)

    # ── Checklist — collapsible expander ─────────────────────────────────────
    if sections.get("checklist"):
        st.markdown(
            '<hr style="border:none;border-top:1px solid #dde1e7;margin:16px 0">',
            unsafe_allow_html=True,
        )
        with st.expander("View full checklist detail"):
            groups = _split_checklist_groups(sections["checklist"])
            if len(groups) >= 2:
                left_col, right_col = st.columns(2)
                with left_col:
                    for g in groups[:2]:
                        st.markdown(g)
                with right_col:
                    for g in groups[2:]:
                        st.markdown(g)
            else:
                st.markdown(sections["checklist"])

    st.markdown(
        '<hr style="border:none;border-top:1px solid #dde1e7;margin:16px 0">',
        unsafe_allow_html=True,
    )

    # ── Outcome panel ─────────────────────────────────────────────────────────
    current_outcome = item.get("outcome") or ""
    current_notes   = item.get("outcome_notes") or ""
    item_id_s       = _html.escape(str(item["id"]))

    st.markdown(
        '<div style="font-size:12px;font-weight:700;text-transform:uppercase;'
        'letter-spacing:0.5px;color:#666;margin-bottom:10px">Team Decision</div>',
        unsafe_allow_html=True,
    )

    btns_html = '<div style="display:flex;gap:8px;flex-wrap:nowrap;margin-bottom:12px">'
    for label, color in OUTCOME_OPTIONS:
        selected = (current_outcome == label)
        href     = (f"?{q}_run_action=set_outcome"
                    f"&item_id={item_id_s}"
                    f"&outcome={_url_quote(label, safe='')}")
        if selected:
            style = (f"text-decoration:none;display:inline-block;flex:1;text-align:center;"
                     f"border:2px solid {color};border-radius:20px;padding:7px 14px;"
                     f"font-size:12px;font-weight:600;white-space:nowrap;"
                     f"background:{color};color:#fff")
        else:
            style = (f"text-decoration:none;display:inline-block;flex:1;text-align:center;"
                     f"border:2px solid {color};border-radius:20px;padding:7px 14px;"
                     f"font-size:12px;font-weight:600;white-space:nowrap;"
                     f"background:#fff;color:{color}")
        btns_html += f'<a href="{href}" target="_self" style="{style}">{label}</a>'
    btns_html += '</div>'
    st.markdown(btns_html, unsafe_allow_html=True)

    # ── Notes + navigation on one row ─────────────────────────────────────────
    note_col, prev_col, ctr_col, next_col = st.columns([7, 1.2, 1.2, 1.5])

    note_col.text_input(
        "Note",
        value=current_notes,
        key=f"notes_{item['id']}",
        label_visibility="collapsed",
        placeholder="Note: optional facilitator note",
    )
    if prev_col.button("← Prev", disabled=(idx == 0), use_container_width=True):
        st.session_state["run_item_index"] = idx - 1
        st.rerun()
    ctr_col.markdown(
        f'<div style="text-align:center;padding-top:8px;font-size:13px;color:#aaa">'
        f'{idx + 1} of {total}</div>',
        unsafe_allow_html=True,
    )
    if next_col.button("Next →", disabled=(idx == total - 1),
                       use_container_width=True, type="primary"):
        st.session_state["run_item_index"] = idx + 1
        st.rerun()


def page_summary():
    session_name = st.session_state.get("current_session_name", "Session")
    team_name    = st.session_state.get("current_team_name", "Team")
    session_id   = st.session_state["current_session_id"]

    items = list(reversed(get_backlog_items(session_id)))

    # Auto-mark as complete when summary is viewed
    session = get_session(session_id)
    if session and session.get("status") != "complete":
        update_session_status(session_id, "complete")

    # ── Header ────────────────────────────────────────────────────────────────
    n = len(items)
    st.markdown(
        f'<h2 style="margin:0 0 4px 0;color:#1e2a3a">Session Summary</h2>'
        f'<p style="margin:0;color:#888;font-size:13px">'
        f'{_html.escape(session_name)}&nbsp;·&nbsp;{_html.escape(team_name)}'
        f'&nbsp;·&nbsp;{n} item{"s" if n != 1 else ""}'
        f'&nbsp;·&nbsp;<span style="background:#e8f5e9;color:#2e7d32;padding:1px 8px;'
        f'border-radius:10px;font-size:11px;font-weight:600;border:1px solid #a5d6a7">'
        f'Complete</span></p>',
        unsafe_allow_html=True,
    )

    st.write("")

    # ── Outcome count bar ─────────────────────────────────────────────────────
    st.markdown(_render_outcome_count_bar_html(items), unsafe_allow_html=True)

    st.divider()

    # ── Summary table ─────────────────────────────────────────────────────────
    st.markdown(_render_summary_table_html(items), unsafe_allow_html=True)

    # ── Action buttons ────────────────────────────────────────────────────────
    st.write("")
    csv_data = _generate_summary_csv(items)
    btn1, btn2, _ = st.columns([2, 2, 6])
    btn1.download_button(
        "Export CSV",
        data=csv_data,
        file_name=f"{session_name} Summary.csv",
        mime="text/csv",
        use_container_width=True,
    )
    if btn2.button("Reopen Session", use_container_width=True):
        update_session_status(session_id, "in_progress")
        st.session_state["page"] = "run_session"
        st.rerun()



# ── Top navigation ─────────────────────────────────────────────────────────────
def _build_team_options_html(q: str, current_team_id: str) -> str:
    """Return an HTML <select> (or plain text) for the team breadcrumb slot."""
    try:
        teams = get_teams()
    except Exception:
        teams = []
    if not teams:
        name = _html.escape(st.session_state.get("current_team_name", "Team"))
        return f'<span style="color:#90CAF9;font-size:13px;font-weight:600">{name}</span>'
    opts = ""
    for t in teams:
        sel   = "selected" if t["id"] == current_team_id else ""
        label = _html.escape(t["name"])
        opts += f'<option value="{t["id"]}" {sel}>{label}</option>'
    return (
        f'<select class="tn-select" '
        f'onchange="window.location=\'?{q}_team=\'+encodeURIComponent(this.value)">'
        f'{opts}</select>'
    )


def show_topnav():
    """Render the fixed top navigation bar (replaces sidebar)."""
    page      = st.session_state.get("page", "teams")
    email     = _html.escape(st.session_state.get("user_email", ""))
    team_id   = st.session_state.get("current_team_id", "")
    sess_name = st.session_state.get("current_session_name", "")
    sid       = st.session_state.get("session_id", "")

    # ── Handle team action params (card links → dialogs / forms) ────────────
    team_action   = st.query_params.get("_team_action", "")
    tid           = st.query_params.get("tid", "")
    team_name_val = st.query_params.get("team_name", "").strip()
    if team_action in ("add_team", "submit_add_team", "cancel_add_team",
                       "rename_team", "delete_team"):
        for k in ("_team_action", "tid", "team_name"):
            try:
                del st.query_params[k]
            except Exception:
                pass
        if team_action == "add_team":
            st.session_state["show_add_team"] = True
        elif team_action == "submit_add_team":
            if team_name_val:
                create_team(team_name_val)
                st.session_state["team_created_success"] = True
                st.session_state["team_created_name"]    = f"Team '{team_name_val}' created."
            st.session_state.pop("show_add_team", None)
        elif team_action == "cancel_add_team":
            st.session_state.pop("show_add_team", None)
        elif team_action == "rename_team":
            st.session_state["pending_team_rename_id"] = tid
        elif team_action == "delete_team":
            st.session_state["pending_team_delete_id"] = tid
        st.session_state["page"] = "teams"
        st.rerun()
        return

    # ── Handle session action params ──────────────────────────────────────────
    sess_action  = st.query_params.get("_sess_action", "")
    sess_id_val  = st.query_params.get("sess_id", "")
    sess_status_val = st.query_params.get("sess_status", "")
    if sess_action in ("open_session", "rename_session", "delete_session", "start_session"):
        for k in ("_sess_action", "sess_id", "sess_status"):
            try:
                del st.query_params[k]
            except Exception:
                pass
        if sess_action == "open_session" and sess_id_val:
            session_obj = get_session(sess_id_val)
            if session_obj:
                st.session_state["current_session_id"]   = sess_id_val
                st.session_state["current_session_name"] = session_obj["name"]
                st.session_state["run_item_index"]        = 0
                if sess_status_val == "preparing":
                    st.session_state["page"] = "prepare"
                elif sess_status_val == "complete":
                    st.session_state["page"] = "summary"
                else:
                    st.session_state["page"] = "run_session"
        elif sess_action == "rename_session":
            st.session_state["pending_session_rename_id"] = sess_id_val
            st.session_state["page"] = "sessions"
        elif sess_action == "delete_session":
            st.session_state["pending_session_delete_id"] = sess_id_val
            st.session_state["page"] = "sessions"
        elif sess_action == "start_session" and sess_id_val:
            update_session_status(sess_id_val, "in_progress")
            st.session_state["run_item_index"] = 0
            st.session_state["page"] = "run_session"
        st.rerun()
        return

    # ── Handle item actions (delete_item) ────────────────────────────────────
    item_action = st.query_params.get("_item_action", "")
    item_id_val = st.query_params.get("item_id", "")
    if item_action in ("delete_item",):
        for k in ("_item_action", "item_id"):
            try:
                del st.query_params[k]
            except Exception:
                pass
        if item_action == "delete_item" and item_id_val:
            st.session_state["pending_item_delete_id"] = item_id_val
        st.rerun()
        return

    # ── Handle run-session outcome saves ─────────────────────────────────────
    run_action  = st.query_params.get("_run_action", "")
    run_item_id = st.query_params.get("item_id",     "")
    run_outcome = st.query_params.get("outcome",     "")
    if run_action in ("set_outcome",):
        for k in ("_run_action", "item_id", "outcome"):
            try:
                del st.query_params[k]
            except Exception:
                pass
        if run_item_id and run_outcome:
            notes = st.session_state.get(f"notes_{run_item_id}", "")
            update_backlog_item_outcome(run_item_id, run_outcome, notes)
            st.session_state["outcome_saved"] = True
        st.rerun()
        return

    # ── Handle prepare-page toolbar toggles ──────────────────────────────────
    prep_action = st.query_params.get("_prep_action", "")
    if prep_action in ("toggle_add", "toggle_jira"):
        try:
            del st.query_params["_prep_action"]
        except Exception:
            pass
        if prep_action == "toggle_add":
            new_val = not st.session_state.get("show_add_item", False)
            st.session_state["show_add_item"] = new_val
            if new_val:
                st.session_state["show_jira_panel"] = False
                st.session_state.pop("jira_issues", None)
        elif prep_action == "toggle_jira":
            new_val = not st.session_state.get("show_jira_panel", False)
            st.session_state["show_jira_panel"] = new_val
            if new_val:
                st.session_state["show_add_item"] = False
            if not new_val:
                st.session_state.pop("jira_issues", None)
        st.rerun()
        return

    # ── Handle nav-click redirects (query params set by HTML links) ───────────
    nav_dest  = st.query_params.get("_nav",  "")
    team_dest = st.query_params.get("_team", "")

    if nav_dest or team_dest:
        for k in ["_nav", "_team"]:
            try:
                del st.query_params[k]
            except Exception:
                pass

        if nav_dest == "logout":
            do_logout()
            st.rerun()
            return

        if team_dest:
            teams_list = get_teams()
            name_map   = {t["id"]: t["name"] for t in teams_list}
            if team_dest in name_map:
                st.session_state["current_team_id"]   = team_dest
                st.session_state["current_team_name"] = name_map[team_dest]
                st.session_state.pop("current_session_id",   None)
                st.session_state.pop("current_session_name", None)
                st.session_state["page"] = "sessions"
                st.rerun()
                return

        if nav_dest == "teams":
            st.session_state["page"] = "teams"
            st.rerun()
            return
        elif nav_dest == "sessions" and team_id:
            st.session_state["page"] = "sessions"
            st.rerun()
            return
        elif nav_dest == "view_summary" and st.session_state.get("current_session_id"):
            st.session_state["page"] = "summary"
            st.rerun()
            return

    # ── CSS ───────────────────────────────────────────────────────────────────
    st.markdown("""
<style>
.main .block-container {
    padding-top: 68px !important;
    padding-left: 32px !important;
    padding-right: 32px !important;
    max-width: 1200px !important;
}
.tn-bar {
    position: fixed; top: 0; left: 0; right: 0;
    height: 52px; background: #1e2a3a; z-index: 9999;
    box-shadow: 0 2px 8px rgba(0,0,0,0.2);
    display: flex; align-items: center;
    padding: 0 32px; gap: 0;
    font-family: 'Segoe UI', Arial, sans-serif;
    box-sizing: border-box;
}
.tn-brand {
    font-size: 14px; font-weight: 700; color: #90CAF9;
    white-space: nowrap; margin-right: 24px; letter-spacing: 0.3px;
    text-decoration: none !important;
}
.tn-items { display: flex; align-items: center; gap: 2px; flex: 1; overflow: hidden; }
a.tn-btn {
    display: inline-block; text-decoration: none !important;
    font-size: 13px; font-weight: 600;
    padding: 6px 14px; border-radius: 6px;
    white-space: nowrap; cursor: pointer;
    transition: background 0.15s, color 0.15s;
    line-height: 1.3;
}
a.tn-active  { background: #1565C0 !important; color: #fff !important; }
a.tn-inactive { background: none !important; color: #aaa !important; }
a.tn-inactive:hover { background: #2c3e50 !important; color: #fff !important; }
span.tn-current {
    display: inline-block; font-size: 13px; font-weight: 600;
    padding: 6px 14px; border-radius: 6px;
    background: #1565C0; color: #fff; white-space: nowrap; line-height: 1.3;
}
.tn-sep { color: #3d5166; font-size: 14px; padding: 0 4px; user-select: none; flex-shrink: 0; }
.tn-select {
    background: #2c3e50; border: 1px solid #3d5166;
    color: #fff; font-size: 13px;
    padding: 5px 10px; border-radius: 6px; cursor: pointer;
    flex-shrink: 0;
}
.tn-right { display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
.tn-user  { font-size: 12px; color: #7f8c8d; white-space: nowrap; }
a.tn-logout {
    text-decoration: none !important; white-space: nowrap;
    background: none; border: 1px solid #3d5166;
    color: #aaa !important; font-size: 12px; font-weight: 600;
    padding: 5px 12px; border-radius: 6px; cursor: pointer;
}
a.tn-logout:hover { border-color: #aaa; color: #fff !important; }
</style>
""", unsafe_allow_html=True)

    # ── Build breadcrumb ──────────────────────────────────────────────────────
    q = f"sid={sid}&" if sid else ""

    if page == "teams":
        crumb = '<span class="tn-current">Your Teams</span>'

    elif page == "sessions":
        team_sel = _build_team_options_html(q, team_id)
        crumb = (
            f'<a href="?{q}_nav=teams" target="_self" class="tn-btn tn-inactive">Your Teams</a>'
            f'<span class="tn-sep">›</span>'
            f'{team_sel}'
            f'<span class="tn-sep">›</span>'
            f'<span class="tn-current">Sessions</span>'
        )

    elif page in ("prepare", "run_session", "summary"):
        raw   = sess_name or "Session"
        label = _html.escape((raw[:22] + "…") if len(raw) > 22 else raw)
        team_sel = _build_team_options_html(q, team_id)
        crumb = (
            f'<a href="?{q}_nav=teams" target="_self" class="tn-btn tn-inactive">Your Teams</a>'
            f'<span class="tn-sep">›</span>'
            f'{team_sel}'
            f'<span class="tn-sep">›</span>'
            f'<a href="?{q}_nav=sessions" target="_self" class="tn-btn tn-inactive">Sessions</a>'
            f'<span class="tn-sep">›</span>'
            f'<span class="tn-current">{label}</span>'
        )

    else:
        crumb = '<span class="tn-current">Your Teams</span>'

    # ── Render nav bar HTML ───────────────────────────────────────────────────
    st.markdown(f"""
<div class="tn-bar">
  <span class="tn-brand">Backlog Refinement Advisor</span>
  <div class="tn-items">{crumb}</div>
  <div class="tn-right">
    <span class="tn-user">{email}</span>
    <a href="?{q}_nav=logout" target="_self" class="tn-logout">Log Out</a>
  </div>
</div>
""", unsafe_allow_html=True)


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    params = st.query_params

    if params.get("type") == "recovery":
        if "token_hash" in params:
            handle_password_recovery(token_hash=params["token_hash"])
            return
        if "code" in params:
            handle_password_recovery(code=params["code"])
            return
        if "access_token" in params:
            handle_password_recovery(
                access_token=params["access_token"],
                refresh_token=params.get("refresh_token", ""),
            )
            return

    if not is_authenticated():
        sid = params.get("sid")
        if sid:
            if not load_server_session(sid):
                try:
                    del st.query_params["sid"]
                except Exception:
                    pass
                page_login()
                return
        elif not restore_session():
            page_login()
            return

    try:
        if not restore_session():
            clear_session()
            page_login()
            return
        update_server_session()
        show_topnav()

        page       = st.session_state.get("page", "teams")
        team_id    = st.session_state.get("current_team_id")
        session_id = st.session_state.get("current_session_id")

        if page == "teams":
            page_teams()
        elif page == "sessions":
            if not team_id:
                page_teams()
            else:
                page_sessions()
        elif page in ("prepare", "assessment"):
            if not team_id:
                page_teams()
            elif not session_id:
                page_sessions()
            else:
                page_prepare()
        elif page == "run_session":
            if not team_id:
                page_teams()
            elif not session_id:
                page_sessions()
            else:
                page_run_session()
        elif page == "summary":
            if not team_id:
                page_teams()
            elif not session_id:
                page_sessions()
            else:
                page_summary()
        else:
            page_teams()

    except Exception as e:
        if is_auth_error(e):
            clear_session()
            st.warning("Your session has expired. Please log in again.")
            page_login()
        else:
            raise


main()
