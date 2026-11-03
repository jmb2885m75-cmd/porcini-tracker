"""Soil texture and pH for a coordinate, from the ISRIC SoilGrids geodata service (250 m resolution).

Lookups fail soft: any network/parse problem returns None so a run never breaks because of soil data.
"""
from typing import Any, Dict, Optional

import requests

SOILGRIDS_URL = "https://rest.isric.org/soilgrids/v2.0/properties/query"
SOIL_PROPERTIES = ("clay", "sand", "silt", "phh2o")
SOIL_DEPTHS = ("0-5cm", "5-15cm")
CALCAREOUS_PH = 7.5


def usda_texture(clay: float, sand: float, silt: float) -> str:
    """USDA texture class from clay/sand/silt percentages."""
    if silt + 1.5 * clay < 15:
        return "sand"
    if silt + 2 * clay < 30:
        return "loamy sand"
    if (7 <= clay < 20 and sand > 52) or (clay < 7 and silt < 50):
        return "sandy loam"
    if 7 <= clay < 27 and 28 <= silt < 50 and sand <= 52:
        return "loam"
    if (silt >= 50 and 12 <= clay < 27) or (50 <= silt < 80 and clay < 12):
        return "silt loam"
    if silt >= 80 and clay < 12:
        return "silt"
    if 20 <= clay < 35 and silt < 28 and sand > 45:
        return "sandy clay loam"
    if 27 <= clay < 40 and 20 < sand <= 45:
        return "clay loam"
    if 27 <= clay < 40 and sand <= 20:
        return "silty clay loam"
    if clay >= 35 and sand > 45:
        return "sandy clay"
    if clay >= 40 and silt >= 40:
        return "silty clay"
    if clay >= 40:
        return "clay"
    return "loam"


def drainage_class(texture: str) -> str:
    if texture in ("sand", "loamy sand", "sandy loam"):
        return "drains quickly"
    if texture in ("clay", "silty clay", "sandy clay"):
        return "holds water"
    return "moderate drainage"


def ph_class(ph: float) -> str:
    if ph < 5.5:
        return "acidic"
    if ph < 6.5:
        return "slightly acidic"
    if ph < CALCAREOUS_PH:
        return "neutral"
    return "alkaline"


def _layer_means(payload: Any) -> Dict[str, float]:
    """Mean over the requested depths per property, converted to conventional units (% and pH)."""
    means: Dict[str, float] = {}
    layers = ((payload or {}).get("properties") or {}).get("layers") or []
    for layer in layers:
        name = layer.get("name")
        factor = ((layer.get("unit_measure") or {}).get("d_factor")) or 10
        values = []
        for depth in layer.get("depths") or []:
            mean = (depth.get("values") or {}).get("mean")
            if isinstance(mean, (int, float)) and not isinstance(mean, bool):
                values.append(mean / factor)
        if name and values:
            means[name] = sum(values) / len(values)
    return means


def parse_soil_payload(payload: Any) -> Optional[Dict[str, Any]]:
    means = _layer_means(payload)
    if not all(key in means for key in ("clay", "sand", "silt")):
        return None
    # g/kg -> % (d_factor 10); renormalise so the three fractions add up to 100
    clay, sand, silt = means["clay"], means["sand"], means["silt"]
    total = clay + sand + silt
    if total <= 0:
        return None
    clay, sand, silt = (v * 100 / total for v in (clay, sand, silt))
    texture = usda_texture(clay, sand, silt)
    profile: Dict[str, Any] = {
        "texture": texture,
        "drainage": drainage_class(texture),
        "clay_pct": round(clay),
        "sand_pct": round(sand),
        "silt_pct": round(silt),
        "source": "SoilGrids",
    }
    if "phh2o" in means and 3 <= means["phh2o"] <= 10:
        ph = round(means["phh2o"], 1)
        profile.update({"ph": ph, "ph_class": ph_class(ph), "calcareous": ph >= CALCAREOUS_PH})
    return profile


def fetch_soil_profile(latitude: float, longitude: float, timeout: int = 30) -> Optional[Dict[str, Any]]:
    params = [("lon", longitude), ("lat", latitude), ("value", "mean")]
    params += [("property", p) for p in SOIL_PROPERTIES] + [("depth", d) for d in SOIL_DEPTHS]
    try:
        response = requests.get(SOILGRIDS_URL, params=params, timeout=timeout)
        response.raise_for_status()
        return parse_soil_payload(response.json())
    except Exception as exc:
        print(f"[WARN] Soil lookup failed: {type(exc).__name__}")
        return None


def ensure_soil_profile(loc_store: Dict[str, Any], latitude: float, longitude: float, fetch: Any = fetch_soil_profile) -> Optional[Dict[str, Any]]:
    """Return the cached profile, fetching it once (soil does not change); a failed lookup is retried next run."""
    cached = loc_store.get("soil")
    if isinstance(cached, dict) and cached.get("texture"):
        return cached
    profile = fetch(latitude, longitude)
    if profile:
        loc_store["soil"] = profile
    return profile


def describe_soil(profile: Optional[Dict[str, Any]]) -> str:
    if not profile:
        return ""
    text = f"{profile['texture']} soil ({profile['drainage']})"
    if profile.get("ph") is not None:
        text += f", pH {profile['ph']:.1f} ({profile['ph_class']})"
    return text[0].upper() + text[1:]
