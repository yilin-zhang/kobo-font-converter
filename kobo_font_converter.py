#!/usr/bin/env python3
"""Kobo font converter.

Creates legacy 'kern' tables from modern GPOS kerning so fonts work better on
Kobo kepub rendering engines.
"""

from __future__ import annotations

import argparse
import glob as pyglob
import json
import re
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables._k_e_r_n import KernTable_format_0

Pair = Tuple[str, str]


@dataclass(frozen=True)
class StyleSpec:
    name: str
    weight: int
    is_bold: bool
    is_italic: bool
    panose_weight: int
    panose_letterform: int


STYLE_SPECS = {
    "bold_italic": StyleSpec("Bold Italic", 700, True, True, 8, 3),
    "bold": StyleSpec("Bold", 700, True, False, 8, 2),
    "italic": StyleSpec("Italic", 400, False, True, 5, 3),
    "regular": StyleSpec("Regular", 400, False, False, 5, 2),
}


@dataclass
class InspectResult:
    path: str
    has_gpos: bool
    has_kern: bool
    kern_subtables: int
    kern_pairs: int
    kobo_compatible: bool


@dataclass
class ConvertResult:
    source: str
    output: str
    status: str
    message: str
    existing_kern_pairs: int = 0
    extracted_gpos_pairs: int = 0
    written_kern_pairs: int = 0
    written_kern_subtables: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="kobo-font-converter",
        description="Inspect and convert fonts to include Kobo-friendly legacy 'kern' kerning.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    inspect = sub.add_parser("inspect", help="Inspect font kerning compatibility")
    inspect.add_argument("inputs", nargs="+", help="Font file paths or glob patterns")
    inspect.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    convert = sub.add_parser("convert", help="Generate Kobo-compatible fonts")
    convert.add_argument("inputs", nargs="+", help="Font file paths or glob patterns")
    convert.add_argument(
        "--output-dir",
        default="out",
        help="Directory for converted files (default: out)",
    )
    convert.add_argument(
        "--family-name",
        help="Optional font family name override (e.g. 'Vollkorn Kobo')",
    )
    convert.add_argument(
        "--no-fix-metadata",
        action="store_true",
        help="Skip Kobo-oriented style/weight/PANOSE metadata normalization.",
    )
    convert.add_argument(
        "--output-name",
        help=(
            "Output filename base for style set (e.g. YZ_CrimsonPro -> "
            "YZ_CrimsonPro-Regular.ttf, YZ_CrimsonPro-Bold.ttf)"
        ),
    )
    convert.add_argument(
        "--size-scale",
        type=float,
        default=1.0,
        help="Overall font optical scale factor (default: 1.0)",
    )
    convert.add_argument(
        "--line-gap-percent",
        type=float,
        help="Set line gap to percent of UPM (e.g. 20 for 20%%).",
    )
    convert.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow overwriting existing files in output directory",
    )
    convert.add_argument(
        "--force-rebuild-kern",
        action="store_true",
        help="Rebuild legacy 'kern' even if font already has one",
    )
    convert.add_argument(
        "--remove-gpos",
        action="store_true",
        help="Remove GPOS table after writing legacy 'kern' (Kobo-only preference)",
    )
    convert.add_argument(
        "--max-pairs-per-subtable",
        type=int,
        default=10000,
        help="Maximum kerning pairs per legacy kern subtable (default: 10000)",
    )
    convert.add_argument(
        "--fail-if-no-gpos-kern",
        action="store_true",
        help="Fail conversion for fonts where no GPOS kern pairs can be extracted",
    )
    convert.add_argument(
        "--report-json",
        help="Optional JSON report path for conversion results",
    )

    return parser.parse_args()


def expand_inputs(inputs: List[str]) -> List[Path]:
    files: List[Path] = []
    seen = set()
    for raw in inputs:
        p = Path(raw)
        if any(ch in raw for ch in "*?[]"):
            matches = [Path(m) for m in sorted(pyglob.glob(raw, recursive=True))]
        elif p.is_dir():
            matches = sorted(p.rglob("*.ttf"))
        else:
            matches = [p]

        for m in matches:
            if m.is_file() and m.suffix.lower() in {".ttf", ".otf"}:
                resolved = m.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    files.append(resolved)
    return files


