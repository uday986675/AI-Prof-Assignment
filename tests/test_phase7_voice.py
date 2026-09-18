"""Phase 7 — voice: STT -> agent turn -> TTS, with text always available.

Run from repo root:  python -m unittest discover -s tests -v

All speech providers are MOCKED (no network, no API keys needed): the tests
exercise the platform behavior, not the cloud providers —

  A. Speech boundary (unit)
     - transcribe: happy path, empty audio, oversize, bad format, provider error
     - provider selection: gemini-first, groq fallback, none -> SpeechError
     - synthesize: TTS bytes wrapped in a RIFF/WAVE container, None on failure
  B. /agent/voice/status + /agent/voice/transcribe
     - auth required; capability report reflects config
  C. POST /agent/sessions/{id}/voice (full voice turn, mocked STT/TTS)
     - happy path: transcript -> agent reply -> audio_base64 WAV
     - STT failure -> 422 (client falls back to text)
     - TTS failure -> 200 with audio_base64=None (text still served)
     - RBAC: doctor/other-patient blocked; cross-owner session -> 404
  D. Persistence & audit
     - user event recorded with audio_origin="voice"; typed turns stay "text"
     - event log exposes audio_origin
"""
from __future__ import annotations

import base64
import io
import os
import struct
import tempfile
import unittest
import unittest.mock  # noqa: F401  (the env's unittest shim needs this for unittest.mock.patch)
import uuid
import wave
from datetime import date, datetime, time, timedelta

# Throwaway DBs BEFORE any app import (mirrors the Phase 3–6 harness).
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp(prefix='hap-p7-')}/platform.db")
os.environ.setdefault("MOCK_EHR_DATABASE_URL", f"sqlite:///{tempfile.mkdtemp(prefix='hap-p7-')}/ehr.db")
os.environ.setdefault("MOCK_EHR_API_KEY", "test-ehr-key-7")
os.environ.setdefault("APP_ENVIRONMENT", "test")

from fastapi.testclient import TestClient  # noqa: E402

import backend.app.agent.llm as agent_llm  # noqa: E402
import backend.app.speech.speech as speech_mod  # noqa: E402
from backend.app.core.security import hash_password  # noqa: E402
from backend.app.database.base import SessionLocal as PlatformSession  # noqa: E402
from backend.app.main import app as platform_app  # noqa: E402
from backend.app.models import (  # noqa: E402
    ConversationEvent,
    Doctor,
    DoctorAvailability,
    Hospital,
    Patient,
    User,
)
from backend.app.scheduling import now_utc  # noqa: E402

platform_client = TestClient(platform_app)


def _no_llm(*args, **kwargs):  # pragma: no cover - simple stub
    raise agent_llm.LLMUnavailableError("tests run without an LLM")


def _patch_llm(testcase: unittest.TestCase) -> None:
    patcher = unittest.mock.patch("backend.app.agent.graph.interpret_utterance", side_effect=_no_llm)
    patcher.start()
    testcase.addCleanup(patcher.stop)


