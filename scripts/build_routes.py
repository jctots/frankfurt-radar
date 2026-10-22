"""Build per-line route GeoJSON for the web map from a gtfs.de feed.

Reads the full Germany feed (https://download.gtfs.de/germany/free/latest.zip,
as a .zip or an extracted folder) and writes one small GeoJSON file per
Frankfurt line to web/static/routes/<service>/<line>.geojson, plus an
index.json listing what was built. The raw feed is never committed — only
this output is (see docs/routes.md).

gtfs.de has no shapes.txt, so each route is drawn as straight segments
between consecutive stops. Lines are selected by geography (at least one stop
inside the Frankfurt bounding box) because line names repeat across Germany
(S8 exists in Munich and Berlin too).

Usage:
    python scripts/build_routes.py path/to/latest.zip [--out web/static/routes]
"""

import argparse
import collections
import csv
import io
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

BBOX = (50.015, 8.47, 50.23, 8.80)   # Frankfurt city area: lat_min, lon_min, lat_max, lon_max
BUS_AGENCIES = {"traffiQ"}           # city buses only — regional bus numbers collide across RMV
MAX_PATTERNS = 4                     # per line: most-run stop sequences (directions/branches) kept
ATTRIBUTION = "Timetable data: DELFI e.V. via gtfs.de, CC BY 4.0"

# Folder names match the RMV alert's service label (models.CLS_LABEL), lowercased without '-'
REQUIRED = {
    "sbahn": {f"S{n}" for n in range(1, 10)},
    "ubahn": {f"U{n}" for n in range(1, 10)},
}
MIN_TRAM_LINES = 8
MAX_LINE_DROP = 0.2   # fail if the line count falls by more than this vs. the previous index.json


def service_for(route_type: str, name: str, agency: str) -> str | None:
    """Map a GTFS route to the RMV service folder it belongs in, or None to skip it."""
    if route_type == "0":
        return "tram"
    if route_type == "1":
        return "ubahn"
    if route_type == "2":
        if re.fullmatch(r"S\d+", name):
            return "sbahn"
        if re.fullmatch(r"R[BE]\d+", name):
            return "regional"
        return None   # ICE/IC/EC/FLX/night specials — RMV doesn't alert on these
    if route_type == "3" and agency in BUS_AGENCIES and name != "SEV":   # SEV = assorted rail replacement buses
        return "bus"
    return None


def line_key(name: str) -> str:
    """Normalised line name used as the file name — RMV may send 'RE 30' where GTFS has 'RE30'."""
    return re.sub(r"\s+", "", name)


class Feed:
    """Read GTFS tables from a .zip or an extracted folder, streaming row by row."""

    def __init__(self, path: Path):
        self.path = path
        self.zip = zipfile.ZipFile(path) if path.is_file() else None

    def open(self, name: str):
        if self.zip:
            return io.TextIOWrapper(self.zip.open(name), encoding="utf-8-sig")
        return open(self.path / name, encoding="utf-8-sig")

    def rows(self, name: str):
        with self.open(name) as f:
            yield from csv.DictReader(f)

    def stop_times(self):
        """Yield (trip_id, stop_id, stop_sequence) — plain split, csv.DictReader is too slow for 2 GB."""
        with self.open("stop_times.txt") as f:
            header = f.readline().strip().split(",")
            it, ist, iseq = header.index("trip_id"), header.index("stop_id"), header.index("stop_sequence")
            for line in f:
                p = line.rstrip("\n").split(",")
                yield p[it], p[ist], p[iseq]

    def version(self) -> str:
        row = next(self.rows("feed_info.txt"), {})
        return row.get("feed_version", "")