def legacy_kern_stats(font: TTFont) -> Tuple[int, int]:
    if "kern" not in font:
        return 0, 0
    subtables = getattr(font["kern"], "kernTables", [])
    pair_count = sum(len(getattr(st, "kernTable", {})) for st in subtables)
    return len(subtables), pair_count


def inspect_one(path: Path) -> InspectResult:
    font = TTFont(str(path))
    try:
        kern_subtables, kern_pairs = legacy_kern_stats(font)
        return InspectResult(
            path=str(path),
            has_gpos="GPOS" in font,
            has_kern="kern" in font,
            kern_subtables=kern_subtables,
            kern_pairs=kern_pairs,
            kobo_compatible=kern_pairs > 0,
        )
    finally:
        font.close()


def _kern_lookup_indices(gpos_table) -> List[int]:
    feature_list = getattr(gpos_table, "FeatureList", None)
    if not feature_list:
        return []

    indices: List[int] = []
    for feature_record in feature_list.FeatureRecord:
        if feature_record.FeatureTag == "kern":
            indices.extend(feature_record.Feature.LookupListIndex)
    return sorted(set(indices))


def _iter_pairpos_subtables(lookup) -> Iterable:
    for subtable in lookup.SubTable:
        if lookup.LookupType == 2:
            yield subtable
        elif lookup.LookupType == 9 and getattr(subtable, "ExtensionLookupType", None) == 2:
            yield subtable.ExtSubTable


def _kerning_value(value1, value2=None) -> int:
    xadv = getattr(value1, "XAdvance", 0) if value1 is not None else 0
    if xadv:
        return int(xadv)
    return 0


def _extract_pairs_from_pairpos_subtable(font: TTFont, pairpos_subtable) -> Dict[Pair, int]:
    extracted: Dict[Pair, int] = {}
    fmt = getattr(pairpos_subtable, "Format", None)

    if fmt == 1:
        first_glyphs = pairpos_subtable.Coverage.glyphs
        for first_glyph, pair_set in zip(first_glyphs, pairpos_subtable.PairSet):
            for rec in pair_set.PairValueRecord:
                value = _kerning_value(rec.Value1, rec.Value2)
                if value:
                    extracted[(first_glyph, rec.SecondGlyph)] = value
        return extracted

    if fmt == 2:
        first_coverage = pairpos_subtable.Coverage.glyphs
        class1_defs = pairpos_subtable.ClassDef1.classDefs
        class2_defs = pairpos_subtable.ClassDef2.classDefs

        class1_to_glyphs: Dict[int, List[str]] = {i: [] for i in range(pairpos_subtable.Class1Count)}
        for gname in first_coverage:
            class_id = class1_defs.get(gname, 0)
            if class_id < pairpos_subtable.Class1Count:
                class1_to_glyphs[class_id].append(gname)

        class2_to_glyphs: Dict[int, List[str]] = {i: [] for i in range(pairpos_subtable.Class2Count)}
        for gname in font.getGlyphOrder():
            class_id = class2_defs.get(gname, 0)
            if class_id < pairpos_subtable.Class2Count:
                class2_to_glyphs[class_id].append(gname)

        for class1_index in range(pairpos_subtable.Class1Count):
            first_group = class1_to_glyphs.get(class1_index, [])
            if not first_group:
                continue

            class1_record = pairpos_subtable.Class1Record[class1_index]
            for class2_index in range(pairpos_subtable.Class2Count):
                second_group = class2_to_glyphs.get(class2_index, [])
                if not second_group:
                    continue

                class2_record = class1_record.Class2Record[class2_index]
                value = _kerning_value(class2_record.Value1, class2_record.Value2)
                if not value:
                    continue

                for g1 in first_group:
                    for g2 in second_group:
                        extracted[(g1, g2)] = value

    return extracted


def extract_kern_pairs_from_gpos(font: TTFont) -> Dict[Pair, int]:
    if "GPOS" not in font:
        return {}

    gpos = font["GPOS"].table
    lookup_list = getattr(gpos, "LookupList", None)
    if not lookup_list:
        return {}

    lookup_indices = _kern_lookup_indices(gpos)
    if not lookup_indices:
        return {}

    merged: Dict[Pair, int] = {}
    for lookup_index in lookup_indices:
        lookup = lookup_list.Lookup[lookup_index]
        for subtable in _iter_pairpos_subtables(lookup):
            merged.update(_extract_pairs_from_pairpos_subtable(font, subtable))

    return {pair: value for pair, value in merged.items() if value}


