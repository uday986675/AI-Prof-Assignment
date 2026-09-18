"""Healthcare AI Agent — Streamlit Demo Frontend.

A conversational interface for the Healthcare AI Access Platform.
Connects to the deployed FastAPI backend via API_BASE_URL env var.

Run locally:
    streamlit run frontend_app.py

Deploy on Render:
    Build:  pip install -r requirements.txt
    Start:  streamlit run frontend_app.py --server.port $PORT --server.address 0.0.0.0 --server.headless true
    Env:    API_BASE_URL=https://ai-prof-assignment-kioi.onrender.com
"""
from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()
import hashlib
import time

import requests
import streamlit as st

# ── Configuration ────────────────────────────────────────────────────────────
API_BASE_URL = os.environ.get(
    "API_BASE_URL",
    "http://127.0.0.1:8000"
).rstrip("/")

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Healthcare AI Assistant",
    page_icon="🏥",
    layout="centered",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ───────────────────────────────────────────────────────────────
st.markdown("""
<style>
    /* Global refinements */
    .stApp {
        background: linear-gradient(135deg, #0f172a 0%, #1a1a2e 50%, #0f172a 100%);
    }

    /* Header styling */
    .app-header {
        text-align: center;
        padding: 1.5rem 0 1rem;
    }
    .app-header h1 {
        background: linear-gradient(135deg, #38bdf8, #818cf8, #c084fc);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        font-size: 2rem;
        font-weight: 700;
        margin-bottom: 0.25rem;
    }
    .app-header p {
        color: #94a3b8;
        font-size: 0.95rem;
    }

    /* Chat bubbles */
    [data-testid="stChatMessage"] {
        border-radius: 12px;
        margin-bottom: 0.5rem;
        border: 1px solid rgba(56, 189, 248, 0.08);
    }

    /* Booking confirmation card */
    .booking-card {
        background: linear-gradient(135deg, #064e3b, #065f46);
        border: 1px solid #10b981;
        border-radius: 12px;
        padding: 1.25rem;
        margin: 0.75rem 0;
    }
    .booking-card h4 {
        color: #34d399;
        margin: 0 0 0.75rem;
        font-size: 1.05rem;
    }
    .booking-card .detail {
        color: #d1fae5;
        font-size: 0.92rem;
        line-height: 1.7;
    }

    /* Error card */
    .error-card {
        background: rgba(239, 68, 68, 0.1);
        border: 1px solid rgba(239, 68, 68, 0.3);
        border-radius: 12px;
        padding: 1rem;
        margin: 0.5rem 0;
        color: #fca5a5;
    }

    /* Status pill */
    .status-pill {
        display: inline-block;
        padding: 0.2rem 0.75rem;
        border-radius: 99px;
        font-size: 0.78rem;
        font-weight: 600;
    }
    .pill-ok { background: rgba(52,211,153,0.15); color: #34d399; border: 1px solid rgba(52,211,153,0.3); }
    .pill-warn { background: rgba(251,191,36,0.15); color: #fbbf24; border: 1px solid rgba(251,191,36,0.3); }
    .pill-info { background: rgba(56,189,248,0.15); color: #38bdf8; border: 1px solid rgba(56,189,248,0.3); }

    /* Sidebar polish */
    section[data-testid="stSidebar"] {
        background: #111827;
        border-right: 1px solid #1e293b;
    }
    section[data-testid="stSidebar"] .stMarkdown h3 {
        color: #38bdf8;
    }

    /* Voice section */
    .voice-section {
        background: rgba(30, 41, 59, 0.6);
        border: 1px solid #334155;
        border-radius: 12px;
        padding: 1rem;
        margin-top: 0.5rem;
    }
</style>
""", unsafe_allow_html=True)


