import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("build_routes", Path(__file__).parent.parent / "scripts" / "build_routes.py")
br = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(br)

# Frankfurt stops F1–F3, plus M1–M2 in Munich (outside the bbox)
STOPS = {"F1": (50.107, 8.663), "F2": (50.110, 8.680), "F3": (50.115, 8.700), "M1": (48.140, 11.558), "M2": (48.150, 11.570)}
ROUTES = [
    # route_id, agency_id, short_name, route_type
    ("r_s8", "db", "S8", "2"),
    ("r_s8_muc", "db", "S8", "2"),      # Munich S8 — no Frankfurt stop, must be dropped
    ("r_ice", "db", "ICE 11", "2"),     # long-distance — not a service RMV alerts on
    ("r_12", "tq", "12", "0"),          # tram 12
    ("r_bus12", "rmv", "12", "3"),      # regional bus 12 — not traffiQ, must be dropped
    ("r_30", "tq", "30", "3"),
]
TRIPS = {  # trip_id: (route_id, stop sequence)
    "t1": ("r_s8", ["F1", "F2", "F3"]),
    "t2": ("r_s8", ["F1", "F2", "F3"]),
    "t3": ("r_s8", ["F3", "F2", "F1"]),     # reverse of t1 — kept once, not twice
    "t4": ("r_s8_muc", ["M1", "M2"]),
    "t5": ("r_ice", ["F1", "M1"]),
    "t6": ("r_12", ["F2", "F3"]),
    "t7": ("r_bus12", ["F1", "F3"]),
    "t8": ("r_30", ["F1", "F3"]),
}


def _write_feed(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "agency.txt").write_text("agency_id,agency_name\ndb,DB Regio\ntq,traffiQ\nrmv,RMV\n")
    (root / "feed_info.txt").write_text("feed_publisher_name,feed_version\ngtfs.de,test-1\n")
    (root / "stops.txt").write_text("stop_name,stop_id,stop_lat,stop_lon\n" + "".join(f"Stop {k},{k},{la},{lo}\n" for k, (la, lo) in STOPS.items()))
    (root / "routes.txt").write_text("route_id,agency_id,route_short_name,route_type\n" + "".join(",".join(r) + "\n" for r in ROUTES))
    (root / "trips.txt").write_text("route_id,service_id,trip_id\n" + "".join(f"{r},1,{t}\n" for t, (r, _) in TRIPS.items()))
    (root / "stop_times.txt").write_text("trip_id,arrival_time,departure_time,stop_id,stop_sequence\n" + "".join(
        f"{t},08:00:00,08:00:00,{s},{i}\n" for t, (_, seq) in TRIPS.items() for i, s in enumerate(seq)))


@pytest.fixture
def feed_dir(tmp_path):
    _write_feed(tmp_path / "gtfs")
    return tmp_path / "gtfs"


def test_selects_frankfurt_lines_by_service(feed_dir):
    lines = br.build(br.Feed(feed_dir))
    assert set(lines) == {("sbahn", "S8"), ("tram", "12"), ("bus", "30")}


def test_reverse_pattern_kept_once(feed_dir):
    feats = br.build(br.Feed(feed_dir))[("sbahn", "S8")]
    assert len(feats) == 1
    assert feats[0]["properties"] == {"from": "Stop F1", "to": "Stop F3", "trips": 2}
    assert feats[0]["geometry"]["coordinates"] == [[8.663, 50.107], [8.68, 50.11], [8.7, 50.115]]


def test_reads_zip_same_as_folder(feed_dir, tmp_path):
    zpath = tmp_path / "latest.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        for f in feed_dir.iterdir():
            z.write(f, f.name)
    assert br.build(br.Feed(zpath)) == br.build(br.Feed(feed_dir))


def test_check_flags_missing_required_lines(feed_dir):
    problems = br.check(br.build(br.Feed(feed_dir)), previous_count=None)
    assert any("missing sbahn" in p for p in problems)
    assert any("missing ubahn" in p for p in problems)
    assert any("tram lines" in p for p in problems)


def test_check_flags_large_drop():
    lines = {("sbahn", f"S{n}"): [] for n in range(1, 10)}
    lines |= {("ubahn", f"U{n}"): [] for n in range(1, 10)}
    lines |= {("tram", str(n)): [] for n in range(11, 19)}
    assert br.check(lines, previous_count=26) == []
    assert br.check(lines, previous_count=100) == ["line count dropped from 100 to 26"]


def test_write_replaces_old_output(feed_dir, tmp_path):
    out = tmp_path / "routes"
    (out / "bus").mkdir(parents=True)
    (out / "bus" / "99.geojson").write_text("{}")   # a line that no longer exists
    br.write(br.build(br.Feed(feed_dir)), out, "test-1")
    assert not (out / "bus" / "99.geojson").exists()
    assert (out / "sbahn" / "S8.geojson").exists()
    index = json.loads((out / "index.json").read_text())
    assert index["feed_version"] == "test-1"
    assert index["lines"] == {"bus": ["30"], "sbahn": ["S8"], "tram": ["12"]}


def test_line_key_strips_spaces():
    assert br.line_key("RE 30") == "RE30"
