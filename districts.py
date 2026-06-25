"""Frankfurt district lookup from coordinates.

Maps lat/lon to the nearest Frankfurt Stadtteil using centroid distance.
Centroids sourced from the same table used by the web frontend (index.html).
"""
import math

# Display-ready district names keyed by slug
DISTRICTS: dict[str, tuple[float, float]] = {
    "Sachsenhausen":    (50.0953, 8.6828),
    "Bornheim":         (50.1261, 8.7147),
    "Innenstadt":       (50.1109, 8.6821),
    "Nordend":          (50.1242, 8.6873),
    "Westend":          (50.1186, 8.6648),
    "Bockenheim":       (50.1176, 8.6506),
    "Gallus":           (50.1032, 8.6461),
    "Gutleutviertel":   (50.1020, 8.6543),
    "Altstadt":         (50.1113, 8.6831),
    "Ostend":           (50.1168, 8.7062),
    "Riederwald":       (50.1221, 8.7233),
    "Seckbach":         (50.1320, 8.7345),
    "Fechenheim":       (50.1137, 8.7567),
    "Dornbusch":        (50.1407, 8.6801),
    "Eschersheim":      (50.1508, 8.6679),
    "Hausen":           (50.1407, 8.6493),
    "Praunheim":        (50.1466, 8.6268),
    "Rödelheim":        (50.1280, 8.6218),
    "Heddernheim":      (50.1547, 8.6406),
    "Niederursel":      (50.1603, 8.6310),
    "Ginnheim":         (50.1415, 8.6615),
    "Sossenheim":       (50.1428, 8.5978),
    "Nied":             (50.1168, 8.5870),
    "Höchst":           (50.0987, 8.5456),
    "Flughafen":        (50.0379, 8.5622),
    "Bahnhofsviertel":  (50.1069, 8.6624),
    "Riedberg":         (50.1750, 8.6341),
    "Schwanheim":       (50.0826, 8.5778),
    "Griesheim":        (50.1083, 8.6020),
    "Oberrad":          (50.0893, 8.7031),
    "Niederrad":        (50.0855, 8.6723),
    "Eckenheim":        (50.1408, 8.7010),
    "Enkheim":          (50.1362, 8.7514),
    "Bergen-Enkheim":   (50.1461, 8.7654),
    "Berkersheim":      (50.1457, 8.7214),
    "Preungesheim":     (50.1489, 8.7042),
    "Bonames":          (50.1713, 8.7111),
    "Harheim":          (50.1904, 8.7232),
    "Nieder-Erlenbach": (50.1923, 8.6945),
    "Kalbach":          (50.1732, 8.6608),
    "Nieder-Eschbach":  (50.1841, 8.6515),
    "Frankfurter Berg":  (50.1641, 8.6960),
    "Sindlingen":       (50.0965, 8.4952),
    "Zeilsheim":        (50.0926, 8.5172),
    "Unterliederbach":  (50.1037, 8.5681),
}

FRANKFURT_CENTER = (50.1109, 8.6821)
_MAX_RADIUS_KM = 15.0


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def coords_to_district(lat: float | None, lon: float | None) -> str | None:
    """Return the nearest Frankfurt district name, or None if outside the city."""
    if lat is None or lon is None:
        return None
    if _haversine_km(lat, lon, *FRANKFURT_CENTER) > _MAX_RADIUS_KM:
        return None
    best_name = None
    best_dist = float("inf")
    for name, (clat, clon) in DISTRICTS.items():
        d = _haversine_km(lat, lon, clat, clon)
        if d < best_dist:
            best_dist = d
            best_name = name
    return best_name