def add_legacy_kern(font: TTFont, kern_pairs: Dict[Pair, int], max_pairs_per_subtable: int) -> Tuple[int, int]:
    if not kern_pairs:
        return 0, 0

    kern_table = newTable("kern")
    kern_table.version = 0
    kern_table.kernTables = []

    items = [(pair, int(value)) for pair, value in kern_pairs.items() if value]
    items.sort()

    for i in range(0, len(items), max_pairs_per_subtable):
        chunk = dict(items[i : i + max_pairs_per_subtable])
        subtable = KernTable_format_0()
        subtable.version = 0
        subtable.length = None
        subtable.coverage = 1
        subtable.kernTable = chunk
        kern_table.kernTables.append(subtable)

    font["kern"] = kern_table
    return len(items), len(kern_table.kernTables)


def _scale_int(value: int, factor: float) -> int:
    return int(round(value * factor))


def apply_optical_scale(font: TTFont, factor: float) -> None:
    if abs(factor - 1.0) < 1e-9:
        return

    if "glyf" in font:
        glyf = font["glyf"]
        for glyph in glyf.glyphs.values():
            glyph.expand(glyf)
            if glyph.isComposite():
                for comp in getattr(glyph, "components", []):
                    comp.x = _scale_int(comp.x, factor)
                    comp.y = _scale_int(comp.y, factor)
            elif getattr(glyph, "numberOfContours", 0) > 0 and hasattr(glyph, "coordinates"):
                glyph.coordinates.scale((factor, factor))
                glyph.coordinates.toInt()

    if "hmtx" in font:
        hmtx = font["hmtx"].metrics
        for gname, (adv, lsb) in list(hmtx.items()):
            hmtx[gname] = (_scale_int(adv, factor), _scale_int(lsb, factor))

    if "hhea" in font:
        hhea = font["hhea"]
        for field in (
            "ascent",
            "descent",
            "lineGap",
            "advanceWidthMax",
            "minLeftSideBearing",
            "minRightSideBearing",
            "xMaxExtent",
            "caretOffset",
        ):
            if hasattr(hhea, field):
                setattr(hhea, field, _scale_int(getattr(hhea, field), factor))

    if "head" in font:
        head = font["head"]
        for field in ("xMin", "yMin", "xMax", "yMax"):
            if hasattr(head, field):
                setattr(head, field, _scale_int(getattr(head, field), factor))

    if "OS/2" in font:
        os2 = font["OS/2"]
        for field in (
            "xAvgCharWidth",
            "ySubscriptXSize",
            "ySubscriptYSize",
            "ySubscriptXOffset",
            "ySubscriptYOffset",
            "ySuperscriptXSize",
            "ySuperscriptYSize",
            "ySuperscriptXOffset",
            "ySuperscriptYOffset",
            "yStrikeoutSize",
            "yStrikeoutPosition",
            "sTypoAscender",
            "sTypoDescender",
            "sTypoLineGap",
            "usWinAscent",
            "usWinDescent",
            "sxHeight",
            "sCapHeight",
        ):
            if hasattr(os2, field):
                setattr(os2, field, _scale_int(getattr(os2, field), factor))

    if "post" in font:
        post = font["post"]
        for field in ("underlinePosition", "underlineThickness"):
            if hasattr(post, field):
                setattr(post, field, _scale_int(getattr(post, field), factor))

    if "kern" in font:
        for subtable in getattr(font["kern"], "kernTables", []):
            table = getattr(subtable, "kernTable", None)
            if table:
                for pair, value in list(table.items()):
                    table[pair] = _scale_int(value, factor)


def apply_line_gap_percent(font: TTFont, percent: float) -> None:
    upm = int(font["head"].unitsPerEm) if "head" in font else 1000
    gap = int(round(upm * percent / 100.0))

    if "hhea" in font:
        font["hhea"].lineGap = gap

    if "OS/2" in font:
        os2 = font["OS/2"]
        if hasattr(os2, "sTypoLineGap"):
            os2.sTypoLineGap = gap


def _first_name(font: TTFont, name_ids: List[int]) -> str:
    name_table = font["name"]
    preferred = sorted(
        name_table.names,
        key=lambda r: (0 if r.platformID == 3 else 1, 0 if r.langID == 0x409 else 1),
    )
    for record in preferred:
        if record.nameID in name_ids:
            value = record.toUnicode().strip()
            if value:
                return value
    return ""


