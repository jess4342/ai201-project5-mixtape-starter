# Project 5: Mixtape Bug Hunt — Submission

## AI Usage

I used Claude Code as a pair-debugging partner throughout this project. Specifically:

- **Tracing call chains.** I asked it to walk each feature from route → service so I understood where logic actually lived (e.g., `POST /songs/<id>/rate` → `routes/songs.py` → `notification_service.rate_song()`). This confirmed the README's claim that all business logic lives in `services/` and routes only parse input and format responses.
- **Reproducing bugs before reading the fix.** Rather than trusting the issue titles, I had it help me write small scripts that drove the service functions directly against an in-memory SQLite DB, and I ran the existing `pytest` suite to see which failures were real.
- **Explaining stdlib behavior I was unsure about.** For the streak bug I asked what `datetime.weekday()` returns for each day (Monday = 0 … Sunday = 6), which made the `!= 6` comparison obviously wrong.

**Where I had to verify myself / where AI was incomplete:** My initial assumption (shared by the AI's first read of `search_service.py`) was that the `outerjoin` on `song_tags` would obviously produce duplicate rows for a multi-tag song, so the search bug would reproduce immediately. When I actually ran `search_songs("Crown Heights")` against a 3-tag song, it returned **1** result, not 3 — the test `test_search_no_duplicates_multi_tag_song` **passes** as-is. I had to dig in myself to learn that SQLAlchemy's legacy `Session.query(Entity)` **automatically de-duplicates ORM entity instances**, which masks the buggy join. I confirmed the mechanism by running the same join selecting `Song.id` (a raw column) instead of the entity: that returned **3 rows**. So the "duplicate in search" defect is real and latent in the code, but it does not surface through the current entity-returning code path. That nuance was something the AI's surface-level reading got wrong until I tested it.

---

## Codebase Map

Mixtape is a small Flask + SQLAlchemy JSON API. The architecture is strictly layered: **routes parse/format, services hold all logic, models define data.**

### Main files

- **`app.py`** — Flask app factory (`create_app`) and the shared `db = SQLAlchemy()` instance. Registers the four route blueprints and creates tables.
- **`models.py`** — Six SQLAlchemy entities plus three association tables:
  - `User` — has `listening_streak` and `last_listened_at`; `friends` is a self-referential many-to-many via the `friendships` table (stored bidirectionally by the seeder).
  - `Song` — shared by a user (`shared_by`); tags are many-to-many via `song_tags`.
  - `Tag`, `Rating` (unique per user+song, score 1–5), `ListeningEvent` (one row per listen, timestamped), `Notification` (per-recipient, typed, read flag).
  - `Playlist` — songs are many-to-many via the **`playlist_entries`** association table, which carries an explicit **`position`** integer, `added_by`, and `added_at`. Playlist order is by `position`, not insertion order.
- **`routes/`** — Thin blueprints: `songs.py` (search / detail / rate / listen), `playlists.py` (create / detail / list songs / add song), `users.py` (profile / streak / notifications), `feed.py` (listening-now / activity).
- **`services/`** — Where every bug lives:
  - `streak_service.py` — `record_listening_event()` + `update_listening_streak()` day-boundary math.
  - `feed_service.py` — `get_friends_listening_now()` (recency-filtered) and `get_activity_feed()` (last-N, unfiltered).
  - `search_service.py` — `search_songs()` case-insensitive title/artist match.
  - `notification_service.py` — `add_to_playlist()`, `rate_song()`, and notification CRUD.
  - `playlist_service.py` — `get_playlist_songs()` position-ordered retrieval.
- **`seed_data.py`** — Rebuilds the DB with 5 users, 25 songs (deliberately with 0/1/3+ tags to expose the search bug), 3 playlists, and both recent (~10 min) and old (2h–14d) listening events. The comments here document the *intended* behavior (e.g., only listens "within the past 30 minutes" should appear in listening-now), which is how I inferred correct thresholds.
- **`tests/`** — `test_streaks.py`, `test_search.py`, `test_playlists.py`. Several tests are written to fail against the current buggy code.

### Data flow — rating a song (and why Issue #4 is visible)

`POST /songs/<song_id>/rate` (`routes/songs.py`) parses `user_id` + `score`, then calls `notification_service.rate_song()`. That function validates the score range, upserts a `Rating` row (respecting the `unique_user_song_rating` constraint), and commits. The parallel function `add_to_playlist()` in the same file does one extra thing: after mutating state it calls `create_notification(user_id=song.shared_by, ...)` to alert the song's original sharer. `rate_song()` **does not** — that asymmetry is Issue #4.

### Patterns I noticed

1. **Route → service delegation is total.** Every route function's body is: parse JSON/args → `try` a single service call → `jsonify`. No business logic leaks into routes.
2. **Services are the only place `db.session` is used** (routes only touch `db.session.get` for the trivial user lookup in `users.py`). So bugs are reliably in `services/`, as the README promised.
3. **Timestamps are always UTC-aware on write** (`datetime.now(timezone.utc)`), but SQLite stores them naive — so the streak code has to defensively re-attach `tzinfo` before comparing (`last_listened.replace(tzinfo=timezone.utc)`).
4. **Association tables carry data** (`playlist_entries.position`, `friendships` symmetric rows), so features depend on querying the join table directly, not just relationship collections.

---

## Root Cause Analysis

I investigated all five issues. Reproduction was done by (a) running `pytest tests/` and (b) driving the service functions directly against an in-memory SQLite DB.

### Issue #1 — My listening streak keeps resetting

**How I reproduced it.** Ran `pytest tests/test_streaks.py`; `test_streak_increments_on_sunday` fails with `assert 1 == 2`. To confirm outside the test, I called `update_listening_streak(user, saturday)` then `update_listening_streak(user, sunday)` for `2024-06-15` (Sat) and `2024-06-16` (Sun): the streak went `1 → 1` instead of `1 → 2`. The trigger condition is precise: **any consecutive-day listen where the second day is a Sunday.** Weekday listens work fine, which is why the bug looked intermittent to users.

**How I found the root cause.** README pointed me to `streak_service.py`. The only branching on the day-gap is in `update_listening_streak()`. The increment branch reads `elif days_since_last == 1 and today.weekday() != 6:`. I asked what `weekday()` returns and confirmed Sunday = 6. That was the moment — the extra `and today.weekday() != 6` clause has no business rule behind it; a consecutive day is a consecutive day regardless of which day it is.

**The root cause.** `datetime.weekday()` returns 6 for Sunday. The increment condition required `days_since_last == 1 AND today is not Sunday`. So when a user listened on a Saturday and again on Sunday, `days_since_last == 1` was true but `today.weekday() != 6` was false, so control fell through to the `else` branch that **resets the streak to 1**. Every streak that crossed into a Sunday was silently reset mid-week.

**Fix and side-effect check.** Removed the spurious weekday clause so the branch is simply `elif days_since_last == 1:` (commit `43115b6`). This restores the documented rule ("listened yesterday → increment"). **Verified:** after the fix `pytest tests/` reports **13 passed** — `test_streak_increments_on_sunday` now passes, and the specific behaviors that could plausibly break with a change to this branch still hold: new-user streak = 1, same-day listen does not double-count, and a skipped day still resets to 1. None of those depend on the weekday, so removing the guard could only affect the Sunday case, which is exactly the one that was broken.

### Issue #2 — Friends Listening Now shows people from yesterday

**How I reproduced it.** Created two friends `a` and `b`, inserted one `ListeningEvent` for `b` timestamped **20 hours ago**, and called `get_friends_listening_now(a.id)`. `b` appeared in the feed. Twenty hours ago is "yesterday," yet it's surfaced as listening *now*. The seed data's own comment says only listens "within the past 30 minutes" should count, which is the intended contract.

**How I found the root cause.** `feed_service.py` computes `cutoff = now - RECENT_THRESHOLD` and filters `ListeningEvent.listened_at >= cutoff`. The filter logic is correct; the constant is not. `RECENT_THRESHOLD = timedelta(hours=24)`. A 24-hour window by definition includes all of yesterday.

**The root cause.** The recency window for a "listening *now*" feed was set to a full day. Any friend who listened at any point in the previous 24 hours matches `listened_at >= now - 24h` and is reported as currently listening.

**Fix and side-effect check.** Narrowed `RECENT_THRESHOLD` to `timedelta(minutes=30)`, matching the seeder's "past 30 minutes" comment (commit `8e2a179`). The query structure, dedup-per-friend logic, and ordering are unchanged. **Verified with a controlled script:** I inserted two events for the same friend — one 20 h old, one 10 min old — and confirmed `get_friends_listening_now()` now returns **1** (the recent one; the 20 h event is correctly dropped). Critically, I checked the code path most likely to be collateral damage: `get_activity_feed()`, which shares the friend-lookup logic but is supposed to ignore recency. It still returns **2** (both events), confirming the threshold change only touched the "listening now" window and did not leak into the unfiltered activity feed.

### Issue #3 — The same song keeps showing up twice in search

**How I reproduced it (two conditions, and an honest caveat).** The duplicate requires **two** conditions together, which is why it looked intermittent. (A) *Data condition:* the matched song must have **≥ 2 tags** — the `outerjoin(song_tags)` emits one row per tag, so a 0- or 1-tag song produces ≤ 1 row and can never duplicate; a 2+ tag song produces 2+ rows. (B) *Consumption condition:* those rows only reach the caller if the result set is read **without ORM entity uniquing.** I tested the matrix against a 3-tag song that matches the query:

| Consumption path | Rows returned |
|---|:---:|
| `db.session.query(Song)...all()` (**current code**) | **1** |
| `db.session.query(Song)...all()`, query matches title **and** artist | **1** |
| `db.session.execute(select(Song)...).scalars().all()` (2.0 idiom, no `.unique()`) | **3** |
| `...scalars().unique().all()` | **1** |

So the honest caveat: through the **current** code path this returns **1**, and `test_search_no_duplicates_multi_tag_song` **passes unchanged** — the test author assumed the join would produce 3, but it doesn't here. The defect is real but *latent*.

**How I found the root cause.** `search_songs()` does `db.session.query(Song).outerjoin(song_tags, ...)`. I first assumed this obviously duplicated, then reproduced and got 1, which forced me to explain the gap. Selecting a raw column (`Song.id`) over the same join returned **3 rows**, proving the join *does* fan out — so something between SQL and the return value was collapsing it. That something is SQLAlchemy's **legacy `Session.query(Entity)`, which auto-de-duplicates identical ORM entity instances by primary key** before `.all()` returns. The `select().scalars().all()` experiment (3 rows) confirmed the fan-out is only suppressed by that uniquing, not by anything in the code.

**The root cause.** The `outerjoin` on `song_tags` serves no purpose — nothing in the `WHERE` or `SELECT` references it, and tags are already loaded lazily by `to_dict()` via the `lazy="subquery"` relationship. Its only effect is to multiply rows by tag count. Visible correctness depends entirely on legacy-`Query` uniquing; because `requirements.txt` pins `sqlalchemy>=2.0`, a routine migration to the 2.0 `select()` idiom (which does **not** auto-`unique()`) would resurface N duplicates for every 2+ tag song.

**Fix and side-effect check.** Removed the `outerjoin(song_tags, ...)` line entirely and dropped the now-unused `Tag`/`song_tags` imports — the title/artist `ilike` filter only needs the `Song` table (commit `0ce84c9`). This makes correctness independent of the uniquing behavior. **Verified:** `pytest tests/` reports **13 passed**, including all five search tests — the no-tag song (`Midnight Drive`), single-tag song (`Block Party`), and multi-tag song (`Crown Heights Anthem`) each appear exactly once, and the empty-result case (`zzz_no_match_zzz → []`) is unchanged. Tags still render in each result dict, confirming the relationship load was never dependent on the join.

### Issue #4 — Notified when a friend added my song to a playlist, but not when they rated it

**How I reproduced it.** User `b` rated user `a`'s song (`rate_song(b.id, song.id, 5)`), then I read `get_notifications(a.id)`: **0 notifications** before and after. For contrast, `add_to_playlist()` on the same song *does* create a `song_added_to_playlist` notification for `a`. So the two interaction paths behave inconsistently, exactly as the issue reports.

**How I found the root cause.** Both handlers live in `notification_service.py`. `add_to_playlist()` ends with a `create_notification(user_id=song.shared_by, notification_type="song_added_to_playlist", ...)` guarded by `if song.shared_by != added_by_user_id`. `rate_song()` validates, upserts the `Rating`, commits, and returns — with **no `create_notification` call anywhere.**

**The root cause.** The notification side effect was simply never implemented for rating. `rate_song()` persists the rating but omits the "notify the original sharer" step that its sibling `add_to_playlist()` performs.

**Fix and side-effect check.** After the rating is committed I added `create_notification(user_id=song.shared_by, notification_type="song_rated", body=f"{rater.username} rated your song '{song.title}' {score}/5.")`, guarded by `if song.shared_by != user_id` so users aren't notified for rating their own songs — mirroring `add_to_playlist`'s `song.shared_by != added_by_user_id` guard (commit `010507c`). **Verified with a controlled script:** a friend rating the sharer's song produces exactly **1** notification with the expected body; the guard works — a user rating **their own** song produces **0** new notifications. I specifically exercised the branch most likely to be affected: the **upsert path**, where re-rating an already-rated song updates the existing `Rating` rather than inserting. Re-rating still succeeds and (by design, matching `add_to_playlist` which notifies on every call) emits another notification, so the sharer is told about the score change. `pytest tests/` remains **13 passed**, confirming the added notification didn't disturb the rating persistence the tests cover.

### Issue #5 — The last song in a playlist never shows up

**How I reproduced it.** Ran `pytest tests/test_playlists.py`; `test_playlist_returns_all_songs` fails (got 4, expected 5) and `test_playlist_returns_songs_in_order` fails ("Right contains one more item: 'Track 5'"). Any playlist with N songs returns N−1, always dropping whichever song has the highest `position`.

**How I found the root cause.** `get_playlist_songs()` builds the correct position-ordered query, then returns `[song.to_dict() for song in songs[:-1]]`. The query is right; the slice is wrong. `songs[:-1]` discards the final element of the ordered list.

**The root cause.** The list comprehension slices off the last element with `[:-1]`. Because the query is ordered ascending by `position`, the dropped element is always the last song in the playlist. (The empty-playlist test still passes because `[][:-1]` is `[]`, which is why the bug went unnoticed for empty playlists.)

**Fix and side-effect check.** Dropped the `[:-1]` so the comprehension iterates the full ordered list — `[song.to_dict() for song in songs]` (commit `5bd1b55`). **Verified:** `pytest tests/` reports **13 passed** — `test_playlist_returns_all_songs` now returns 5, and `test_playlist_returns_songs_in_order` returns `["Track 1" … "Track 5"]`. I deliberately re-checked the empty-playlist edge case (`test_empty_playlist_returns_empty_list`), the one place a slice change could regress: the full-list comprehension over an empty query result is still `[]`, so no `IndexError` or spurious content, and it passes.

---

## Summary of reproduction results

| # | Issue | How triggered | Reproduced |
|---|-------|---------------|:----------:|
| 1 | Streak resets | Listen Sat then Sun → streak `1→1` not `1→2`; `pytest` fails | ✅ |
| 2 | Feed shows yesterday | Friend event 20h old appears in listening-now (24h window) | ✅ |
| 3 | Search duplicates | 2+ tag song via `select().scalars().all()` = 3 rows; current `query().all()` = 1 (masked by ORM uniquing) | ⚠️ latent |
| 4 | No rating notification | Rate another user's song → 0 notifications for the sharer | ✅ |
| 5 | Last playlist song missing | 5-song playlist returns 4; `pytest` fails | ✅ |

---

## Commit History

All five fixes live on the `bugfix/mixtape` branch as separate, single-purpose commits using conventional `fix:` prefixes:

```
5bd1b55 fix: stop dropping the last song in playlist retrieval
010507c fix: notify song sharer when their song is rated
0ce84c9 fix: remove pointless tag outerjoin that duplicates search results
8e2a179 fix: narrow Friends Listening Now window from 24h to 30min
43115b6 fix: increment listening streak on Sundays instead of resetting
2dfdeaa Add .gitignore file and update README with setup instructions   (pre-existing)
7b64551 initial commit                                                   (pre-existing)
```

Each commit touches exactly one service file. After all five, the full suite is green:

```
$ pytest tests/
13 passed
```
