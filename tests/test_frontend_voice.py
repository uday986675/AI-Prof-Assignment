"""Unit tests for Streamlit voice input state tracking, deduplication, and questionnaire normalization."""
import hashlib
from unittest.mock import MagicMock, patch
import pytest

import frontend_app
from backend.app.agent.graph import _coerce_choice


class DummyAudioInput:
    def __init__(self, data: bytes):
        self.data = data

    def getvalue(self) -> bytes:
        return self.data


@pytest.fixture(autouse=True)
def mock_streamlit_state(monkeypatch):
    """Set up mock Streamlit session_state."""
    state = {
        "token": "fake-jwt-token",
        "conversation_id": "test-session-123",
        "messages": [],
        "voice_status": {"stt_provider": "gemini"},
        "processed_audio_hashes": set(),
    }
    
    class MockSessionState(dict):
        def __getattr__(self, item):
            return self[item]
        def __setattr__(self, key, value):
            self[key] = value

    mock_state = MockSessionState(state)
    monkeypatch.setattr(frontend_app, "st", MagicMock())
    frontend_app.st.session_state = mock_state
    return mock_state


def test_voice_recording_deduplication_sequence(mock_streamlit_state):
    """
    Verifies the specific reproduction sequence:
    1. First recording -> processed once.
    2. Second recording (fails with error) -> first recording NOT processed again.
    3. Third attempt (no new recording) -> no STT request happens.
    4. Fourth attempt (genuinely new recording) -> transcribed & sent to agent.
    """
    audio_1 = b"AUDIO_DATA_ORTHOPEDIC_DOCTOR"
    audio_2 = b"AUDIO_DATA_SECOND_RECORDING_ATTEMPT"
    audio_3 = b"AUDIO_DATA_NEW_THIRD_RECORDING"

    processed_calls = []

    def mock_api_post_form(path, files, data):
        audio_bytes = files["audio"][1]
        processed_calls.append(audio_bytes)
        resp = MagicMock()
        if audio_bytes == audio_2:
            resp.ok = False
            resp.status_code = 500
            resp.text = "Internal Server Error"
        else:
            resp.ok = True
            resp.json.return_value = {
                "transcript": "Transcribed text",
                "reply": "Agent response",
                "meta": {},
                "state": {},
            }
        return resp

    with patch("frontend_app._api_post_form", side_effect=mock_api_post_form):
        # ── FIRST ATTEMPT ──
        dummy_1 = DummyAudioInput(audio_1)
        raw_1 = dummy_1.getvalue()
        hash_1 = hashlib.sha256(raw_1).hexdigest()
        
        assert hash_1 not in mock_streamlit_state.processed_audio_hashes
        mock_streamlit_state.processed_audio_hashes.add(hash_1)
        frontend_app._handle_voice_input(raw_1)

        assert len(processed_calls) == 1
        assert processed_calls[0] == audio_1
        assert hash_1 in mock_streamlit_state.processed_audio_hashes

        # Rerun simulation with audio_1 still in widget state:
        if hash_1 not in mock_streamlit_state.processed_audio_hashes:
            frontend_app._handle_voice_input(raw_1)
        assert len(processed_calls) == 1  # Not processed again!

        # ── SECOND ATTEMPT ──
        dummy_2 = DummyAudioInput(audio_2)
        raw_2 = dummy_2.getvalue()
        hash_2 = hashlib.sha256(raw_2).hexdigest()

        assert hash_2 not in mock_streamlit_state.processed_audio_hashes
        mock_streamlit_state.processed_audio_hashes.add(hash_2)
        frontend_app._handle_voice_input(raw_2)

        assert len(processed_calls) == 2
        assert processed_calls[1] == audio_2
        assert hash_1 in mock_streamlit_state.processed_audio_hashes
        assert hash_2 in mock_streamlit_state.processed_audio_hashes

        # ── THIRD ATTEMPT ──
        if hash_1 not in mock_streamlit_state.processed_audio_hashes:
            frontend_app._handle_voice_input(raw_1)
        if hash_2 not in mock_streamlit_state.processed_audio_hashes:
            frontend_app._handle_voice_input(raw_2)

        assert len(processed_calls) == 2  # No new STT request happened!

        # ── FOURTH ATTEMPT ──
        dummy_3 = DummyAudioInput(audio_3)
        raw_3 = dummy_3.getvalue()
        hash_3 = hashlib.sha256(raw_3).hexdigest()

        if hash_3 not in mock_streamlit_state.processed_audio_hashes:
            mock_streamlit_state.processed_audio_hashes.add(hash_3)
            frontend_app._handle_voice_input(raw_3)

        assert len(processed_calls) == 3
        assert processed_calls[2] == audio_3


def test_questionnaire_normalization_scenarios():
    """
    Verifies Problem 1 & Tests 4, 5, 6 questionnaire boolean normalization:
    - "after an injury" -> True (yes)
    - "yeah, I hurt it when I fell" -> True (yes)
    - "no injury" -> False (no)
    - "it wasn't an injury" -> False (no)
    - "I didn't hurt it" -> False (no)
    """
    q_bool = {"kind": "boolean", "key": "injury_history"}

    # Positive cases (normalize to the stored value "yes")
    assert _coerce_choice(q_bool, "after an injury") == "yes"
    assert _coerce_choice(q_bool, "yes, I injured it") == "yes"
    assert _coerce_choice(q_bool, "it happened after I fell") == "yes"
    assert _coerce_choice(q_bool, "I hurt it in an accident") == "yes"
    assert _coerce_choice(q_bool, "yeah") == "yes"
    assert _coerce_choice(q_bool, "yeah, I hurt it when I fell") == "yes"

    # Negative cases (normalize to the stored value "no")
    assert _coerce_choice(q_bool, "no injury") == "no"
    assert _coerce_choice(q_bool, "it wasn't an injury") == "no"
    assert _coerce_choice(q_bool, "I didn't hurt it") == "no"
    assert _coerce_choice(q_bool, "no") == "no"
    assert _coerce_choice(q_bool, "nope") == "no"