def _set_or_add_name_records(name_table, name_id: int, value: str) -> None:
    touched = False
    for record in list(name_table.names):
        if record.nameID == name_id:
            name_table.setName(value, name_id, record.platformID, record.platEncID, record.langID)
            touched = True
    if not touched:
        name_table.setName(value, name_id, 3, 1, 0x409)
        name_table.setName(value, name_id, 1, 0, 0)


def _postscript_safe(value: str) -> str:
    collapsed = re.sub(r"\s+", "", value)
    return re.sub(r"[^A-Za-z0-9-]", "", collapsed)


def infer_style_spec(path: Path) -> StyleSpec:
    name = path.stem.lower().replace("-", "").replace("_", "")
    if "bolditalic" in name:
        return STYLE_SPECS["bold_italic"]
    if "bold" in name:
        return STYLE_SPECS["bold"]
    if "italic" in name:
        return STYLE_SPECS["italic"]
    return STYLE_SPECS["regular"]


def style_filename_token(style_spec: StyleSpec) -> str:
    return style_spec.name.replace(" ", "")


def remove_wws_name_records(font: TTFont) -> None:
    if "name" not in font:
        return
    name_table = font["name"]
    name_table.names = [n for n in name_table.names if n.nameID not in {21, 22}]


def normalize_style_metadata(font: TTFont, style_spec: StyleSpec) -> None:
    if "name" in font:
        name_table = font["name"]
        _set_or_add_name_records(name_table, 2, style_spec.name)
        _set_or_add_name_records(name_table, 17, style_spec.name)

    if "OS/2" in font:
        os2 = font["OS/2"]
        os2.usWeightClass = style_spec.weight

        # fsSelection bits: 0 italic, 5 bold, 6 regular
        fs = int(getattr(os2, "fsSelection", 0))
        fs &= ~((1 << 0) | (1 << 5) | (1 << 6))
        if style_spec.is_italic:
            fs |= 1 << 0
        if style_spec.is_bold:
            fs |= 1 << 5
        if not style_spec.is_bold and not style_spec.is_italic:
            fs |= 1 << 6
        os2.fsSelection = fs

        panose = getattr(os2, "panose", None)
        if panose is not None:
            panose.bWeight = style_spec.panose_weight
            panose.bLetterForm = style_spec.panose_letterform

    if "head" in font:
        head = font["head"]
        mac_style = int(getattr(head, "macStyle", 0))
        mac_style &= ~((1 << 0) | (1 << 1))
        if style_spec.is_bold:
            mac_style |= 1 << 0
        if style_spec.is_italic:
            mac_style |= 1 << 1
        head.macStyle = mac_style


def rename_family(font: TTFont, family_name: str) -> None:
    style_name = _first_name(font, [17, 2]) or "Regular"
    full_name = family_name if style_name.lower() == "regular" else f"{family_name} {style_name}"
    ps_name = f"{_postscript_safe(family_name)}-{_postscript_safe(style_name)}"
    unique_id = f"{full_name};kobo-font-converter"

    name_table = font["name"]
    _set_or_add_name_records(name_table, 1, family_name)
    _set_or_add_name_records(name_table, 16, family_name)
    _set_or_add_name_records(name_table, 4, full_name)
    _set_or_add_name_records(name_table, 6, ps_name)
    _set_or_add_name_records(name_table, 3, unique_id)

    if not _first_name(font, [17]):
        _set_or_add_name_records(name_table, 17, style_name)


def output_path_for(
    source: Path,
    output_dir: Path,
    output_name: str | None = None,
    style_spec: StyleSpec | None = None,
) -> Path:
    if output_name and style_spec is not None:
        stem = f"{output_name}-{style_filename_token(style_spec)}"
    else:
        stem = source.stem
    return output_dir / f"{stem}{source.suffix}"


