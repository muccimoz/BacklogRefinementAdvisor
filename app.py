import time
import streamlit as st
import httpx
import anthropic
from datetime import date as date_type
from supabase import create_client, Client

st.set_page_config(
    page_title="Backlog Refinement Advisor",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Hide Streamlit chrome ──────────────────────────────────────────────────────
st.markdown("""
<style>
#MainMenu  {visibility: hidden;}
footer     {visibility: hidden;}
header     {visibility: hidden;}
section[data-testid="stSidebar"] {background-color: #1e2a3a;}
section[data-testid="stSidebar"] * {color: #ffffff !important;}
section[data-testid="stSidebar"] .stButton > button {
    background-color: #2c3e50 !important; color: #ffffff !important; border: none;
    width: 100%; text-align: left;
}
section[data-testid="stSidebar"] .stButton > button:hover {background-color: #34495e !important;}
section[data-testid="stSidebar"] .stSelectbox div[data-baseweb="select"] * {color: #000000 !important;}
</style>
""", unsafe_allow_html=True)


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
        r = db().table("refinement_sessions").select("id, name, created_at").eq(
            "team_id", team_id
        ).order("created_at", desc=True).execute()
        return r.data or []
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

## Clarity Gradient: [rating]

[Explain why this rating was assigned, referencing specific aspects of the submission]

## Refinement Threshold: [zone]

[Explain why this zone was assigned]

## Checklist Analysis

### 1. Shared Understanding
[For each of the 4 items: one line stating whether it is satisfied, a gap, or unclear based on what was submitted. For any gaps, add 1-2 clarifying questions indented beneath it.]

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


# ── Dialogs ────────────────────────────────────────────────────────────────────
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
    st.title("Your Teams")

    if st.session_state.pop("team_created_success", None):
        st.success(st.session_state.pop("team_created_name", "Team created."))
    if st.session_state.pop("team_deleted_success", None):
        st.success(st.session_state.pop("team_deleted_name", "Team deleted."))
    if st.session_state.pop("team_renamed_success", None):
        st.success("Team renamed.")

    teams = get_teams()

    with st.expander("How to use this page"):
        st.markdown("""
- Each team has its own refinement sessions and backlog assessments.
- Click **Open** to view and manage refinement sessions for that team.
- Use **Add New Team** to create a separate team for each group you want to track independently.
- Use **Rename** to update a team's name, or **Delete** to permanently remove it and all its data.
        """)

    with st.expander("Add New Team", expanded=(len(teams) == 0)):
        with st.form("add_team"):
            name = st.text_input("Team Name")
            if st.form_submit_button("Add Team"):
                if name.strip():
                    create_team(name.strip())
                    st.session_state["team_created_success"] = True
                    st.session_state["team_created_name"]    = f"Team '{name.strip()}' created."
                    st.rerun()
                else:
                    st.warning("Please enter a team name.")

    if not teams:
        st.info("No teams yet. Add one above to get started.")
        return

    st.divider()

    for team in teams:
        col_name, col_open, col_rename, col_delete = st.columns([5, 2, 2, 2])
        col_name.write(f"**{team['name']}**")

        if col_open.button("Open", key=f"open_{team['id']}"):
            st.session_state["current_team_id"]   = team["id"]
            st.session_state["current_team_name"] = team["name"]
            st.session_state["sidebar_team_sel"]  = team["id"]
            st.session_state["page"]              = "sessions"
            st.rerun()

        if col_rename.button("Rename", key=f"rename_{team['id']}"):
            st.session_state[f"renaming_{team['id']}"] = True
            st.rerun()

        if col_delete.button("Delete", key=f"delete_{team['id']}"):
            _dialog_delete_team(team)

        if st.session_state.get(f"renaming_{team['id']}"):
            with st.form(f"rename_form_{team['id']}"):
                new_name = st.text_input("New name", value=team["name"])
                c1, c2   = st.columns(2)
                save     = c1.form_submit_button("Save")
                cancel   = c2.form_submit_button("Cancel")
            if save:
                if new_name.strip():
                    update_team(team["id"], new_name.strip())
                    if st.session_state.get("current_team_id") == team["id"]:
                        st.session_state["current_team_name"] = new_name.strip()
                    st.session_state.pop(f"renaming_{team['id']}", None)
                    st.session_state["team_renamed_success"] = True
                    st.rerun()
                else:
                    st.warning("Name cannot be empty.")
            if cancel:
                st.session_state.pop(f"renaming_{team['id']}", None)
                st.rerun()


def page_sessions():
    team_name = st.session_state.get("current_team_name", "Team")
    st.title(f"Refinement Sessions — {team_name}")

    if st.session_state.pop("session_created", None):
        st.success(st.session_state.pop("session_created_name", "Session created."))
    if st.session_state.pop("session_deleted", None):
        st.success(st.session_state.pop("session_deleted_name", "Session deleted."))
    if st.session_state.pop("session_renamed", None):
        st.success("Session renamed.")

    team_id  = st.session_state["current_team_id"]
    sessions = get_refinement_sessions(team_id)

    today        = date_type.today()
    default_name = f"Session — {today.strftime('%b')} {today.day} {today.year}"

    with st.expander("How to use this page"):
        st.markdown("""
- Each refinement session represents one backlog refinement meeting.
- Click **Open** to submit and review backlog items assessed within that session.
- Use **Add New Session** to create a session for your next refinement meeting.
- Use **Rename** to update a session name, or **Delete** to remove it and all its items.
        """)

    with st.expander("Add New Session", expanded=(len(sessions) == 0)):
        with st.form("add_session"):
            name = st.text_input("Session Name", value=default_name)
            if st.form_submit_button("Add Session"):
                if name.strip():
                    create_refinement_session(team_id, name.strip())
                    st.session_state["session_created"]      = True
                    st.session_state["session_created_name"] = f"Session '{name.strip()}' created."
                    st.rerun()
                else:
                    st.warning("Please enter a session name.")

    if not sessions:
        st.info("No sessions yet. Add one above to get started.")
        return

    st.divider()

    for session in sessions:
        col_name, col_open, col_rename, col_delete = st.columns([5, 2, 2, 2])
        col_name.write(f"**{session['name']}**")

        if col_open.button("Open", key=f"open_sess_{session['id']}"):
            st.session_state["current_session_id"]   = session["id"]
            st.session_state["current_session_name"] = session["name"]
            st.session_state["page"]                 = "assessment"
            st.rerun()

        if col_rename.button("Rename", key=f"rename_sess_{session['id']}"):
            st.session_state[f"renaming_sess_{session['id']}"] = True
            st.rerun()

        if col_delete.button("Delete", key=f"delete_sess_{session['id']}"):
            _dialog_delete_session(session)

        if st.session_state.get(f"renaming_sess_{session['id']}"):
            with st.form(f"rename_sess_form_{session['id']}"):
                new_name = st.text_input("New name", value=session["name"])
                c1, c2   = st.columns(2)
                save     = c1.form_submit_button("Save")
                cancel   = c2.form_submit_button("Cancel")
            if save:
                if new_name.strip():
                    update_refinement_session(session["id"], new_name.strip())
                    if st.session_state.get("current_session_id") == session["id"]:
                        st.session_state["current_session_name"] = new_name.strip()
                    st.session_state.pop(f"renaming_sess_{session['id']}", None)
                    st.session_state["session_renamed"] = True
                    st.rerun()
                else:
                    st.warning("Name cannot be empty.")
            if cancel:
                st.session_state.pop(f"renaming_sess_{session['id']}", None)
                st.rerun()


def page_assessment():
    session_name = st.session_state.get("current_session_name", "Session")
    team_name    = st.session_state.get("current_team_name", "Team")
    st.title(session_name)
    st.caption(f"Team: {team_name}")

    if st.session_state.pop("item_submitted", None):
        st.success("Assessment complete.")
    if st.session_state.pop("item_deleted", None):
        st.success(st.session_state.pop("item_deleted_name", "Item deleted."))

    session_id = st.session_state["current_session_id"]
    items      = get_backlog_items(session_id)

    with st.expander("How to use this page"):
        st.markdown("""
- Enter a backlog item's details and click **Run Assessment** to get an AI evaluation.
- Gemini evaluates the item against Mike Cohn's 14-point refinement checklist.
- Each item receives a **Clarity Gradient** rating (High / Moderate / Low) and a
  **Refinement Threshold** zone (Too Vague / Refinement Zone / Over-Refined).
- Only the Title is required — the more detail you provide, the better the evaluation.
- All assessed items for this session are shown below the form.
        """)

    with st.expander("Submit New Item for Assessment", expanded=True):
        with st.form("assessment_form"):
            title               = st.text_input("Title *")
            description         = st.text_area("Description", height=100,
                                               placeholder="What needs to be done and why (the problem being solved)")
            acceptance_criteria = st.text_area("Acceptance Criteria", height=100,
                                               placeholder="What must be true for this item to be considered complete")
            dependencies        = st.text_input("Dependencies",
                                               placeholder="Other items or systems this relies on")
            assumptions         = st.text_input("Assumptions",
                                               placeholder="What the team is taking as given")
            notes               = st.text_area("Notes", height=80,
                                              placeholder="Anything else relevant")
            submitted = st.form_submit_button("Run Assessment")

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
                            session_id, title.strip(), description, acceptance_criteria,
                            dependencies, assumptions, notes, clarity, zone, output,
                        )
                        st.session_state["item_submitted"] = True
                        st.rerun()
                    except Exception as e:
                        st.error(f"Assessment failed: {e}")

    if not items:
        st.info("No items assessed in this session yet.")
        return

    st.divider()
    st.subheader("Assessed Items")

    for item in items:
        clarity = item.get("clarity_gradient", "")
        zone    = item.get("threshold_zone", "")
        label   = f"{item['title']}   |   {clarity}   |   {zone}"

        with st.expander(label):
            c1, c2 = st.columns(2)
            c1.markdown(f"**Clarity Gradient:** {clarity}")
            c2.markdown(f"**Refinement Threshold:** {zone}")
            st.markdown("---")

            if item.get("gemini_output"):
                st.markdown(item["gemini_output"])

            st.markdown("---")
            if st.button("Delete this item", key=f"del_item_{item['id']}"):
                _dialog_delete_item(item)


