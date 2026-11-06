"""City gazetteer used for location detection and travel-radius checks.

Deliberately small, curated and dependency-free. Add your own cities here or
override coordinates in `profile.yaml` under `city_coords`.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable

# Canonical name -> (latitude, longitude, country)
CITY_COORDS: dict[str, tuple[float, float, str]] = {
    # India - primary film hubs
    "mumbai": (19.0760, 72.8777, "India"),
    "delhi": (28.6139, 77.2090, "India"),
    "new delhi": (28.6139, 77.2090, "India"),
    "noida": (28.5355, 77.3910, "India"),
    "greater noida": (28.4744, 77.5040, "India"),
    "gurugram": (28.4595, 77.0266, "India"),
    "faridabad": (28.4089, 77.3178, "India"),
    "ghaziabad": (28.6692, 77.4538, "India"),
    "bangalore": (12.9716, 77.5946, "India"),
    "bengaluru": (12.9716, 77.5946, "India"),
    "hyderabad": (17.3850, 78.4867, "India"),
    "secunderabad": (17.4399, 78.4983, "India"),
    "chennai": (13.0827, 80.2707, "India"),
    "kolkata": (22.5726, 88.3639, "India"),
    "pune": (18.5204, 73.8567, "India"),
    "ahmedabad": (23.0225, 72.5714, "India"),
    "jaipur": (26.9124, 75.7873, "India"),
    "lucknow": (26.8467, 80.9462, "India"),
    "kanpur": (26.4499, 80.3319, "India"),
    "varanasi": (25.3176, 82.9739, "India"),
    "patna": (25.5941, 85.1376, "India"),
    "bhopal": (23.2599, 77.4126, "India"),
    "indore": (22.7196, 75.8577, "India"),
    "chandigarh": (30.7333, 76.7794, "India"),
    "mohali": (30.7046, 76.7179, "India"),
    "amritsar": (31.6340, 74.8723, "India"),
    "ludhiana": (30.9010, 75.8573, "India"),
    "dehradun": (30.3165, 78.0322, "India"),
    "shimla": (31.1048, 77.1734, "India"),
    "srinagar": (34.0837, 74.7973, "India"),
    "jammu": (32.7266, 74.8570, "India"),
    "raipur": (21.2514, 81.6296, "India"),
    "ranchi": (23.3441, 85.3096, "India"),
    "bhubaneswar": (20.2961, 85.8245, "India"),
    "cuttack": (20.4625, 85.8830, "India"),
    "guwahati": (26.1445, 91.7362, "India"),
    "shillong": (25.5788, 91.8933, "India"),
    "imphal": (24.8170, 93.9368, "India"),
    "aizawl": (23.7271, 92.7176, "India"),
    "kohima": (25.6751, 94.1086, "India"),
    "agartala": (23.8315, 91.2868, "India"),
    "gangtok": (27.3389, 88.6065, "India"),
    "itanagar": (27.0844, 93.6053, "India"),
    "panaji": (15.4909, 73.8278, "India"),
    "goa": (15.2993, 74.1240, "India"),
    "margao": (15.2832, 73.9862, "India"),
    "kochi": (9.9312, 76.2673, "India"),
    "cochin": (9.9312, 76.2673, "India"),
    "thiruvananthapuram": (8.5241, 76.9366, "India"),
    "trivandrum": (8.5241, 76.9366, "India"),
    "kozhikode": (11.2588, 75.7804, "India"),
    "thrissur": (10.5276, 76.2144, "India"),
    "coimbatore": (11.0168, 76.9558, "India"),
    "madurai": (9.9252, 78.1198, "India"),
    "trichy": (10.7905, 78.7047, "India"),
    "tiruchirappalli": (10.7905, 78.7047, "India"),
    "salem": (11.6643, 78.1460, "India"),
    "mysore": (12.2958, 76.6394, "India"),
    "mysuru": (12.2958, 76.6394, "India"),
    "mangalore": (12.9141, 74.8560, "India"),
    "mangaluru": (12.9141, 74.8560, "India"),
    "hubli": (15.3647, 75.1240, "India"),
    "belgaum": (15.8497, 74.4977, "India"),
    "nagpur": (21.1458, 79.0882, "India"),
    "nashik": (19.9975, 73.7898, "India"),
    "aurangabad": (19.8762, 75.3433, "India"),
    "kolhapur": (16.7050, 74.2433, "India"),
    "surat": (21.1702, 72.8311, "India"),
    "vadodara": (22.3072, 73.1812, "India"),
    "rajkot": (22.3039, 70.8022, "India"),
    "bhavnagar": (21.7645, 72.1519, "India"),
    "jodhpur": (26.2389, 73.0243, "India"),
    "udaipur": (24.5854, 73.7125, "India"),
    "ajmer": (26.4499, 74.6399, "India"),
    "bikaner": (28.0229, 73.3119, "India"),
    "agra": (27.1767, 78.0081, "India"),
    "mathura": (27.4924, 77.6737, "India"),
    "meerut": (28.9845, 77.7064, "India"),
    "bareilly": (28.3670, 79.4304, "India"),
    "aligarh": (27.8974, 78.0880, "India"),
    "moradabad": (28.8386, 78.7733, "India"),
    "gorakhpur": (26.7606, 83.3732, "India"),
    "prayagraj": (25.4358, 81.8463, "India"),
    "allahabad": (25.4358, 81.8463, "India"),
    "jabalpur": (23.1815, 79.9864, "India"),
    "gwalior": (26.2183, 78.1828, "India"),
    "ujjain": (23.1765, 75.7885, "India"),
    "kota": (25.2138, 75.8648, "India"),
    "jalandhar": (31.3260, 75.5762, "India"),
    "patiala": (30.3398, 76.3869, "India"),
    "ambala": (30.3752, 76.7821, "India"),
    "rohtak": (28.8955, 76.6066, "India"),
    "hisar": (29.1492, 75.7217, "India"),
    "karnal": (29.6857, 76.9905, "India"),
    "sonipat": (28.9931, 77.0151, "India"),
    "panipat": (29.3909, 76.9635, "India"),
    "haridwar": (29.9457, 78.1642, "India"),
    "rishikesh": (30.0869, 78.2676, "India"),
    "nainital": (29.3919, 79.4542, "India"),
    "mussoorie": (30.4598, 78.0644, "India"),
    "dharamshala": (32.2190, 76.3234, "India"),
    "manali": (32.2432, 77.1892, "India"),
    "leh": (34.1526, 77.5771, "India"),
    "port blair": (11.6234, 92.7265, "India"),
    # South Asia
    "karachi": (24.8607, 67.0011, "Pakistan"),
    "lahore": (31.5204, 74.3587, "Pakistan"),
    "islamabad": (33.6844, 73.0479, "Pakistan"),
    "dhaka": (23.8103, 90.4125, "Bangladesh"),
    "chittagong": (22.3569, 91.7832, "Bangladesh"),
    "colombo": (6.9271, 79.8612, "Sri Lanka"),
    "kathmandu": (27.7172, 85.3240, "Nepal"),
    "kabul": (34.5553, 69.2075, "Afghanistan"),
    # Middle East
    "dubai": (25.2048, 55.2708, "United Arab Emirates"),
    "abu dhabi": (24.4539, 54.3773, "United Arab Emirates"),
    "sharjah": (25.3463, 55.4209, "United Arab Emirates"),
    "doha": (25.2854, 51.5310, "Qatar"),
    "riyadh": (24.7136, 46.6753, "Saudi Arabia"),
    "jeddah": (21.4858, 39.1925, "Saudi Arabia"),
    "muscat": (23.5880, 58.3829, "Oman"),
    "kuwait city": (29.3759, 47.9774, "Kuwait"),
    "manama": (26.2285, 50.5860, "Bahrain"),
    "amman": (31.9454, 35.9284, "Jordan"),
    "beirut": (33.8938, 35.5018, "Lebanon"),
    "tel aviv": (32.0853, 34.7818, "Israel"),
    "istanbul": (41.0082, 28.9784, "Turkey"),
    "ankara": (39.9334, 32.8597, "Turkey"),
    "tehran": (35.6892, 51.3890, "Iran"),
    # Europe
    "london": (51.5074, -0.1278, "United Kingdom"),
    "manchester": (53.4808, -2.2426, "United Kingdom"),
    "birmingham": (52.4862, -1.8904, "United Kingdom"),
    "glasgow": (55.8642, -4.2518, "United Kingdom"),
    "edinburgh": (55.9533, -3.1883, "United Kingdom"),
    "dublin": (53.3498, -6.2603, "Ireland"),
    "paris": (48.8566, 2.3522, "France"),
    "cannes": (43.5528, 7.0174, "France"),
    "lyon": (45.7640, 4.8357, "France"),
    "marseille": (43.2965, 5.3698, "France"),
    "berlin": (52.5200, 13.4050, "Germany"),
    "munich": (48.1351, 11.5820, "Germany"),
    "hamburg": (53.5511, 9.9937, "Germany"),
    "cologne": (50.9375, 6.9603, "Germany"),
    "frankfurt": (50.1109, 8.6821, "Germany"),
    "madrid": (40.4168, -3.7038, "Spain"),
    "barcelona": (41.3874, 2.1686, "Spain"),
    "lisbon": (38.7223, -9.1393, "Portugal"),
    "rome": (41.9028, 12.4964, "Italy"),
    "milan": (45.4642, 9.1900, "Italy"),
    "venice": (45.4408, 12.3155, "Italy"),
    "naples": (40.8518, 14.2681, "Italy"),
    "amsterdam": (52.3676, 4.9041, "Netherlands"),
    "rotterdam": (51.9244, 4.4777, "Netherlands"),
    "brussels": (50.8503, 4.3517, "Belgium"),
    "vienna": (48.2082, 16.3738, "Austria"),
    "zurich": (47.3769, 8.5417, "Switzerland"),
    "geneva": (46.2044, 6.1432, "Switzerland"),
    "stockholm": (59.3293, 18.0686, "Sweden"),
    "oslo": (59.9139, 10.7522, "Norway"),
    "copenhagen": (55.6761, 12.5683, "Denmark"),
    "helsinki": (60.1699, 24.9384, "Finland"),
    "warsaw": (52.2297, 21.0122, "Poland"),
    "prague": (50.0755, 14.4378, "Czechia"),
    "budapest": (47.4979, 19.0402, "Hungary"),
    "athens": (37.9838, 23.7275, "Greece"),
    "thessaloniki": (40.6401, 22.9444, "Greece"),
    "moscow": (55.7558, 37.6173, "Russia"),
    "kyiv": (50.4501, 30.5234, "Ukraine"),
    "reykjavik": (64.1466, -21.9426, "Iceland"),
    # Americas
    "new york": (40.7128, -74.0060, "United States"),
    "los angeles": (34.0522, -118.2437, "United States"),
    "hollywood": (34.0928, -118.3287, "United States"),
    "atlanta": (33.7490, -84.3880, "United States"),
    "chicago": (41.8781, -87.6298, "United States"),
    "austin": (30.2672, -97.7431, "United States"),
    "miami": (25.7617, -80.1918, "United States"),
    "toronto": (43.6532, -79.3832, "Canada"),
    "vancouver": (49.2827, -123.1207, "Canada"),
    "montreal": (45.5017, -73.5673, "Canada"),
    "mexico city": (19.4326, -99.1332, "Mexico"),
    "guadalajara": (20.6597, -103.3496, "Mexico"),
    "bogota": (4.7110, -74.0721, "Colombia"),
    "buenos aires": (-34.6037, -58.3816, "Argentina"),
    "sao paulo": (-23.5505, -46.6333, "Brazil"),
    "rio de janeiro": (-22.9068, -43.1729, "Brazil"),
    "lima": (-12.0464, -77.0428, "Peru"),
    "santiago": (-33.4489, -70.6693, "Chile"),
    # Asia-Pacific
    "tokyo": (35.6762, 139.6503, "Japan"),
    "osaka": (34.6937, 135.5023, "Japan"),
    "seoul": (37.5665, 126.9780, "South Korea"),
    "busan": (35.1796, 129.0756, "South Korea"),
    "beijing": (39.9042, 116.4074, "China"),
    "shanghai": (31.2304, 121.4737, "China"),
    "hong kong": (22.3193, 114.1694, "Hong Kong"),
    "taipei": (25.0330, 121.5654, "Taiwan"),
    "singapore": (1.3521, 103.8198, "Singapore"),
    "bangkok": (13.7563, 100.5018, "Thailand"),
    "kuala lumpur": (3.1390, 101.6869, "Malaysia"),
    "jakarta": (-6.2088, 106.8456, "Indonesia"),
    "bali": (-8.4095, 115.1889, "Indonesia"),
    "manila": (14.5995, 120.9842, "Philippines"),
    "ho chi minh city": (10.8231, 106.6297, "Vietnam"),
    "hanoi": (21.0285, 105.8542, "Vietnam"),
    "sydney": (-33.8688, 151.2093, "Australia"),
    "melbourne": (-37.8136, 144.9631, "Australia"),
    "brisbane": (-27.4698, 153.0251, "Australia"),
    "auckland": (-36.8485, 174.7633, "New Zealand"),
    "wellington": (-41.2866, 174.7756, "New Zealand"),
    # Africa
    "cairo": (30.0444, 31.2357, "Egypt"),
    "alexandria": (31.2001, 29.9187, "Egypt"),
    "lagos": (6.5244, 3.3792, "Nigeria"),
    "nairobi": (-1.2921, 36.8219, "Kenya"),
    "cape town": (-33.9249, 18.4241, "South Africa"),
    "johannesburg": (-26.2041, 28.0473, "South Africa"),
    "casablanca": (33.5731, -7.5898, "Morocco"),
    "marrakech": (31.6295, -7.9811, "Morocco"),
    "tunis": (36.8065, 10.1815, "Tunisia"),
    "accra": (5.6037, -0.1870, "Ghana"),
    "addis ababa": (8.9806, 38.7578, "Ethiopia"),
}

# Common aliases and short forms -> canonical
ALIASES: dict[str, str] = {
    "bombay": "mumbai",
    "calcutta": "kolkata",
    "madras": "chennai",
    "bengaluru": "bangalore",
    "gurgaon": "gurugram",
    "cochin": "kochi",
    "trivandrum": "thiruvananthapuram",
    "mysuru": "mysore",
    "mangaluru": "mangalore",
    "allahabad": "prayagraj",
    "pondicherry": "puducherry",
    "new bombay": "navi mumbai",
    "navi mumbai": "navi mumbai",
    "ncr": "delhi",
    "delhi ncr": "delhi",
    "national capital region": "delhi",
    "nyc": "new york",
    "la": "los angeles",
    "sf": "san francisco",
    "san francisco": "san francisco",
    "d.c.": "washington",
    "washington dc": "washington",
    "washington d.c.": "washington",
    "uk": "london",
    "u.k.": "london",
    "uae": "dubai",
    "u.a.e.": "dubai",
}

CITY_COORDS.update(
    {
        "puducherry": (11.9416, 79.8083, "India"),
        "pondicherry": (11.9416, 79.8083, "India"),
        "navi mumbai": (19.0330, 73.0297, "India"),
        "washington": (38.9072, -77.0369, "United States"),
        "san francisco": (37.7749, -122.4194, "United States"),
    }
)

# Pre-compiled city match patterns, longest first to avoid "delhi" matching
# inside "new delhi".
_CITY_PATTERNS: list[tuple[str, re.Pattern[str]]] = []
for _name in sorted(set(CITY_COORDS) | set(ALIASES), key=len, reverse=True):
    _canonical = ALIASES.get(_name, _name)
    # \b is not reliable for names with dots; use a light custom boundary.
    _pattern = re.compile(rf"(?<![\w]){re.escape(_name)}(?![\w])", re.IGNORECASE)
    _CITY_PATTERNS.append((_canonical, _pattern))


@dataclass(frozen=True)
class CityHit:
    city: str
    country: str
    latitude: float
    longitude: float
    matched: str


def canonical_city(name: str) -> str | None:
    key = re.sub(r"\s+", " ", (name or "").strip().lower())
    key = ALIASES.get(key, key)
    return key if key in CITY_COORDS else None


def find_city(text: str) -> CityHit | None:
    """Return the first/strongest city mentioned in text."""
    if not text:
        return None
    for canonical, pattern in _CITY_PATTERNS:
        match = pattern.search(text)
        if match:
            lat, lon, country = CITY_COORDS.get(canonical, (0.0, 0.0, ""))
            return CityHit(canonical, country, lat, lon, match.group(0))
    return None


def find_all_cities(text: str, limit: int = 10) -> list[CityHit]:
    hits: list[CityHit] = []
    seen: set[str] = set()
    if not text:
        return hits
    for canonical, pattern in _CITY_PATTERNS:
        match = pattern.search(text)
        if match and canonical not in seen:
            lat, lon, country = CITY_COORDS.get(canonical, (0.0, 0.0, ""))
            hits.append(CityHit(canonical, country, lat, lon, match.group(0)))
            seen.add(canonical)
            if len(hits) >= limit:
                break
    return hits


def coords_for(city: str | None) -> tuple[float, float] | None:
    if not city:
        return None
    canonical = canonical_city(city)
    if not canonical:
        return None
    lat, lon, _ = CITY_COORDS[canonical]
    return lat, lon


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(h)))


def distance_between(city_a: str | None, city_b: str | None) -> float | None:
    a, b = coords_for(city_a), coords_for(city_b)
    if not a or not b:
        return None
    return haversine_km(a, b)


def city_tokens() -> Iterable[str]:
    return set(CITY_COORDS) | set(ALIASES)
