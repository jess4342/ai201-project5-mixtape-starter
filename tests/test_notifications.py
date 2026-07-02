"""
tests/test_notifications.py — Mixtape

Regression tests for notification behavior.

These target Issue #4 ("I got notified when a friend added my song to a
playlist but not when they rated it"), which previously had NO test coverage.
Against the buggy code — where rate_song() persisted a Rating but never called
create_notification() — test_rating_notifies_song_sharer would fail because the
sharer's notification list stays empty.
"""

import pytest
from app import create_app, db
from models import User, Song
from services.notification_service import rate_song, get_notifications


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def seed(app):
    """A sharer who shared a song, and a separate user who will rate it."""
    with app.app_context():
        sharer = User(username="sharer", email="sharer@example.com")
        rater = User(username="rater", email="rater@example.com")
        db.session.add_all([sharer, rater])
        db.session.flush()

        song = Song(title="Shared Song", artist="Some Artist", shared_by=sharer.id)
        db.session.add(song)
        db.session.commit()
        yield {"sharer": sharer, "rater": rater, "song": song}


def test_rating_notifies_song_sharer(app, seed):
    """
    Rating another user's song must notify the original sharer.

    This is the regression guard for Issue #4: it fails against the buggy
    rate_song() (0 notifications) and passes after the notification is added.
    """
    with app.app_context():
        sharer_id = seed["sharer"].id
        rate_song(seed["rater"].id, seed["song"].id, 5)

        notifs = get_notifications(sharer_id)
        assert len(notifs) == 1
        assert notifs[0]["type"] == "song_rated"


def test_rating_own_song_does_not_notify(app, seed):
    """A user rating their own song should not generate a self-notification."""
    with app.app_context():
        sharer_id = seed["sharer"].id
        rate_song(sharer_id, seed["song"].id, 4)
        assert get_notifications(sharer_id) == []
