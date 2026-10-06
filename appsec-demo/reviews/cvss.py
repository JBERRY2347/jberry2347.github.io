"""CVSS v3.1 base score calculator (FIRST specification, section 7)."""
import math

WEIGHTS = {
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2},
    "AC": {"L": 0.77, "H": 0.44},
    "UI": {"N": 0.85, "R": 0.62},
    "CIA": {"H": 0.56, "L": 0.22, "N": 0.0},
}


def roundup(value):
    scaled = round(value * 100000)
    return scaled / 100000.0 if scaled % 10000 == 0 else (math.floor(scaled / 10000) + 1) / 10.0


def base_score(vector):
    parts = dict(p.split(":") for p in vector.split("/") if not p.startswith("CVSS"))
    changed = parts["S"] == "C"
    pr = {"N": 0.85, "L": 0.68 if changed else 0.62, "H": 0.5 if changed else 0.27}[parts["PR"]]
    iss = 1 - (1 - WEIGHTS["CIA"][parts["C"]]) * (1 - WEIGHTS["CIA"][parts["I"]]) * (1 - WEIGHTS["CIA"][parts["A"]])
    impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15 if changed else 6.42 * iss
    exploit = 8.22 * WEIGHTS["AV"][parts["AV"]] * WEIGHTS["AC"][parts["AC"]] * pr * WEIGHTS["UI"][parts["UI"]]
    if impact <= 0:
        return 0.0
    return roundup(min(1.08 * (impact + exploit), 10)) if changed else roundup(min(impact + exploit, 10))


def severity(score):
    if score is None or score == 0:
        return "Info"
    return "Critical" if score >= 9.0 else "High" if score >= 7.0 else "Medium" if score >= 4.0 else "Low"
