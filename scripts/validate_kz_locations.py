"""Compare the checked-in Kazakhstan location catalog with KATO and Krisha."""

from __future__ import annotations

import argparse
import hashlib
import re
import time
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import httpx

from agent.locations import LOCATIONS, LocationCatalog

_XML_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_CITY_PREFIX = re.compile(r"^\u0433\.(?!\u0430\.)", re.IGNORECASE)


def _xlsx_rows(path: Path) -> list[dict[str, str]]:
    with ZipFile(path) as archive:
        shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        shared = [
            "".join(node.text or "" for node in item.findall(".//m:t", _XML_NS))
            for item in shared_root.findall("m:si", _XML_NS)
        ]
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))

    raw_rows: list[list[str]] = []
    for row in sheet.findall(".//m:sheetData/m:row", _XML_NS):
        values: list[str] = []
        for cell in row.findall("m:c", _XML_NS):
            value_node = cell.find("m:v", _XML_NS)
            raw = "" if value_node is None else value_node.text or ""
            value = shared[int(raw)] if cell.get("t") == "s" and raw else raw
            values.append(value)
        raw_rows.append(values)

    if not raw_rows:
        return []
    headers = raw_rows[0]
    return [
        dict(zip(headers, values, strict=False))
        for values in raw_rows[1:]
        if values
    ]


def _city_name(value: str) -> str:
    return _CITY_PREFIX.sub("", value).strip()


def _kazakh_city_name(value: str) -> str:
    return re.sub(r"\s+(?:қ|ќ)\.$", "", value, flags=re.IGNORECASE).strip()


def _is_official_city(row: dict[str, str]) -> bool:
    return _CITY_PREFIX.match(row.get("rus_name", "")) is not None


def _is_city_district(row: dict[str, str]) -> bool:
    if row.get("k") != "1" or _is_official_city(row):
        return False
    return (
        "район" in row.get("rus_name", "").casefold()
        or "аудан" in row.get("kaz_name", "").casefold()
    )


def compare_catalog_to_kato(
    catalog: LocationCatalog,
    xlsx_path: Path,
) -> list[str]:
    """Return stable human-readable differences between catalog and KATO."""
    rows = _xlsx_rows(xlsx_path)
    official_cities = {row["te"]: row for row in rows if _is_official_city(row)}
    official_districts = {row["te"]: row for row in rows if _is_city_district(row)}
    catalog_cities = {city.kato_code: city for city in catalog.cities}
    catalog_districts = {
        district.kato_code: district
        for city in catalog.cities
        for district in city.districts
    }

    problems: list[str] = []
    for code in sorted(official_cities.keys() - catalog_cities.keys()):
        name = _city_name(official_cities[code]["rus_name"])
        problems.append(f"missing official city KATO={code} name={name}")
    for code in sorted(catalog_cities.keys() - official_cities.keys()):
        problems.append(
            f"catalog city absent from KATO KATO={code} "
            f"name={catalog_cities[code].name_ru}"
        )
    for code in sorted(official_districts.keys() - catalog_districts.keys()):
        name = official_districts[code]["rus_name"]
        problems.append(f"missing official city district KATO={code} name={name}")
    for code in sorted(catalog_districts.keys() - official_districts.keys()):
        problems.append(
            f"catalog district absent from KATO KATO={code} "
            f"name={catalog_districts[code].name_ru}"
        )

    for code in sorted(official_cities.keys() & catalog_cities.keys()):
        official = official_cities[code]
        city = catalog_cities[code]
        official_ru = _city_name(official["rus_name"])
        official_kk = _kazakh_city_name(official["kaz_name"])
        if (city.name_ru, city.name_kk) != (official_ru, official_kk):
            problems.append(
                f"city name mismatch KATO={code} "
                f"catalog=({city.name_ru}, {city.name_kk}) "
                f"official=({official_ru}, {official_kk})"
            )
    return problems


def audit_krisha_urls(
    catalog: LocationCatalog,
    *,
    delay_seconds: float,
) -> list[str]:
    """Check each searchable city URL at a deliberately low request rate."""
    problems: list[str] = []
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36"
        )
    }
    with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client:
        for city in catalog.cities:
            if city.krisha_slug is None:
                continue
            url = f"https://krisha.kz/prodazha/kvartiry/{city.krisha_slug}/"
            response = client.get(url)
            lowered = response.text.casefold()
            if response.status_code == 429:
                problems.append(f"Krisha rate-limited city={city.canonical} status=429")
            elif response.status_code != 200:
                problems.append(
                    f"Krisha city URL failed city={city.canonical} "
                    f"status={response.status_code} url={url}"
                )
            elif "captcha" in lowered and "a-card" not in lowered:
                problems.append(f"Krisha anti-bot page city={city.canonical} url={url}")
            if delay_seconds > 0:
                time.sleep(delay_seconds)
    return problems


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kato-xlsx", type=Path, required=True)
    parser.add_argument("--audit-krisha", action="store_true")
    parser.add_argument("--delay-seconds", type=float, default=2.0)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    actual_sha = hashlib.sha256(args.kato_xlsx.read_bytes()).hexdigest()
    problems: list[str] = []
    if actual_sha != LOCATIONS.metadata.source_sha256:
        problems.append(
            "KATO SHA-256 differs from the pinned catalog snapshot: "
            f"expected={LOCATIONS.metadata.source_sha256} actual={actual_sha}"
        )
    problems.extend(compare_catalog_to_kato(LOCATIONS, args.kato_xlsx))
    if args.audit_krisha:
        problems.extend(
            audit_krisha_urls(
                LOCATIONS,
                delay_seconds=max(args.delay_seconds, 0),
            )
        )

    if problems:
        for problem in problems:
            print(f"ERROR: {problem}")
        return 1
    searchable = sum(city.krisha_slug is not None for city in LOCATIONS.cities)
    districts = sum(len(city.districts) for city in LOCATIONS.cities)
    print(
        f"OK: {len(LOCATIONS.cities)} cities, {districts} city districts, "
        f"{searchable} Krisha slugs"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
