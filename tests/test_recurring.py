"""End-to-end tests for recurring weekly reservations (连续周循环预约).

POST /points/{id}/recurring-reservations books one point's same local
wall-clock slot for 2..4 consecutive weeks as one atomic group; every weekly
visit is an ordinary, independently manageable reservation.
"""

from __future__ import annotations

import threading
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def iso(dt):
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def create_point(client, tz="UTC", capacity=1):
    r = client.post(
        "/points",
        json={"name": "循环预约测试点", "timezone": tz, "capacity": capacity},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def publish(client, pid, rules, expected=0):
    r = client.post(
        f"/points/{pid}/rules",
        json={"expected_version": expected, "rules": rules},
    )
    assert r.status_code == 201, r.text
    return r.json()["version"]


def all_day_weekly():
    """Every weekday 00:00-23:59 — covers any slot the tests pick."""
    return [
        {"weekday": w, "start_time": "00:00", "end_time": "23:59"}
        for w in range(1, 8)
    ]


def open_point(client, tz="UTC", capacity=1, weekly=None, closed=None, makeup=None):
    pid = create_point(client, tz=tz, capacity=capacity)
    publish(
        client,
        pid,
        {
            "weekly": all_day_weekly() if weekly is None else weekly,
            "closed_dates": closed or [],
            "makeup": makeup or [],
        },
    )
    return pid


def local_date_after(client_tz, days):
    return (datetime.now(UTC).astimezone(ZoneInfo(client_tz)) + timedelta(days=days)).date()


def book_recurring(client, pid, first_date, start_time="09:00", end_time="10:00",
                   weeks=3, key="recurring-1"):
    return client.post(
        f"/points/{pid}/recurring-reservations",
        json={
            "idempotency_key": key,
            "first_date": first_date.isoformat(),
            "start_time": start_time,
            "end_time": end_time,
            "weeks": weeks,
        },
    )


def reserve(client, pid, key, start, end):
    return client.post(
        f"/points/{pid}/reservations",
        json={"idempotency_key": key, "start": iso(start), "end": iso(end)},
    )


def day_at(day, hour, minute=0, tz=UTC):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_book_three_consecutive_weeks_returns_plan_and_ordinary_reservations(client):
    pid = open_point(client)
    first = local_date_after("UTC", 2)
    r = book_recurring(client, pid, first, weeks=3)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["plan_id"] > 0
    assert body["point_id"] == pid
    assert body["first_date"] == first.isoformat()
    assert body["start_time"] == "09:00"
    assert body["end_time"] == "10:00"
    assert body["weeks"] == 3
    assert len(body["visits"]) == 3

    expected_dates = [first + timedelta(weeks=i) for i in range(3)]
    ids, tokens = set(), set()
    for i, visit in enumerate(body["visits"]):
        assert visit["week"] == i + 1
        assert visit["local_date"] == expected_dates[i].isoformat()
        assert visit["start"] == iso(day_at(expected_dates[i], 9))
        assert visit["end"] == iso(day_at(expected_dates[i], 10))
        assert visit["rules_version"] == 1
        reservation = visit["reservation"]
        assert reservation["point_id"] == pid
        assert reservation["status"] == "active"
        assert reservation["revision"] == 1
        assert reservation["cancel_token"]
        ids.add(reservation["id"])
        tokens.add(reservation["cancel_token"])
    assert len(ids) == 3
    assert len(tokens) == 3


def test_four_weeks_is_the_maximum_accepted(client):
    pid = open_point(client)
    first = local_date_after("UTC", 1)
    r = book_recurring(client, pid, first, weeks=4, key="r4")
    assert r.status_code == 201, r.text
    assert len(r.json()["visits"]) == 4
    assert [v["week"] for v in r.json()["visits"]] == [1, 2, 3, 4]


def test_overnight_slot_end_before_start_books_into_next_local_day(client):
    # Shanghai is UTC+8 with no DST: 23:00 local -> next day 01:00 local is
    # exactly two hours; the visit crosses local midnight.  The point opens
    # 20:00 -> 02:00 on that weekday (every visit is the same weekday).
    first = local_date_after("Asia/Shanghai", 2)
    pid = open_point(
        client,
        tz="Asia/Shanghai",
        weekly=[
            {"weekday": first.isoweekday(), "start_time": "20:00",
             "end_time": "02:00"}
        ],
    )
    r = book_recurring(client, pid, first, "23:00", "01:00", weeks=2,
                       key="overnight")
    assert r.status_code == 201, r.text
    visits = r.json()["visits"]
    sh = ZoneInfo("Asia/Shanghai")
    for i, visit in enumerate(visits):
        day = first + timedelta(weeks=i)
        assert visit["start"] == iso(day_at(day, 23, tz=sh))
        assert visit["end"] == iso(day_at(day + timedelta(days=1), 1, tz=sh))
        assert visit["local_date"] == day.isoformat()


# ---------------------------------------------------------------------------
# Structural / policy validation
# ---------------------------------------------------------------------------

def test_weeks_outside_2_4_is_invalid_request(client):
    pid = open_point(client)
    first = local_date_after("UTC", 2)
    for bad in (1, 5):
        r = book_recurring(client, pid, first, weeks=bad, key=f"bad-{bad}")
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "invalid_request"


def test_malformed_clock_time_is_invalid_period(client):
    pid = open_point(client)
    first = local_date_after("UTC", 2)
    r = book_recurring(client, pid, first, start_time="9am", key="bad-clock")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_period"


def test_visit_longer_than_max_duration_fails_with_week_detail(client):
    pid = open_point(client)
    first = local_date_after("UTC", 2)
    # 20:00 -> 01:00 next day is five hours, over the 120-minute maximum.
    r = book_recurring(client, pid, first, "20:00", "01:00", weeks=2,
                       key="too-long")
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "invalid_period"
    assert err["details"]["week"] == 1
    assert err["details"]["local_date"] == first.isoformat()


def test_first_week_beyond_bookable_horizon_fails(client):
    pid = open_point(client)
    first = (datetime.now(UTC) + timedelta(days=36)).date()
    r = book_recurring(client, pid, first, weeks=2, key="too-far")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "invalid_period"
    assert err["details"]["week"] == 1


def test_point_without_published_rules_is_no_published_version(client):
    pid = create_point(client)
    first = local_date_after("UTC", 2)
    r = book_recurring(client, pid, first, weeks=2)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "no_published_version"


def test_unknown_point_is_404(client):
    first = local_date_after("UTC", 2)
    r = book_recurring(client, 999999, first, weeks=2)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "point_not_found"


# ---------------------------------------------------------------------------
# Per-week calendar / capacity: any failing week books nothing
# ---------------------------------------------------------------------------

def test_closed_week_aborts_whole_group_and_names_failing_week(client):
    first = local_date_after("UTC", 2)
    closed_week = first + timedelta(weeks=1)
    pid = open_point(client, closed=[closed_week.isoformat()])
    r = book_recurring(client, pid, first, weeks=3, key="closed-group")
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["code"] == "outside_open_hours"
    assert err["details"]["week"] == 2
    assert err["details"]["local_date"] == closed_week.isoformat()
    assert err["details"]["point_id"] == pid

    # Nothing was booked for any week: the week-1 slot is still free.
    follow = reserve(
        client, pid, "after-failure-w1",
        day_at(first, 9), day_at(first, 10),
    )
    assert follow.status_code == 201, follow.text


def test_capacity_conflict_on_one_week_aborts_whole_group(client):
    first = local_date_after("UTC", 2)
    busy_day = first + timedelta(weeks=2)  # week 3
    pid = open_point(client, capacity=1)
    # An existing visitor holds 09:30-10:30 on week 3: the planned 09:00-10:00
    # window overlaps it under half-open counting.
    taken = reserve(
        client, pid, "existing-visitor",
        day_at(busy_day, 9, 30), day_at(busy_day, 10, 30),
    )
    assert taken.status_code == 201, taken.text

    r = book_recurring(client, pid, first, weeks=3, key="full-group")
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["code"] == "reservation_overlap"
    assert err["details"]["week"] == 3
    assert err["details"]["local_date"] == busy_day.isoformat()
    assert err["details"]["capacity"] == 1
    assert err["details"]["peak_concurrency"] == 1
    assert taken.json()["id"] in err["details"]["conflicting_reservation_ids"]

    # Week 1 was not booked either: the slot is still available.
    follow = reserve(
        client, pid, "after-failure", day_at(first, 9), day_at(first, 10)
    )
    assert follow.status_code == 201, follow.text


def test_successful_group_really_occupies_every_week(client):
    pid = open_point(client, capacity=1)
    first = local_date_after("UTC", 2)
    booked = book_recurring(client, pid, first, weeks=2, key="occupy")
    assert booked.status_code == 201, booked.text
    # Capacity 1: an overlapping ordinary reservation fails on week 2 ...
    week2 = first + timedelta(weeks=1)
    clash = reserve(
        client, pid, "clash-week2",
        day_at(week2, 9, 30), day_at(week2, 10, 30),
    )
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "reservation_overlap"
    # ... while touching the end is still allowed.
    touch = reserve(
        client, pid, "touch-week2",
        day_at(week2, 10), day_at(week2, 11),
    )
    assert touch.status_code == 201, touch.text


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_same_key_same_content_replays_first_creation(client):
    pid = open_point(client)
    first = local_date_after("UTC", 2)
    first = book_recurring(client, pid, first, weeks=3).json()
    replay = book_recurring(
        client, pid, date.fromisoformat(first["first_date"]),
        weeks=3,
    )
    assert replay.status_code == 200
    assert replay.json() == first


def test_same_key_different_content_conflicts(client):
    pid = open_point(client)
    first = local_date_after("UTC", 2)
    assert book_recurring(client, pid, first, weeks=3, key="k").status_code == 201
    conflict = book_recurring(client, pid, first, weeks=2, key="k")
    assert conflict.status_code == 409
    err = conflict.json()["error"]
    assert err["code"] == "idempotency_conflict"
    assert err["details"]["weeks"] == 3

    other_time = book_recurring(
        client, pid, first, "11:00", "12:00", weeks=3, key="k"
    )
    assert other_time.status_code == 409
    other_day = book_recurring(
        client, pid, first + timedelta(days=1), weeks=3, key="k"
    )
    assert other_day.status_code == 409


def test_failed_group_does_not_consume_the_key(client):
    first = local_date_after("UTC", 2)
    closed_week = first + timedelta(weeks=1)
    pid = open_point(client, closed=[closed_week.isoformat()])
    failed = book_recurring(client, pid, first, weeks=3, key="retry-me")
    assert failed.status_code == 409
    # After the closure is lifted by republishing without it, the same key
    # succeeds: the failed attempt consumed nothing.
    publish(
        client,
        pid,
        {"weekly": all_day_weekly(), "closed_dates": [], "makeup": []},
        expected=1,
    )
    retry = book_recurring(client, pid, first, weeks=3, key="retry-me")
    assert retry.status_code == 201, retry.text
    assert len(retry.json()["visits"]) == 3


def test_recurring_key_namespace_is_independent_of_single_reservation(client):
    pid = open_point(client)
    first = local_date_after("UTC", 2)
    # Pick a different window for the ordinary reservation so they do not
    # overlap at capacity; the key itself is shared.
    single = reserve(
        client, pid, "shared-key",
        day_at(first, 14), day_at(first, 15),
    )
    assert single.status_code == 201, single.text
    group = book_recurring(client, pid, first, weeks=2, key="shared-key")
    assert group.status_code == 201, group.text


def test_same_key_on_another_point_is_independent(client):
    p1, p2 = open_point(client), open_point(client)
    first = local_date_after("UTC", 2)
    r1 = book_recurring(client, p1, first, weeks=2, key="same")
    r2 = book_recurring(client, p2, first, weeks=2, key="same")
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["plan_id"] != r2.json()["plan_id"]


# ---------------------------------------------------------------------------
# Every week is an ordinary, independently manageable reservation
# ---------------------------------------------------------------------------

def test_each_week_can_be_cancelled_independently_and_replay_shows_state(client):
    pid = open_point(client)
    first = local_date_after("UTC", 2)
    created = book_recurring(client, pid, first, weeks=3).json()
    week2 = created["visits"][1]
    rid, token = week2["reservation"]["id"], week2["reservation"]["cancel_token"]

    cancel = client.post(
        f"/points/{pid}/reservations/{rid}/cancel",
        json={"cancel_token": token},
    )
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["status"] == "cancelled"
    # Repeat cancel stays idempotent.
    again = client.post(
        f"/points/{pid}/reservations/{rid}/cancel",
        json={"cancel_token": token},
    )
    assert again.status_code == 200
    assert again.json()["status"] == "cancelled"

    replay = book_recurring(client, pid, first, weeks=3).json()
    statuses = [v["reservation"]["status"] for v in replay["visits"]]
    assert statuses == ["active", "cancelled", "active"]
    # The first-creation windows of the group are unchanged; the cancelled
    # week keeps its original token in the replay snapshot.
    assert replay["visits"][1]["start"] == week2["start"]
    assert replay["visits"][1]["reservation"]["cancel_token"] == token


def test_each_week_can_be_rescheduled_without_moving_the_group_snapshot(client):
    pid = open_point(client)
    first = local_date_after("UTC", 2)
    created = book_recurring(client, pid, first, weeks=2).json()
    week1 = created["visits"][0]
    rid, token = week1["reservation"]["id"], week1["reservation"]["cancel_token"]

    moved = client.post(
        f"/points/{pid}/reservations/{rid}/reschedule",
        json={
            "cancel_token": token,
            "idempotency_key": "move-week1",
            "expected_revision": 1,
            "start": iso(day_at(first, 11)),
            "end": iso(day_at(first, 11, 30)),
        },
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["revision"] == 2

    # The group replay keeps the first-created window of week 1 (and reports
    # revision 1 as creation), mirroring ordinary creation-key replay.
    replay = book_recurring(client, pid, first, weeks=2).json()
    replayed_w1 = replay["visits"][0]
    assert replayed_w1["start"] == iso(day_at(first, 9))
    assert replayed_w1["reservation"]["revision"] == 1
    assert replayed_w1["reservation"]["status"] == "active"


def test_wrong_cancel_token_on_a_week_is_refused(client):
    pid = open_point(client)
    first = local_date_after("UTC", 2)
    created = book_recurring(client, pid, first, weeks=2).json()
    rid = created["visits"][0]["reservation"]["id"]
    r = client.post(
        f"/points/{pid}/reservations/{rid}/cancel",
        json={"cancel_token": "not-the-token"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "invalid_cancel_token"


# ---------------------------------------------------------------------------
# DST conversion through the API (spring-forward week)
# ---------------------------------------------------------------------------

def test_dst_transition_week_is_converted_correctly_via_api(client):
    # Drive the wall-clock -> UTC conversion through the real endpoint.  If a
    # New York DST transition Sunday falls inside the bookable horizon (30
    # days) starting at least a week out, book the fixed slot across it and
    # assert the transition week resolves with the gap/repeat rules:
    #   spring forward: 02:30 doesn't exist -> start moves to 03:00 EDT and
    #     that week's 02:30-03:30 slot is only 30 UTC-minutes long;
    #   fall back: 00:30-01:30 spans the repeated hour (start early, end late)
    #     and that week is 120 UTC-minutes long.
    # Most calendar dates have no transition inside the horizon: the test
    # skips then (the pure engine tests cover both transitions unconditionally).
    import pytest

    ny = ZoneInfo("America/New_York")
    now = datetime.now(UTC)
    today = now.astimezone(ny).date()
    horizon = today + timedelta(days=30)
    transition = None
    spring = False
    probe = today + timedelta(days=8)
    while probe <= horizon:
        if probe.isoweekday() == 7:
            # Compare at noon: a US transition happens at 02:00/03:00, so a
            # midnight comparison would still see the pre-change offset.
            before = datetime.combine(probe - timedelta(days=7),
                                      time(12), ny).utcoffset()
            after = datetime.combine(probe, time(12), ny).utcoffset()
            if after - before in (timedelta(hours=1), timedelta(hours=-1)):
                transition = probe
                spring = after - before == timedelta(hours=1)
                break
        probe += timedelta(days=1)
    if transition is None:
        pytest.skip("no New York DST transition Sunday inside the booking horizon")
    first = transition - timedelta(weeks=1)
    pid = open_point(client, tz="America/New_York")
    if spring:
        r = book_recurring(
            client, pid, first, "02:30", "03:30", weeks=2, key="dst-spring"
        )
    else:
        r = book_recurring(
            client, pid, first, "00:30", "01:30", weeks=2, key="dst-fall"
        )
    assert r.status_code == 201, r.text
    visits = {v["week"]: v for v in r.json()["visits"]}

    def window(week):
        s = datetime.fromisoformat(visits[week]["start"].replace("Z", "+00:00"))
        e = datetime.fromisoformat(visits[week]["end"].replace("Z", "+00:00"))
        return s, e

    if spring:
        assert window(1)[1] - window(1)[0] == timedelta(hours=1)
        gap_start, gap_end = window(2)
        assert gap_end - gap_start == timedelta(minutes=30)
        # 02:30 doesn't exist -> first valid moment 03:00 EDT = 07:00Z.
        assert gap_start == datetime(
            transition.year, transition.month, transition.day, 7, tzinfo=UTC
        )
    else:
        assert window(1)[1] - window(1)[0] == timedelta(hours=1)
        rep_start, rep_end = window(2)
        assert rep_end - rep_start == timedelta(hours=2)
        # Start 00:30 EDT (earlier, -04) = 04:30Z; end 01:30 takes the later
        # EST instance (-05) = 06:30Z: a two-hour UTC span.
        assert rep_start == datetime(
            transition.year, transition.month, transition.day, 4, 30, tzinfo=UTC
        )
        assert rep_end == datetime(
            transition.year, transition.month, transition.day, 6, 30, tzinfo=UTC
        )


def test_change_protection_sees_recurring_weeks(client):
    # Republishing a narrower calendar that un-covers a booked recurring week
    # is refused and names the conflicting reservation ids.
    pid = open_point(client)
    first = local_date_after("UTC", 2)
    created = book_recurring(client, pid, first, weeks=3).json()
    week_ids = [v["reservation"]["id"] for v in created["visits"]]

    conflict = client.post(
        f"/points/{pid}/rules",
        json={
            "expected_version": 1,
            "rules": {"weekly": [], "closed_dates": [], "makeup": []},
        },
    )
    assert conflict.status_code == 409
    err = conflict.json()["error"]
    assert err["code"] == "reservation_change_conflict"
    assert set(week_ids) <= set(err["details"]["conflicting_reservation_ids"])


# ---------------------------------------------------------------------------
# Concurrency — the point row lock serializes the whole group
# ---------------------------------------------------------------------------

def test_concurrent_groups_for_same_slot_exactly_one_wins(client):
    pid = open_point(client, capacity=1)
    first = local_date_after("UTC", 2)
    barrier = threading.Barrier(2)
    outcomes = []

    def attempt(key):
        barrier.wait()
        outcomes.append(
            (key, book_recurring(client, pid, first, weeks=2, key=key).status_code)
        )

    threads = [
        threading.Thread(target=attempt, args=("race-1",)),
        threading.Thread(target=attempt, args=("race-2",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = sorted(code for _, code in outcomes)
    assert statuses == [201, 409]
    # Exactly one group holds the week-1 slot.
    clash = reserve(
        client, pid, "double-check", day_at(first, 9), day_at(first, 10)
    )
    assert clash.status_code == 409


def test_concurrent_same_key_same_content_replays(client):
    pid = open_point(client)
    first = local_date_after("UTC", 2)
    barrier = threading.Barrier(2)
    outcomes = []

    def attempt():
        barrier.wait()
        r = book_recurring(client, pid, first, weeks=3, key="same-key")
        outcomes.append((r.status_code, r.json()))

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = sorted(status for status, _ in outcomes)
    assert statuses == [200, 201]
    bodies = [body for _, body in outcomes]
    assert bodies[0] == bodies[1]
    # The group was booked exactly once: same window cannot be taken again.
    clash = reserve(
        client, pid, "double-check", day_at(first, 9), day_at(first, 10)
    )
    assert clash.status_code == 409