def build(feed: Feed) -> dict[tuple[str, str], list[dict]]:
    """Return {(service, line): [GeoJSON features]} for every Frankfurt line in the feed."""
    agencies = {a["agency_id"]: a["agency_name"] for a in feed.rows("agency.txt")}
    route_line = {}
    for r in feed.rows("routes.txt"):
        service = service_for(r["route_type"], r["route_short_name"], agencies.get(r["agency_id"], ""))
        if service:
            route_line[r["route_id"]] = (service, line_key(r["route_short_name"]))
    trip_line = {t["trip_id"]: route_line[t["route_id"]] for t in feed.rows("trips.txt") if t["route_id"] in route_line}

    stops, in_city = {}, set()
    for s in feed.rows("stops.txt"):
        lat, lon = float(s["stop_lat"]), float(s["stop_lon"])
        stops[s["stop_id"]] = (round(lon, 5), round(lat, 5), s["stop_name"])
        if BBOX[0] <= lat <= BBOX[2] and BBOX[1] <= lon <= BBOX[3]:
            in_city.add(s["stop_id"])

    # Two passes over stop_times: holding every candidate trip's sequence at once doesn't fit in memory
    touching = {t for t, s, _ in feed.stop_times() if s in in_city and t in trip_line}
    seqs = collections.defaultdict(list)
    for t, s, q in feed.stop_times():
        if t in touching:
            seqs[t].append((int(q), s))

    patterns = collections.defaultdict(collections.Counter)
    for trip, seq in seqs.items():
        patterns[trip_line[trip]][tuple(s for _, s in sorted(seq))] += 1

    lines = {}
    for key, counts in patterns.items():
        # Longest sequence per terminus pair, then the most-run of those; skip a reverse of one already kept
        best = {}
        for pat, n in counts.items():
            ends = (pat[0], pat[-1])
            if ends not in best or len(pat) > len(best[ends][0]):
                best[ends] = (pat, n)
        kept, feats = set(), []
        for pat, n in sorted(best.values(), key=lambda x: -x[1]):
            if pat[::-1] in kept:
                continue
            kept.add(pat)
            feats.append({
                "type": "Feature",
                "properties": {"from": stops[pat[0]][2], "to": stops[pat[-1]][2], "trips": n},
                "geometry": {"type": "LineString", "coordinates": [list(stops[s][:2]) for s in pat]},
            })
            if len(feats) == MAX_PATTERNS:
                break
        lines[key] = feats
    return lines


def check(lines: dict, previous_count: int | None) -> list[str]:
    """Return problems that should block publishing this build (an empty list means OK)."""
    built = collections.defaultdict(set)
    for service, line in lines:
        built[service].add(line)
    problems = [f"missing {service} lines: {sorted(need - built[service])}"
                for service, need in REQUIRED.items() if need - built[service]]
    if len(built["tram"]) < MIN_TRAM_LINES:
        problems.append(f"only {len(built['tram'])} tram lines (expected at least {MIN_TRAM_LINES})")
    if previous_count and len(lines) < previous_count * (1 - MAX_LINE_DROP):
        problems.append(f"line count dropped from {previous_count} to {len(lines)}")
    return problems


def write(lines: dict, out: Path, feed_version: str) -> None:
    for sub in out.iterdir() if out.exists() else []:
        if sub.is_dir():
            shutil.rmtree(sub)   # removed lines must disappear, not linger from an older build
    index = collections.defaultdict(list)
    for (service, line), feats in sorted(lines.items()):
        path = out / service / f"{line}.geojson"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":"), ensure_ascii=False))
        index[service].append(line)
    for names in index.values():
        names.sort(key=lambda n: (len(n), n))
    (out / "index.json").write_text(json.dumps({
        "feed_version": feed_version,
        "attribution": ATTRIBUTION,
        "lines": dict(sorted(index.items())),
    }, indent=1, ensure_ascii=False) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("feed", type=Path, help="gtfs.de latest.zip or an extracted folder")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / "web" / "static" / "routes")
    args = ap.parse_args()

    feed = Feed(args.feed)
    lines = build(feed)
    previous = None
    if (args.out / "index.json").exists():
        previous = sum(len(v) for v in json.loads((args.out / "index.json").read_text())["lines"].values())
    problems = check(lines, previous)
    if problems:
        for p in problems:
            print(f"ERROR: {p}", file=sys.stderr)
        print("Nothing written.", file=sys.stderr)
        return 1
    write(lines, args.out, feed.version())
    print(f"Wrote {len(lines)} lines to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
