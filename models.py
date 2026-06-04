from dataclasses import dataclass, field
from typing import Optional

SERVICE_CLS: dict[str, int] = {
    "regional": 4,
    "sbahn": 8,
    "ubahn": 16,
    "tram": 32,
    "bus": 64,
}

CLS_LABEL: dict[int, str] = {
    4: "Regional",
    8: "S-Bahn",
    16: "U-Bahn",
    32: "Tram",
    64: "Bus",
}

# Priority order for picking the primary service when a message affects multiple types
CLS_PRIORITY = [8, 16, 32, 64, 4]  # sbahn > ubahn > tram > bus > regional

SOURCE_LABEL: dict[str, str] = {"rmv": "Transport", "polizei": "Police", "dwd": "Weather"}
SOURCE_EMOJI: dict[str, str] = {"rmv": "🚇", "polizei": "🚨", "dwd": "⛈️"}


@dataclass
class Alert:
    id: str
    source: str           # "rmv" | "dwd" | "polizei"
    title: str            # German, pre-translation
    body: str             # German, HTML stripped, pre-translation
    url: Optional[str]
    valid_until: Optional[str]   # ISO string or None
    service: Optional[str]       # human-readable label, e.g. "S-Bahn"
    lines: list[str] = field(default_factory=list)  # affected lines for primary service
    published_at: Optional[str] = None  # ISO UTC; set by PolizeiPoller for 24h filter
    severity: Optional[int] = None      # 1–4 (minor→extreme); set by DWDPoller
    lat: Optional[float] = None         # map pin latitude
    lon: Optional[float] = None         # map pin longitude
    location_label: Optional[str] = None  # human-readable location hint