# ── Sidebar ────────────────────────────────────────────────────────────────────
def show_sidebar():
    with st.sidebar:
        st.markdown("### Backlog Refinement Advisor")
        st.markdown("---")

        if st.button("Your Teams", use_container_width=True):
            st.session_state["page"] = "teams"
            st.rerun()

        teams         = get_teams()
        current_page  = st.session_state.get("page", "teams")
        on_teams_page = current_page == "teams"

        if teams and not on_teams_page:
            team_ids   = [t["id"] for t in teams]
            current_id = st.session_state.get("current_team_id")

            if st.session_state.get("sidebar_team_sel") not in team_ids:
                st.session_state["sidebar_team_sel"] = current_id or team_ids[0]

            name_by_id = {t["id"]: t["name"] for t in teams}
            sel_id = st.selectbox(
                "Team",
                options=team_ids,
                format_func=lambda tid: name_by_id[tid],
                key="sidebar_team_sel",
                label_visibility="collapsed",
            )

            if sel_id != current_id:
                st.session_state["current_team_id"]   = sel_id
                st.session_state["current_team_name"] = name_by_id[sel_id]
                st.session_state.pop("current_session_id",   None)
                st.session_state.pop("current_session_name", None)
                st.session_state["page"] = "sessions"
                st.rerun()

            if st.session_state.get("current_team_id"):
                if st.button("Sessions", use_container_width=True):
                    st.session_state["page"] = "sessions"
                    st.rerun()

        st.markdown("---")
        if st.button("Log Out", use_container_width=True):
            do_logout()
            st.rerun()


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
        show_sidebar()

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
        elif page == "assessment":
            if not team_id:
                page_teams()
            elif not session_id:
                page_sessions()
            else:
                page_assessment()
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