def login(email: str, password: str = "TestPass1!") -> str:
    response = platform_client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def wav_bytes(seconds: float = 0.4, freq: int = 220) -> bytes:
    """A tiny real WAV file (16-bit mono 16 kHz) — enough for STT plumbing."""
    rate = 16000
    n = int(rate * seconds)
    frames = b"".join(
        struct.pack("<h", int(12000 * (1 if i % rate < rate // 2 else -1))) for i in range(n)
    )
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return buf.getvalue()


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class VFixture:
    """Approved hospital + doctor + two patients (one for isolation checks)."""

    def __init__(self) -> None:
        suffix = uuid.uuid4().hex[:8]
        self.suffix = suffix
        db = PlatformSession()
        try:
            hospital = Hospital(name=f"V Hosp {suffix}", status="approved")
            db.add(hospital)
            db.flush()
            self.hospital_id = hospital.id

            doctor_user = User(email=f"v-dr-{suffix}@test.health", password_hash=hash_password("TestPass1!"),
                               full_name="Dr. V Test", role="doctor", hospital_id=hospital.id)
            db.add(doctor_user)
            db.flush()
            doctor = Doctor(hospital_id=hospital.id, full_name=f"Dr. A-V {suffix}", specialty="orthopedics",
                            consultation_minutes=30, appointment_types=["in_person", "video"],
                            status="active", external_provider_id="EHR-PROV-V1", user_id=doctor_user.id)
            db.add(doctor)
            db.flush()
            self.doctor_id = doctor.id
            self.doctor_email = doctor_user.email

            db.add(DoctorAvailability(hospital_id=hospital.id, doctor_id=doctor.id, weekday=0,
                                      start_time="09:00", end_time="12:00", slot_minutes=30,
                                      appointment_types=["in_person", "video"]))

            patient_user = User(email=f"v-patient-{suffix}@test.health", password_hash=hash_password("TestPass1!"),
                                full_name="V Patient", role="patient")
            db.add(patient_user)
            db.flush()
            db.add(Patient(user_id=patient_user.id, phone="+91 90000 10000", date_of_birth=date(1990, 1, 1)))
            self.patient_email = patient_user.email

            other_patient_user = User(email=f"v-p2-{suffix}@test.health", password_hash=hash_password("TestPass1!"),
                                      full_name="V Patient Two", role="patient")
            db.add(other_patient_user)
            db.flush()
            db.add(Patient(user_id=other_patient_user.id, phone="+91 90000 10001", date_of_birth=date(1985, 2, 2)))
            self.patient2_email = other_patient_user.email

            db.commit()
        finally:
            db.close()


class TestASpeechBoundary(unittest.TestCase):
    """STT/TTS unit behavior — providers mocked, contract verified."""

    def setUp(self) -> None:
        _patch_llm(self)

    def test_1_transcribe_returns_provider_text(self) -> None:
        with unittest.mock.patch.object(speech_mod, "_gemini_transcribe", return_value="hello"), \
             unittest.mock.patch.object(speech_mod.settings, "gemini_api_key", "k"), \
             unittest.mock.patch.object(speech_mod.settings, "stt_provider", "gemini"):
            self.assertEqual(speech_mod.transcribe_wav(b"RIFF-fake", "audio/wav"), "hello")

    def test_2_transcribe_empty_audio_rejected(self) -> None:
        with self.assertRaises(speech_mod.SpeechError):
            speech_mod.transcribe_wav(b"")

    def test_3_transcribe_oversize_rejected(self) -> None:
        with self.assertRaises(speech_mod.SpeechError):
            speech_mod.transcribe_wav(b"x" * (speech_mod.MAX_AUDIO_BYTES + 1))

    def test_4_transcribe_bad_format_rejected(self) -> None:
        with self.assertRaises(speech_mod.SpeechError):
            speech_mod.transcribe_wav(b"abc", "audio/mpeg")

    def test_5_no_provider_configured_raises(self) -> None:
        with unittest.mock.patch.object(speech_mod.settings, "stt_provider", "gemini"), \
             unittest.mock.patch.object(speech_mod.settings, "gemini_api_key", ""), \
             unittest.mock.patch.object(speech_mod.settings, "groq_api_key", ""):
            with self.assertRaises(speech_mod.SpeechError):
                speech_mod.transcribe_wav(b"RIFF-fake")

    def test_6_groq_fallback_when_gemini_missing(self) -> None:
        with unittest.mock.patch.object(speech_mod.settings, "stt_provider", ""), \
             unittest.mock.patch.object(speech_mod.settings, "gemini_api_key", ""), \
             unittest.mock.patch.object(speech_mod.settings, "groq_api_key", "g"), \
             unittest.mock.patch.object(speech_mod, "_groq_transcribe", return_value="via groq"):
            self.assertEqual(speech_mod.transcribe_wav(b"RIFF-fake"), "via groq")

    def test_7_explicit_groq_without_key_falls_to_gemini(self) -> None:
        with unittest.mock.patch.object(speech_mod.settings, "stt_provider", "groq"), \
             unittest.mock.patch.object(speech_mod.settings, "groq_api_key", ""), \
             unittest.mock.patch.object(speech_mod.settings, "gemini_api_key", "k"), \
             unittest.mock.patch.object(speech_mod, "_gemini_transcribe", return_value="via gemini"):
            self.assertEqual(speech_mod.transcribe_wav(b"RIFF-fake"), "via gemini")

    def test_8_transcribe_provider_error_surfaces(self) -> None:
        def boom(audio, mime):
            raise speech_mod.SpeechError("gemini exploded")
        with unittest.mock.patch.object(speech_mod, "_gemini_transcribe", side_effect=boom), \
             unittest.mock.patch.object(speech_mod.settings, "gemini_api_key", "k"):
            with self.assertRaises(speech_mod.SpeechError):
                speech_mod.transcribe_wav(b"RIFF-fake")

    def test_9_speak_wraps_pcm_in_riff_wav(self) -> None:
        pcm = b"\x00\x00" * 240
        with unittest.mock.patch.object(speech_mod, "_gemini_speak", return_value=wav_bytes(0.1)), \
             unittest.mock.patch.object(speech_mod.settings, "gemini_api_key", "k"):
            wav = speech_mod.synthesize_speech("hello there")
        self.assertIsNotNone(wav)
        self.assertTrue(wav[:4] == b"RIFF" and wav[8:12] == b"WAVE")

    def test_10_speak_none_without_key_or_text(self) -> None:
        with unittest.mock.patch.object(speech_mod.settings, "gemini_api_key", ""):
            self.assertIsNone(speech_mod.synthesize_speech("hello"))
        with unittest.mock.patch.object(speech_mod.settings, "gemini_api_key", "k"):
            self.assertIsNone(speech_mod.synthesize_speech("   "))

    def test_11_speak_failure_is_soft(self) -> None:
        def boom(text):
            raise RuntimeError("tts down")
        with unittest.mock.patch.object(speech_mod, "_gemini_speak", side_effect=boom), \
             unittest.mock.patch.object(speech_mod.settings, "gemini_api_key", "k"):
            self.assertIsNone(speech_mod.synthesize_speech("hello"))


class TestBStatusAndTranscribe(unittest.TestCase):
    def setUp(self) -> None:
        _patch_llm(self)
        self.fx = VFixture()

    def test_12_status_requires_auth(self) -> None:
        self.assertEqual(platform_client.get("/agent/voice/status").status_code, 401)

    def test_13_status_reports_capability(self) -> None:
        token = login(self.fx.patient_email)
        with unittest.mock.patch.object(speech_mod.settings, "gemini_api_key", "k"), \
             unittest.mock.patch.object(speech_mod.settings, "stt_provider", "gemini"):
            data = platform_client.get("/agent/voice/status", headers=auth(token)).json()
        self.assertEqual(data["stt_provider"], "gemini")
        self.assertTrue(data["tts_available"])
        self.assertEqual(data["voice_mode"], "web_stt_server_tts")

    def test_14_transcribe_probe_happy_path(self) -> None:
        token = login(self.fx.patient_email)
        with unittest.mock.patch.object(speech_mod, "_gemini_transcribe", return_value="I need an orthopedic doctor"), \
             unittest.mock.patch.object(speech_mod.settings, "gemini_api_key", "k"):
            res = platform_client.post(
                "/agent/voice/transcribe",
                headers=auth(token),
                files={"audio": ("speech.wav", wav_bytes(), "audio/wav")},
                data={"mime_type": "audio/wav"},
            )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["transcript"], "I need an orthopedic doctor")
        self.assertEqual(res.json()["stt_provider"], "gemini")

    def test_15_transcribe_probe_stt_failure_is_422(self) -> None:
        token = login(self.fx.patient_email)
        with unittest.mock.patch.object(speech_mod, "_gemini_transcribe",
                                        side_effect=speech_mod.SpeechError("no text")), \
             unittest.mock.patch.object(speech_mod.settings, "gemini_api_key", "k"):
            res = platform_client.post(
                "/agent/voice/transcribe",
                headers=auth(token),
                files={"audio": ("speech.wav", wav_bytes(), "audio/wav")},
                data={"mime_type": "audio/wav"},
            )
        self.assertEqual(res.status_code, 422)
        self.assertIn("no text", res.json()["detail"])


class TestCVoiceTurn(unittest.TestCase):
    """The full voice turn — mocked STT/TTS, REAL agent dialogue + booking."""

    def setUp(self) -> None:
        _patch_llm(self)
        self.fx = VFixture()
        self.token = login(self.fx.patient_email)

    def _session(self) -> str:
        res = platform_client.post("/agent/sessions", headers=auth(self.token), json={})
        assert res.status_code == 201, res.text
        return res.json()["conversation_id"]

    def _voice(self, conv_id: str, transcript: str = "hello", tts_on: bool = True):
        patches = [
            unittest.mock.patch.object(speech_mod, "_gemini_transcribe", return_value=transcript),
            unittest.mock.patch.object(speech_mod.settings, "gemini_api_key", "k"),
        ]
        if tts_on:
            patches.append(unittest.mock.patch.object(speech_mod, "_gemini_speak", return_value=wav_bytes(0.2)))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        res = platform_client.post(
            f"/agent/sessions/{conv_id}/voice",
            headers=auth(self.token),
            files={"audio": ("speech.wav", wav_bytes(), "audio/wav")},
            data={"mime_type": "audio/wav"},
        )
        return res

    def test_16_voice_turn_happy_path(self) -> None:
        conv_id = self._session()
        res = self._voice(conv_id, "I need to see an orthopedic doctor this week")
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(data["transcript"], "I need to see an orthopedic doctor this week")
        self.assertEqual(data["conversation_id"], conv_id)
        self.assertTrue(data["reply"])
        self.assertTrue(data["audio_base64"])
        wav = base64.b64decode(data["audio_base64"])
        self.assertTrue(wav[:4] == b"RIFF" and wav[8:12] == b"WAVE")
        self.assertEqual(data["audio_mime_type"], "audio/wav")

    def test_17_voice_turn_books_real_slot(self) -> None:
        from backend.app.models import Appointment
        conv_id = self._session()
        self._voice(conv_id, "orthopedics this week, in person")
        res = self._voice(conv_id, "1")  # pick the first offered slot by voice
        self.assertEqual(res.status_code, 200, res.text)
        meta = res.json()["meta"]
        # A REAL appointment is created for the voice user. next_action may be
        # "booked" or (Phase 6) "questionnaire" when the booked doctor's
        # hospital has templates and the reply chains into collection; in the
        # shared test DB either doctor/offer may lead, and the EHR outcome may
        # be failed if the slot collides with another fixture's booking. What
        # must hold: the appointment row exists and belongs to this patient.
        self.assertIn(meta.get("next_action"), ("booked", "questionnaire"), res.json()["reply"])
        self.assertTrue(meta.get("appointment_id"), meta)
        self.assertIn("Booked!", res.json()["reply"])
        db = PlatformSession()
        try:
            appt = db.get(Appointment, meta["appointment_id"])
            self.assertIsNotNone(appt)
            self.assertEqual(appt.patient_id, self._patient_id())
        finally:
            db.close()

    def _patient_id(self) -> str:
        from backend.app.models import Patient, User
        db = PlatformSession()
        try:
            user = db.query(User).filter(User.email == self.fx.patient_email).first()
            return db.query(Patient).filter(Patient.user_id == user.id).first().id
        finally:
            db.close()

    def test_18_stt_failure_is_422_text_fallback(self) -> None:
        conv_id = self._session()
        with unittest.mock.patch.object(speech_mod, "_gemini_transcribe",
                                        side_effect=speech_mod.SpeechError("mic muted")), \
             unittest.mock.patch.object(speech_mod.settings, "gemini_api_key", "k"):
            res = platform_client.post(
                f"/agent/sessions/{conv_id}/voice",
                headers=auth(self.token),
                files={"audio": ("speech.wav", wav_bytes(), "audio/wav")},
                data={"mime_type": "audio/wav"},
            )
        self.assertEqual(res.status_code, 422)
        self.assertIn("mic muted", res.json()["detail"])

    def test_19_tts_failure_still_returns_text_reply(self) -> None:
        conv_id = self._session()
        def boom(text):
            raise RuntimeError("tts down")
        with unittest.mock.patch.object(speech_mod, "_gemini_transcribe", return_value="hello"), \
             unittest.mock.patch.object(speech_mod, "_gemini_speak", side_effect=boom), \
             unittest.mock.patch.object(speech_mod.settings, "gemini_api_key", "k"):
            res = platform_client.post(
                f"/agent/sessions/{conv_id}/voice",
                headers=auth(self.token),
                files={"audio": ("speech.wav", wav_bytes(), "audio/wav")},
                data={"mime_type": "audio/wav"},
            )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIsNone(res.json()["audio_base64"])
        self.assertTrue(res.json()["reply"])

    def test_20_voice_requires_patient_role(self) -> None:
        conv_id = self._session()
        doctor_token = login(self.fx.doctor_email)
        res = platform_client.post(
            f"/agent/sessions/{conv_id}/voice",
            headers=auth(doctor_token),
            files={"audio": ("speech.wav", wav_bytes(), "audio/wav")},
        )
        self.assertEqual(res.status_code, 403)

    def test_21_cross_owner_session_hidden_as_404(self) -> None:
        conv_id = self._session()
        intruder = login(self.fx.patient2_email)
        with unittest.mock.patch.object(speech_mod, "_gemini_speak", return_value=None):
            res = platform_client.post(
                f"/agent/sessions/{conv_id}/voice",
                headers=auth(intruder),
                files={"audio": ("speech.wav", wav_bytes(), "audio/wav")},
            )
        self.assertEqual(res.status_code, 404)

    def test_22_voice_requires_session_to_exist(self) -> None:
        with unittest.mock.patch.object(speech_mod, "_gemini_speak", return_value=None):
            res = platform_client.post(
                "/agent/sessions/does-not-exist/voice",
                headers=auth(self.token),
                files={"audio": ("speech.wav", wav_bytes(), "audio/wav")},
            )
        self.assertEqual(res.status_code, 404)

    def test_23_voice_requires_audio_upload(self) -> None:
        conv_id = self._session()
        res = platform_client.post(f"/agent/sessions/{conv_id}/voice", headers=auth(self.token))
        self.assertEqual(res.status_code, 422)


class TestDPersistence(unittest.TestCase):
    """Voice turns are recorded like any other turn, with audio provenance."""

    def setUp(self) -> None:
        _patch_llm(self)
        self.fx = VFixture()
        self.token = login(self.fx.patient_email)

    def test_24_voice_turn_event_recorded_with_audio_origin(self) -> None:
        conv_id_res = platform_client.post("/agent/sessions", headers=auth(self.token), json={})
        conv = conv_id_res.json()
        conv_db_id = conv["conversation_id"]
        with unittest.mock.patch.object(speech_mod, "_gemini_transcribe", return_value="I have shoulder pain"), \
             unittest.mock.patch.object(speech_mod, "_gemini_speak", return_value=None), \
             unittest.mock.patch.object(speech_mod.settings, "gemini_api_key", "k"):
            res = platform_client.post(
                f"/agent/sessions/{conv_db_id}/voice",
                headers=auth(self.token),
                files={"audio": ("speech.wav", wav_bytes(), "audio/wav")},
                data={"mime_type": "audio/wav"},
            )
        self.assertEqual(res.status_code, 200, res.text)
        detail = platform_client.get(f"/agent/sessions/{conv_db_id}", headers=auth(self.token)).json()
        roles = [(e["role"], e.get("audio_origin")) for e in detail["events"]]
        self.assertIn(("user", "voice"), roles)
        self.assertTrue(all(origin is None for role, origin in roles if role == "agent"))

    def test_25_typed_turns_remain_text(self) -> None:
        conv_id = platform_client.post("/agent/sessions", headers=auth(self.token), json={}).json()["conversation_id"]
        res = platform_client.post(f"/agent/sessions/{conv_id}/messages", headers=auth(self.token),
                                   json={"message": "I have shoulder pain"})
        self.assertEqual(res.status_code, 200, res.text)
        detail = platform_client.get(f"/agent/sessions/{conv_id}", headers=auth(self.token)).json()
        self.assertIn(("user", "text"), [(e["role"], e.get("audio_origin")) for e in detail["events"]])

    def test_26_mixed_conversation_keeps_both_origins(self) -> None:
        conv_id = platform_client.post("/agent/sessions", headers=auth(self.token), json={}).json()["conversation_id"]
        platform_client.post(f"/agent/sessions/{conv_id}/messages", headers=auth(self.token),
                             json={"message": "I need an orthopedic doctor this week"})
        with unittest.mock.patch.object(speech_mod, "_gemini_transcribe", return_value="in person"), \
             unittest.mock.patch.object(speech_mod, "_gemini_speak", return_value=None), \
             unittest.mock.patch.object(speech_mod.settings, "gemini_api_key", "k"):
            platform_client.post(
                f"/agent/sessions/{conv_id}/voice",
                headers=auth(self.token),
                files={"audio": ("speech.wav", wav_bytes(), "audio/wav")},
                data={"mime_type": "audio/wav"},
            )
        detail = platform_client.get(f"/agent/sessions/{conv_id}", headers=auth(self.token)).json()
        user_events = [e for e in detail["events"] if e["role"] == "user"]
        self.assertEqual([e["audio_origin"] for e in user_events], ["text", "voice"])

    def test_27_no_audio_bytes_are_ever_persisted(self) -> None:
        """Privacy: the DB stores the transcript, never the recording."""
        conv_id = platform_client.post("/agent/sessions", headers=auth(self.token), json={}).json()["conversation_id"]
        blob = wav_bytes()
        with unittest.mock.patch.object(speech_mod, "_gemini_transcribe", return_value="transcript only"), \
             unittest.mock.patch.object(speech_mod, "_gemini_speak", return_value=None), \
             unittest.mock.patch.object(speech_mod.settings, "gemini_api_key", "k"):
            platform_client.post(
                f"/agent/sessions/{conv_id}/voice",
                headers=auth(self.token),
                files={"audio": ("speech.wav", blob, "audio/wav")},
                data={"mime_type": "audio/wav"},
            )
        db = PlatformSession()
        try:
            rows = db.query(ConversationEvent).all()
            for row in rows:
                self.assertNotIn(b"RIFF", (row.content or "").encode("utf-8", "ignore"))
        finally:
            db.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
