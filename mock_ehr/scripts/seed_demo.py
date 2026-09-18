"""Seed the Mock EHR with demo providers (and optionally a patient).

Run:  python -m mock_ehr.scripts.seed_demo

Idempotent: re-running does not duplicate rows. The platform connector
upserts its patients at sync time, so only providers are seeded here.
"""
from __future__ import annotations

from sqlalchemy import select

from mock_ehr.app.database import Base, SessionLocal, ensure_sqlite_dir, engine
from mock_ehr.app.models import Provider

PROVIDERS = [
    ("EHR-PROV-001", "Dr. Anil Rao", "orthopedics"),
    ("EHR-PROV-002", "Dr. Sneha Mehta", "cardiology"),
    ("EHR-PROV-003", "Dr. James Whitfield", "dermatology"),
]


def main() -> None:
    ensure_sqlite_dir()
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        for ext_id, name, specialty in PROVIDERS:
            existing = db.scalar(select(Provider).where(Provider.external_provider_id == ext_id))
            if existing:
                existing.full_name = name
                existing.specialty = specialty
                existing.is_active = True
                print(f"provider updated: {ext_id} ({name})")
            else:
                db.add(Provider(external_provider_id=ext_id, full_name=name, specialty=specialty))
                print(f"provider created: {ext_id} ({name})")
        db.commit()
        print(f"Mock EHR seed complete. Providers available: {len(PROVIDERS)}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