def convert_one(path: Path, args: argparse.Namespace) -> ConvertResult:
    style_spec = infer_style_spec(path)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_path_for(
        path,
        output_dir,
        args.output_name,
        style_spec,
    )

    if out_path.exists() and not args.overwrite:
        return ConvertResult(
            source=str(path),
            output=str(out_path),
            status="fail",
            message="Output file exists (use --overwrite).",
        )

    font = TTFont(str(path))
    try:
        if not args.no_fix_metadata:
            remove_wws_name_records(font)
            normalize_style_metadata(font, style_spec)

        existing_subtables, existing_pairs = legacy_kern_stats(font)

        should_copy_directly = (
            existing_pairs > 0
            and not args.force_rebuild_kern
            and not args.family_name
            and args.no_fix_metadata
            and not args.remove_gpos
            and not args.output_name
            and abs(args.size_scale - 1.0) < 1e-9
            and args.line_gap_percent is None
        )
        if should_copy_directly:
            shutil.copy2(path, out_path)
            return ConvertResult(
                source=str(path),
                output=str(out_path),
                status="copied",
                message="Legacy kern already present; copied source unchanged.",
                existing_kern_pairs=existing_pairs,
                written_kern_pairs=existing_pairs,
                written_kern_subtables=existing_subtables,
            )

        extracted: Dict[Pair, int] = {}
        written_pairs = existing_pairs
        written_subtables = existing_subtables

        if existing_pairs == 0 or args.force_rebuild_kern:
            extracted = extract_kern_pairs_from_gpos(font)
            if not extracted:
                if args.fail_if_no_gpos_kern:
                    return ConvertResult(
                        source=str(path),
                        output=str(out_path),
                        status="fail",
                        message="No GPOS kern pairs found.",
                        existing_kern_pairs=existing_pairs,
                    )

                if (
                    not args.family_name
                    and not args.remove_gpos
                    and args.no_fix_metadata
                    and not args.output_name
                    and abs(args.size_scale - 1.0) < 1e-9
                    and args.line_gap_percent is None
                ):
                    shutil.copy2(path, out_path)
                    return ConvertResult(
                        source=str(path),
                        output=str(out_path),
                        status="copied",
                        message="No GPOS kern pairs found; copied source unchanged.",
                        existing_kern_pairs=existing_pairs,
                    )
            else:
                written_pairs, written_subtables = add_legacy_kern(
                    font,
                    extracted,
                    max_pairs_per_subtable=args.max_pairs_per_subtable,
                )

        if args.family_name:
            rename_family(font, args.family_name)

        if args.remove_gpos and "GPOS" in font:
            del font["GPOS"]

        apply_optical_scale(font, args.size_scale)

        if args.line_gap_percent is not None:
            apply_line_gap_percent(font, args.line_gap_percent)

        font.save(str(out_path))

        return ConvertResult(
            source=str(path),
            output=str(out_path),
            status="ok",
            message="Legacy kern generated successfully.",
            existing_kern_pairs=existing_pairs,
            extracted_gpos_pairs=len(extracted),
            written_kern_pairs=written_pairs,
            written_kern_subtables=written_subtables,
        )
    finally:
        font.close()


def run_inspect(args: argparse.Namespace) -> int:
    paths = expand_inputs(args.inputs)
    if not paths:
        print("No font files found.", file=sys.stderr)
        return 2

    results = [inspect_one(p) for p in paths]

    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2))
        return 0

    print(f"Found {len(results)} font(s).")
    for r in results:
        compat = "yes" if r.kobo_compatible else "no"
        print(
            f"- {r.path}: kobo_compatible={compat}, "
            f"kern_pairs={r.kern_pairs}, kern_subtables={r.kern_subtables}, has_GPOS={r.has_gpos}"
        )
    return 0


def run_convert(args: argparse.Namespace) -> int:
    paths = expand_inputs(args.inputs)
    if not paths:
        print("No font files found.", file=sys.stderr)
        return 2

    results = [convert_one(p, args) for p in paths]
    for r in results:
        print(
            f"[{r.status.upper():6}] {Path(r.source).name} -> {Path(r.output).name} | "
            f"{r.message}"
        )
        if r.status == "ok":
            print(
                f"         existing_kern={r.existing_kern_pairs}, "
                f"extracted_gpos={r.extracted_gpos_pairs}, "
                f"written_kern={r.written_kern_pairs} in {r.written_kern_subtables} subtables"
            )

    if args.report_json:
        report_path = Path(args.report_json)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")

    failed = sum(1 for r in results if r.status == "fail")
    ok = sum(1 for r in results if r.status == "ok")
    copied = sum(1 for r in results if r.status == "copied")
    print("-" * 72)
    print(f"Processed {len(results)} font(s): {ok} converted, {copied} copied, {failed} failed")
    return 1 if failed else 0


def main() -> int:
    args = parse_args()
    if args.command == "inspect":
        return run_inspect(args)
    if args.command == "convert":
        return run_convert(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
