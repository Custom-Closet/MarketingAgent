import io
import time
from dataclasses import replace
from datetime import timedelta

import pytest

from marketing_agent import daily, pipeline
from marketing_agent.channels.gmail import ThreadStatus
from marketing_agent.config import load_settings
from marketing_agent.db import Database
from marketing_agent.personalize import DMDraft, EmailDraft
from marketing_agent.storage import LockBusy, open_database


class S3Error(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    """Just enough of boto3's S3 client for storage.py."""

    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, Bucket, Key, Body, IfNoneMatch=None):
        if IfNoneMatch == "*" and (Bucket, Key) in self.objects:
            raise S3Error("PreconditionFailed")
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise S3Error("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)

    def download_file(self, Bucket, Key, Filename):
        if (Bucket, Key) not in self.objects:
            raise S3Error("404")
        with open(Filename, "wb") as fh:
            fh.write(self.objects[(Bucket, Key)])

    def upload_file(self, Filename, Bucket, Key):
        with open(Filename, "rb") as fh:
            self.objects[(Bucket, Key)] = fh.read()


@pytest.fixture
def settings(tmp_path):
    return replace(load_settings(), sender_name="Ada", sender_email="ada@brand.co", digest_email="team@brand.co",
                   physical_address="1 Main St", daily_email_cap=10, min_delay_seconds=0, max_delay_seconds=0,
                   db_path=tmp_path / "agent.db", db_s3_uri="")


def test_s3_database_round_trip_and_lock(settings, tmp_path):
    s3 = FakeS3()
    shared = replace(settings, db_s3_uri="s3://bucket/agent.db")
    with open_database(shared, owner="lambda", s3=s3) as db:
        db.upsert_lead(email="a@acme.com", source="test")
        # A second process can't open the database while it's locked.
        with pytest.raises(LockBusy, match="lambda"):
            with open_database(replace(shared, db_path=tmp_path / "other.db"), s3=s3):
                pass
    assert ("bucket", "agent.db.lock") not in s3.objects

    # Another machine sees the uploaded data.
    with open_database(replace(shared, db_path=tmp_path / "laptop.db"), s3=s3) as db:
        assert db.counts()["leads"] == {"new": 1}


def test_s3_upload_happens_even_when_command_fails(settings):
    s3 = FakeS3()
    shared = replace(settings, db_s3_uri="s3://bucket/agent.db")
    with pytest.raises(RuntimeError):
        with open_database(shared, s3=s3) as db:
            db.upsert_lead(email="sent@acme.com", source="test")
            raise RuntimeError("crash after sending")
    assert ("bucket", "agent.db") in s3.objects
    assert ("bucket", "agent.db.lock") not in s3.objects


def test_stale_lock_is_taken_over(settings):
    s3 = FakeS3()
    shared = replace(settings, db_s3_uri="s3://bucket/agent.db")
    with open_database(shared, s3=s3, lock_ttl=timedelta(seconds=-1)):
        with open_database(shared, s3=s3):  # the first lock is already expired
            pass


class FakeGmail:
    def __init__(self):
        self.sent = []

    def send(self, msg, *, thread_id=None):
        self.sent.append(msg)
        return thread_id or f"t{len(self.sent)}", msg["Message-ID"]

    def thread_status(self, thread_id):
        return ThreadStatus(replied=thread_id == "t-old", reply_snippet="Yes, send the catalog")

    def bounced_recipients(self, newer_than_days=14):
        return []


class FakeWriter:
    def draft_email(self, lead):
        return EmailDraft(subject=f"Idea for {lead['company']}", body="Hi")

    def draft_followup(self, lead, history):
        return EmailDraft(subject="Re: Idea", body="Following up")

    def draft_dm(self, lead):
        return DMDraft(message="Hey!")


def test_send_fits_time_budget(settings, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(pipeline.time, "monotonic", lambda: clock[0])

    def sleep(seconds):
        delays.append(seconds)
        clock[0] += seconds

    db = Database(":memory:")
    for i in range(5):
        lead = db.upsert_lead(email=f"p{i}@acme.com", source="test")
        db.update_message(db.add_message(lead, "email", "Hi", subject="Hello"), status="approved")
    slow = replace(settings, min_delay_seconds=100, max_delay_seconds=100)
    delays = []
    sent = pipeline.send_approved_emails(db, FakeGmail(), slow, deadline=clock[0] + 40,
                                         sleep=sleep, log=lambda m: None)
    assert sent == 5
    assert len(delays) == 4 and sum(delays) <= 40  # shrunk from 100s each to fit the 40s budget


def test_daily_run_end_to_end(settings):
    db = Database(":memory:")
    # A lead waiting on a reply, an approved email, and a fresh lead to draft for.
    old = db.upsert_lead(email="old@acme.com", source="test")
    mid = db.add_message(old, "email", "Hi", subject="Hello")
    db.mark_sent(mid, thread_id="t-old", rfc_message_id="<x@brand.co>")
    ready = db.upsert_lead(email="ready@acme.com", source="test")
    db.update_message(db.add_message(ready, "email", "Hi", subject="Hello"), status="approved")
    db.upsert_lead(email="new@acme.com", company="Acme", source="test")

    gmail = FakeGmail()
    report = daily.run(settings, db, deadline=time.monotonic() + 60, gmail=gmail, writer=FakeWriter())

    assert report.errors == []
    assert report.stats["sync"]["replied"] == 1
    assert report.stats["sent"] == 1
    assert report.stats["email_drafts"] == 1
    assert report.replies == [("old@acme.com", "Yes, send the catalog")]
    digest = gmail.sent[-1]
    assert digest["To"] == "team@brand.co"
    assert "1 drafts to review, 1 new replies" in digest["Subject"]
    assert "old@acme.com: Yes, send the catalog" in digest.get_content()


def test_daily_run_isolates_failing_step(settings):
    class BrokenWriter(FakeWriter):
        def draft_email(self, lead):
            raise RuntimeError("Claude down")

    db = Database(":memory:")
    db.upsert_lead(email="new@acme.com", source="test")
    gmail = FakeGmail()
    report = daily.run(settings, db, deadline=time.monotonic() + 60, gmail=gmail, writer=BrokenWriter())
    # Per-lead failures are logged, not fatal; the digest still goes out.
    assert report.stats["email_drafts"] == 0
    assert "digest" in report.stats


def test_targets_queue(settings):
    db = Database(":memory:")
    assert db.add_target("domain", "Acme.com")
    assert not db.add_target("domain", "acme.com")

    class FakeHunter:
        def domain_search(self, domain, **kw):
            from marketing_agent.sources.hunter import HunterContact
            return [HunterContact("jo@acme.com", "Jo", "Li", "CMO", 90, "Acme", domain)]

        def verify(self, email):
            return "valid"

    n = pipeline.process_targets(db, settings, limit=5, hunter=FakeHunter(), ig=None, log=lambda m: None)
    assert n == 1
    assert db.targets()[0]["status"] == "done"
    assert db.leads_without_draft("email", 5)[0]["email"] == "jo@acme.com"