# ── Session state defaults ───────────────────────────────────────────────────
def _init_state():
    defaults = {
        "token": None,
        "user_name": None,
        "user_role": None,
        "conversation_id": None,
        "messages": [],          # list of {"role": "user"|"assistant", "content": str}
        "voice_status": None,
        "processed_audio_hashes": set(),
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init_state()


# ── API helpers ──────────────────────────────────────────────────────────────
def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if st.session_state.token:
        h["Authorization"] = f"Bearer {st.session_state.token}"
    return h


def _api_get(path: str, **kwargs) -> requests.Response:
    return requests.get(f"{API_BASE_URL}{path}", headers=_headers(), timeout=30, **kwargs)


def _api_post(path: str, json_data: dict | None = None, **kwargs) -> requests.Response:
    return requests.post(f"{API_BASE_URL}{path}", headers=_headers(), json=json_data, timeout=60, **kwargs)


def _api_post_form(path: str, files: dict, data: dict | None = None) -> requests.Response:
    """Multipart POST for voice uploads — no Content-Type header (requests sets it)."""
    h = {}
    if st.session_state.token:
        h["Authorization"] = f"Bearer {st.session_state.token}"
    return requests.post(f"{API_BASE_URL}{path}", headers=h, files=files, data=data or {}, timeout=60)


def _handle_error(resp: requests.Response) -> str:
    """Extract a user-friendly error message from a failed response."""
    code = resp.status_code
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        detail = resp.text

    if code == 401:
        st.session_state.token = None
        return "⚠️ Session expired. Please log in again."
    if code == 403:
        return f"🚫 Access denied: {detail}"
    if code == 404:
        return f"🔍 Not found: {detail}"
    if code == 409:
        return f"⚠️ Conflict: {detail}"
    if code == 422:
        return f"⚠️ Validation error: {detail}"
    return f"❌ Error ({code}): {detail}"


# ── Sidebar: Auth ────────────────────────────────────────────────────────────
def _render_sidebar():
    with st.sidebar:
        st.markdown("### 🏥 Healthcare AI")
        st.caption("AI-Powered Appointment Assistant")
        st.divider()

        if st.session_state.token:
            _render_logged_in_sidebar()
        else:
            _render_auth_forms()

        # Footer
        st.divider()
        st.caption(f"🔗 Backend: `{API_BASE_URL}`")


def _render_auth_forms():
    tab_login, tab_register = st.tabs(["🔑 Login", "📝 Register"])

    with tab_login:
        with st.form("login_form"):
            email = st.text_input("Email", value="patient@demo.health", key="login_email")
            password = st.text_input("Password", type="password", value="Demo1234!", key="login_password")
            submitted = st.form_submit_button("Log In", use_container_width=True, type="primary")
            if submitted:
                _do_login(email, password)

    with tab_register:
        with st.form("register_form"):
            full_name = st.text_input("Full Name", key="reg_name")
            email = st.text_input("Email", key="reg_email")
            password = st.text_input("Password", type="password", key="reg_password",
                                     help="Minimum 8 characters")
            phone = st.text_input("Phone (optional)", key="reg_phone")
            dob = st.text_input("Date of Birth (optional, YYYY-MM-DD)", key="reg_dob")
            submitted = st.form_submit_button("Create Account", use_container_width=True)
            if submitted:
                _do_register(full_name, email, password, phone, dob)


def _do_login(email: str, password: str):
    with st.spinner("Logging in…"):
        try:
            resp = _api_post("/auth/login", {"email": email.strip(), "password": password})
        except requests.ConnectionError:
            st.error("Cannot reach the backend. Is it running?")
            return

    if resp.ok:
        data = resp.json()
        st.session_state.token = data["access_token"]
        st.session_state.user_name = data["full_name"]
        st.session_state.user_role = data["role"]
        st.success(f"Welcome, {data['full_name']}!")
        _fetch_voice_status()
        time.sleep(0.5)
        st.rerun()
    else:
        st.error(_handle_error(resp))


def _do_register(full_name: str, email: str, password: str, phone: str, dob: str):
    if not full_name or not email or not password:
        st.warning("Name, email, and password are required.")
        return
    if len(password) < 8:
        st.warning("Password must be at least 8 characters.")
        return

    payload: dict = {"full_name": full_name.strip(), "email": email.strip(), "password": password}
    if phone.strip():
        payload["phone"] = phone.strip()
    if dob.strip():
        payload["date_of_birth"] = dob.strip()

    with st.spinner("Creating account…"):
        try:
            resp = _api_post("/auth/register-patient", payload)
        except requests.ConnectionError:
            st.error("Cannot reach the backend. Is it running?")
            return

    if resp.ok:
        data = resp.json()
        st.session_state.token = data["access_token"]
        st.session_state.user_name = data["full_name"]
        st.session_state.user_role = data["role"]
        st.success(f"Account created! Welcome, {data['full_name']}!")
        _fetch_voice_status()
        time.sleep(0.5)
        st.rerun()
    else:
        st.error(_handle_error(resp))


def _render_logged_in_sidebar():
    st.markdown(f"👤 **{st.session_state.user_name}**")
    st.markdown(f'<span class="status-pill pill-ok">{st.session_state.user_role}</span>',
                unsafe_allow_html=True)
    st.divider()

    # New conversation
    if st.button("🆕 New Conversation", use_container_width=True, type="primary"):
        _start_new_session()

    # Current session info
    if st.session_state.conversation_id:
        st.caption(f"Session: `{st.session_state.conversation_id[:12]}…`")

    st.divider()

    # Voice status
    vs = st.session_state.voice_status
    if vs:
        st.markdown("#### 🎙️ Voice Status")
        stt = vs.get("stt_provider")
        tts = vs.get("tts_available", False)
        st.markdown(
            f'STT: <span class="status-pill {"pill-ok" if stt else "pill-warn"}">'
            f'{stt or "unavailable"}</span>',
            unsafe_allow_html=True
        )
        st.markdown(
            f'TTS: <span class="status-pill {"pill-ok" if tts else "pill-warn"}">'
            f'{"on" if tts else "off"}</span>',
            unsafe_allow_html=True
        )

    st.divider()

    if st.button("🚪 Logout", use_container_width=True):
        for k in list(st.session_state.keys()):
            del st.session_state[k]
        st.rerun()


# ── Voice status ─────────────────────────────────────────────────────────────
def _fetch_voice_status():
    try:
        resp = _api_get("/agent/voice/status")
        if resp.ok:
            st.session_state.voice_status = resp.json()
    except Exception:
        pass  # non-critical


# ── Session management ───────────────────────────────────────────────────────
def _start_new_session():
    with st.spinner("Starting a new conversation…"):
        try:
            resp = _api_post("/agent/sessions", {})
        except requests.ConnectionError:
            st.error("Cannot reach the backend.")
            return

    if resp.ok:
        data = resp.json()
        st.session_state.conversation_id = data["conversation_id"]
        st.session_state.messages = []
        # Add a welcome message
        welcome = (
            "👋 Hello! I'm your healthcare AI assistant. "
            "I can help you find a doctor and book an appointment.\n\n"
            "Try saying something like:\n"
            '- *"I need to see an orthopedic doctor"*\n'
            '- *"Find me a cardiologist for a video visit"*\n'
            '- *"Show me available appointments for this week"*'
        )
        st.session_state.messages.append({"role": "assistant", "content": welcome})
        st.rerun()
    else:
        st.error(_handle_error(resp))


def _ensure_session() -> bool:
    """Create a session if none exists. Returns True if a session is ready."""
    if st.session_state.conversation_id:
        return True
    with st.spinner("Starting a new conversation…"):
        try:
            resp = _api_post("/agent/sessions", {})
        except requests.ConnectionError:
            st.error("Cannot reach the backend.")
            return False

    if resp.ok:
        data = resp.json()
        st.session_state.conversation_id = data["conversation_id"]
        return True
    else:
        st.error(_handle_error(resp))
        return False


# ── Booking card rendering ───────────────────────────────────────────────────
def _extract_booking_info(meta: dict, state: dict) -> dict | None:
    """Try to extract booking confirmation details from agent response metadata."""
    # Check meta for appointment info
    appt = meta.get("appointment") or meta.get("booking") or {}
    if appt and appt.get("id"):
        return appt

    # Check state for completed booking
    if state.get("status") == "completed" or state.get("booked"):
        return {
            "doctor": state.get("doctor_name", state.get("selected_doctor", "")),
            "specialty": state.get("specialty", ""),
            "start_at": state.get("start_at", state.get("appointment_time", "")),
            "appointment_type": state.get("appointment_type", ""),
            "status": "confirmed",
        }

    # Check meta for action results
    action = meta.get("action")
    if action == "appointment_booked":
        return meta.get("result", {})

    return None


def _render_booking_card(info: dict):
    """Render a highlighted booking confirmation card."""
    doctor = info.get("doctor") or info.get("doctor_name") or "—"
    specialty = info.get("specialty") or ""
    start = info.get("start_at") or info.get("appointment_time") or "—"
    appt_type = info.get("appointment_type") or "—"
    status = info.get("status") or "confirmed"

    details = f"**Doctor:** {doctor}"
    if specialty:
        details += f"  \n**Specialty:** {specialty}"
    details += f"  \n**Date/Time:** {start}"
    details += f"  \n**Type:** {appt_type}"
    details += f"  \n**Status:** ✅ {status.replace('_', ' ').title()}"

    st.markdown(f"""
    <div class="booking-card">
        <h4>✅ Appointment Booked!</h4>
        <div class="detail">{details}</div>
    </div>
    """, unsafe_allow_html=True)


# ── Chat interface ───────────────────────────────────────────────────────────
def _render_chat():
    # Header
    st.markdown("""
    <div class="app-header">
        <h1>🏥 Healthcare AI Assistant</h1>
        <p>Book appointments with AI-powered conversational guidance</p>
    </div>
    """, unsafe_allow_html=True)

    if not st.session_state.token:
        # Not logged in — show welcome
        st.info("👈 **Log in** using the sidebar to start chatting with the AI assistant.")
        st.markdown("""
        **Demo credentials** (pre-filled):
        - Email: `patient@demo.health`
        - Password: `Demo1234!`

        Or create a new patient account using the **Register** tab.
        """)
        return

    # ── Chat history ─────────────────────────────────────────────────────
    for idx, msg in enumerate(st.session_state.messages):
        with st.chat_message(msg["role"], avatar="🤖" if msg["role"] == "assistant" else "👤"):
            st.markdown(msg["content"])
            # Show booking card if present
            booking = msg.get("_booking")
            if booking:
                _render_booking_card(booking)

            # Render explicit clickable slot buttons for offered slots
            offers = msg.get("_offers")
            if offers and idx == len(st.session_state.messages) - 1:
                st.caption("👇 **Click a slot to select:**")
                cols = st.columns(min(len(offers), 3))
                for i, offer in enumerate(offers, start=1):
                    col = cols[(i - 1) % len(cols)]
                    when = offer["start_at"][11:16]
                    doc = offer["doctor_name"]
                    with col:
                        if st.button(f"Option {i}: {doc} ({when})", key=f"btn_offer_{idx}_{i}", use_container_width=True):
                            _handle_text_input(str(i))

    # ── Voice input section ──────────────────────────────────────────────
    vs = st.session_state.voice_status
    if vs and vs.get("stt_provider"):
        with st.expander("🎤 Voice Input", expanded=False):
            st.caption("Record a voice message — it will be transcribed and sent to the AI agent.")
            audio_value = st.audio_input("Record your message", key="voice_input")
            if audio_value is not None:
                raw_bytes = audio_value.getvalue()
                if raw_bytes:
                    audio_hash = hashlib.sha256(raw_bytes).hexdigest()
                    if audio_hash not in st.session_state.processed_audio_hashes:
                        st.session_state.processed_audio_hashes.add(audio_hash)
                        _handle_voice_input(raw_bytes)

    # ── Text input ───────────────────────────────────────────────────────
    if prompt := st.chat_input("Type your message… (e.g. 'I need an orthopedic doctor')"):
        _handle_text_input(prompt)


# ── Message handlers ─────────────────────────────────────────────────────────
def _handle_text_input(message: str):
    # Show user message immediately
    st.session_state.messages.append({"role": "user", "content": message})
    with st.chat_message("user", avatar="👤"):
        st.markdown(message)

    # Ensure we have a session
    if not _ensure_session():
        return

    # Send to agent
    with st.chat_message("assistant", avatar="🤖"):
        with st.spinner("Thinking…"):
            try:
                resp = _api_post(
                    f"/agent/sessions/{st.session_state.conversation_id}/messages",
                    {"message": message}
                )
            except requests.ConnectionError:
                err = "❌ Cannot reach the backend. Please try again."
                st.markdown(err)
                st.session_state.messages.append({"role": "assistant", "content": err})
                return
            except requests.Timeout:
                err = "⏱️ Request timed out. The agent may be processing a complex request. Please try again."
                st.markdown(err)
                st.session_state.messages.append({"role": "assistant", "content": err})
                return

        if resp.ok:
            data = resp.json()
            reply = data.get("reply", "")
            meta = data.get("meta", {})
            state = data.get("state", {})

            st.markdown(reply)

            # Check for booking confirmation
            booking_info = _extract_booking_info(meta, state)
            msg_data = {"role": "assistant", "content": reply}
            if booking_info:
                _render_booking_card(booking_info)
                msg_data["_booking"] = booking_info
            if meta.get("next_action") == "present_availability" and state.get("offered"):
                msg_data["_offers"] = state.get("offered")

            st.session_state.messages.append(msg_data)

            # Handle session expiry on 401 mid-conversation
            new_status = data.get("status", "")
            if new_status == "completed":
                st.balloons()
        else:
            err = _handle_error(resp)
            st.markdown(f'<div class="error-card">{err}</div>', unsafe_allow_html=True)
            st.session_state.messages.append({"role": "assistant", "content": err})


def _handle_voice_input(audio_bytes):
    """Process voice input from st.audio_input."""
    if not _ensure_session():
        return

    raw_bytes = audio_bytes if isinstance(audio_bytes, bytes) else audio_bytes.getvalue()
    if not raw_bytes:
        return

    # Ensure hash is recorded in session_state
    audio_hash = hashlib.sha256(raw_bytes).hexdigest()
    st.session_state.processed_audio_hashes.add(audio_hash)

    # Show thinking state
    st.session_state.messages.append({"role": "user", "content": "🎙️ *(voice message)*"})

    with st.spinner("🎙️ Transcribing & processing…"):
        try:
            resp = _api_post_form(
                f"/agent/sessions/{st.session_state.conversation_id}/voice",
                files={"audio": ("recording.wav", raw_bytes, "audio/wav")},
                data={"mime_type": "audio/wav"},
            )
        except requests.ConnectionError:
            st.error("❌ Cannot reach the backend.")
            return
        except requests.Timeout:
            st.error("⏱️ Voice processing timed out.")
            return

    if resp.ok:
        data = resp.json()
        transcript = data.get("transcript", "")
        reply = data.get("reply", "")
        meta = data.get("meta", {})
        state = data.get("state", {})

        # Update the user message with transcript
        st.session_state.messages[-1]["content"] = f"🎙️ *\"{transcript}\"*"

        # Add agent reply
        booking_info = _extract_booking_info(meta, state)
        msg_data = {"role": "assistant", "content": reply}
        if booking_info:
            msg_data["_booking"] = booking_info
        if meta.get("next_action") == "present_availability" and state.get("offered"):
            msg_data["_offers"] = state.get("offered")
        st.session_state.messages.append(msg_data)

        # Play TTS audio if available (autoplay=False to avoid mic feedback loop)
        audio_b64 = data.get("audio_base64")
        if audio_b64:
            import base64
            audio_wav = base64.b64decode(audio_b64)
            st.audio(audio_wav, format="audio/wav", autoplay=False)

        st.rerun()
    else:
        err = _handle_error(resp)
        st.error(err)
        st.session_state.messages[-1]["content"] = f"🎙️ *(voice failed — type instead)*"


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    _render_sidebar()
    _render_chat()


if __name__ == "__main__":
    main()
else:
    # Streamlit runs the module directly
    main()
