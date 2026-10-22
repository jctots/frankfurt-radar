# Line routes on the map

Clicking a transport alert draws its line on the map. The route files live in
`web/static/routes/<service>/<line>.geojson` (e.g. `sbahn/S8.geojson`), where
`<service>` is the alert's service label lowercased without the hyphen
(`S-Bahn` → `sbahn`). `index.json` lists every line that has a file. An alert
whose line has no file falls back to the normal marker popup.

## Where the data comes from

[gtfs.de](https://gtfs.de/de/feeds/)'s full Germany feed, built from DELFI e.V.
data and licensed CC BY 4.0 — the map shows the attribution while a route is on
screen. The feed has no `shapes.txt`, so a route is drawn as straight segments
between consecutive stops.

`scripts/build_routes.py` keeps only what the map needs:

- trams, U-Bahn, S-Bahn and RE/RB lines with at least one stop inside the
  Frankfurt bounding box — line names repeat across Germany, so this is what
  separates Frankfurt's S8 from Munich's
- traffiQ city buses (regional bus numbers collide across RMV); not `SEV`
- per line, up to four of the most-run stop sequences (directions and branches),
  with a reverse of an already-kept sequence skipped

The raw feed (~300 MB zipped, 2.3 GB extracted) is never committed — `gtfs/` and
`latest.zip` are in `.gitignore`.

## Updating

The **Update routes** workflow (`.github/workflows/update-routes.yml`) runs on
the 1st of every month, and can be started by hand from the Actions tab — e.g.
after the December timetable change. It downloads the feed, rebuilds the
routes, and opens a PR listing added and removed lines if anything changed.
Spot-check a changed line on the map, then merge. It never auto-merges.

The build refuses to write anything — and the workflow fails — if S1–S9 or
U1–U9 are missing, fewer than 8 tram lines are found, or the line count drops
by more than 20% against the current `index.json`.

To run it locally:

```bash
curl -LO https://download.gtfs.de/germany/free/latest.zip
python scripts/build_routes.py latest.zip
```
