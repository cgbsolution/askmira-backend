"""Geofence distance and the never-reject rule."""

from __future__ import annotations

import pytest

from app.services.attendance.service import haversine_metres

# Vizionforge-ish reference points in Bengaluru, far enough apart to be
# unambiguous but close enough to exercise the small-distance case.
OFFICE = (12.9716, 77.5946)


def test_zero_distance_to_itself() -> None:
    assert haversine_metres(*OFFICE, *OFFICE) == pytest.approx(0, abs=0.01)


def test_a_hundred_metres_north_reads_as_a_hundred_metres() -> None:
    # 0.001 degrees of latitude is about 111 metres.
    lat, lon = OFFICE
    distance = haversine_metres(lat, lon, lat + 0.001, lon)
    assert distance == pytest.approx(111, abs=2)


def test_distance_is_symmetric() -> None:
    other = (12.9352, 77.6245)
    assert haversine_metres(*OFFICE, *other) == pytest.approx(
        haversine_metres(*other, *OFFICE), abs=0.01
    )


def test_a_few_kilometres_reads_correctly() -> None:
    # Bengaluru city centre to Koramangala, roughly 5 km.
    distance = haversine_metres(*OFFICE, 12.9352, 77.6245)
    assert 4_000 < distance < 6_000


def test_longitude_distance_shrinks_away_from_the_equator() -> None:
    """A sanity check that the formula is spherical, not planar.

    A planar approximation would give the same metres per degree of longitude
    at every latitude, which is wrong and would make northern geofences too
    permissive.
    """
    near_equator = haversine_metres(1.0, 0.0, 1.0, 0.01)
    far_north = haversine_metres(60.0, 0.0, 60.0, 0.01)
    assert far_north < near_equator / 1.5
