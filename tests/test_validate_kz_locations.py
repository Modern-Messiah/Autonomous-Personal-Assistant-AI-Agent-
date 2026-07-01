"""Tests for the reproducible KATO catalog validator."""

from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from agent.locations import CatalogMetadata, City, LocationCatalog
from scripts.validate_kz_locations import compare_catalog_to_kato


def build_kato_fixture(
    path: Path,
    *,
    rows: list[tuple[str, str, str, str]],
) -> Path:
    """Write a minimal XLSX containing code, kind, Kazakh and Russian names."""
    headers = ["te", "ab", "cd", "ef", "hij", "k", "kaz_name", "rus_name"]
    data_rows = [
        [code, code[:2], "00", "00", "000", kind, kk_name, ru_name]
        for code, kind, kk_name, ru_name in rows
    ]
    values = headers + [value for row in data_rows for value in row]
    shared = "".join(f"<si><t>{value}</t></si>" for value in values)
    sheet_rows = []
    cursor = len(headers)
    header_cells = "".join(
        f'<c r="{chr(65 + index)}1" t="s"><v>{index}</v></c>'
        for index in range(len(headers))
    )
    sheet_rows.append(f'<row r="1">{header_cells}</row>')
    for row_number, row_values in enumerate(data_rows, start=2):
        cells = "".join(
            f'<c r="{chr(65 + index)}{row_number}" t="s"><v>{cursor + index}</v></c>'
            for index in range(len(row_values))
        )
        cursor += len(row_values)
        sheet_rows.append(f'<row r="{row_number}">{cells}</row>')

    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "xl/sharedStrings.xml",
            (
                '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f"{shared}</sst>"
            ),
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            (
                '<worksheet xmlns="http://schemas.openxmlformats.org/'
                'spreadsheetml/2006/main"><sheetData>'
                f"{''.join(sheet_rows)}</sheetData></worksheet>"
            ),
        )
    return path


def build_catalog(*, code: str = "101010000") -> LocationCatalog:
    return LocationCatalog(
        metadata=CatalogMetadata(
            kato_version="test",
            updated_at="2026-06-18",
            source_url="test",
            source_sha256="",
        ),
        cities=(
            City(
                kato_code=code,
                canonical="Semei",
                name_ru="Семей",
                name_kk="Семей",
                aliases=("Semei",),
                krisha_slug="semej",
                districts=(),
            ),
        ),
    )


def test_validator_accepts_matching_city_fixture(tmp_path: Path) -> None:
    xlsx = build_kato_fixture(
        tmp_path / "kato.xlsx",
        rows=[("101010000", "1", "Семей қ.", "\u0433.Семей")],
    )

    assert compare_catalog_to_kato(build_catalog(), xlsx) == []


def test_validator_detects_missing_official_city(tmp_path: Path) -> None:
    xlsx = build_kato_fixture(
        tmp_path / "kato.xlsx",
        rows=[
            ("101010000", "1", "Семей қ.", "\u0433.Семей"),
            ("101810000", "1", "Курчатов қ.", "\u0433.Курчатов"),
        ],
    )

    problems = compare_catalog_to_kato(build_catalog(), xlsx)

    assert problems == ["missing official city KATO=101810000 name=Курчатов"]
