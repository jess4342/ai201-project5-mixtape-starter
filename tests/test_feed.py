"""
tests/test_feed.py — Mixtape

Regression tests for the "Friends Listening Now" feed.

These target Issue #2 ("Friends Listening Now shows people from yesterday"),
which previously had NO test coverage. Against the buggy code — where
RECENT_THRESHOLD was timedelta(hours=24) — test_listening_now_excludes_stale_events
would fail because a listen from 20 hours ago still falls inside the 24h window
and is reported as "listening now".
"""

import pytest
from datetime import datetime, timedelta, timezone
from app import create_app, db
from models import User, Song, ListeningEvent, friendships


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def seed(app):
    """Two friends and a song; caller adds the listening events per test."""
    with app.app_context():
        me = User(username="me", email="me@example.com")
        friend = User(username="friend", email="friend@example.com")
        db.session.add_all([me, friend])
        db.session.flush()
        db.session.execute(friendships.insert().values(user_id=me.id, friend_id=friend.id))

        song = Song(title="Some Song", artist="Some Artist", shared_by=me.id)
        db.session.add(song)
        db.session.commit()
        yield {"me": me, "friend": friend, "song": song}


def test_listening_now_includes_a_recent_event(app, seed):
    """A friend who listened minutes ago should appear in the feed."""
    from services.feed_service import get_friends_listening_now
    with app.app_context():
        now = datetime.now(timezone.utc)
        db.session.add(ListeningEvent(
            user_id=seed["friend"].id, song_id=seed["song"].id,
            listened_at=now - timedelta(minutes=5),
        ))
        db.session.commit()
        feed = get_friends_listening_now(seed["me"].id)
        assert len(feed) == 1


def test_listening_now_excludes_stale_events(app, seed):
    """
    A friend whose only listen was 20 hours ago must NOT appear.

    Regression guard for Issue #2: fails against the 24h RECENT_THRESHOLD
    (20h is inside 24h) and passes with the narrowed 30-minute window.
    """
    from services.feed_service import get_friends_listening_now
    with app.app_context():
        now = datetime.now(timezone.utc)
        db.session.add(ListeningEvent(
            user_id=seed["friend"].id, song_id=seed["song"].id,
            listened_at=now - timedelta(hours=20),
        ))
        db.session.commit()
        feed = get_friends_listening_now(seed["me"].id)
        assert feed == []
