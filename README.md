# Kobo Font Converter

Convert modern kerning (`GPOS`) to legacy kerning (`kern`) for better Kobo `kepub` rendering.

This tool is made for practical Kobo use:
- check if fonts are Kobo-compatible,
- generate legacy `kern` tables,
- rename families (to avoid Kobo font collisions),
- normalize style metadata (to improve Regular/Bold/Italic matching).

## Why this exists

Many fonts only ship kerning in `GPOS`. On Kobo firmware/rendering paths, `kepub` often behaves better when a legacy `kern` table exists.

This project automates that conversion and adds Kobo-friendly metadata cleanup.

## Quick Start

### 1) Setup

Requirements:
- Python 3.10+
- [uv](https://github.com/astral-sh/uv) (recommended)

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e .
```

### 2) Inspect fonts

```bash
kobo-font-converter inspect "/path/to/fonts/static/*.ttf"
```

### 3) Convert fonts

```bash
kobo-font-converter convert \
  /path/to/fonts/static/Vollkorn-Regular.ttf \
  /path/to/fonts/static/Vollkorn-Italic.ttf \
  /path/to/fonts/static/Vollkorn-Bold.ttf \
  /path/to/fonts/static/Vollkorn-BoldItalic.ttf \
  --output-dir /path/to/output/static \
  --family-name "Vollkorn Kobo" \
  --overwrite
```

## Commands

### `inspect`

Check Kobo compatibility for one or more fonts.

```bash
kobo-font-converter inspect "/path/to/fonts/*.ttf"
kobo-font-converter inspect "/path/to/fonts/*.ttf" --json
```

### `convert`

Convert fonts by writing legacy `kern` from `GPOS` kerning and optionally applying Kobo metadata fixes.

```bash
kobo-font-converter convert "/path/to/fonts/*.ttf" --output-dir out
```

## Options

- `--family-name "..."`: set internal font family name (recommended for Kobo to avoid old/new family collisions).
- `--no-fix-metadata`: skip metadata normalization.
- `--overwrite`: replace existing output files.
- `--suffix -kobo`: add filename suffix.
- `--force-rebuild-kern`: rebuild `kern` even if already present.
- `--remove-gpos`: remove `GPOS` after conversion (Kobo-only preference).
- `--max-pairs-per-subtable 10000`: chunk size for legacy `kern` subtables.
- `--fail-if-no-gpos-kern`: fail conversion if no `GPOS` kerning can be extracted.
- `--report-json report.json`: save machine-readable conversion results.

## What metadata normalization does

By default, conversion also normalizes style metadata based on filename:
- style names (`Regular`, `Italic`, `Bold`, `Bold Italic`),
- weight and style flags,
- key PANOSE fields,
- WWS name cleanup.

This improves Kobo style linking so bold/italic text is less likely to be synthesized incorrectly.

## Behavior details

- The converter reads `GPOS` kerning from `kern` feature lookups.
- It supports both direct PairPos and extension-wrapped PairPos lookups.
- It writes legacy `kern` in chunks (default 10,000 pairs/subtable) for device compatibility.

## Recommended Kobo workflow

1. Prefer static 4-face families (`Regular`, `Italic`, `Bold`, `BoldItalic`) over variable fonts.
2. Convert with a unique family name (for example, `Vollkorn Kobo`).
3. Copy converted files to Kobo `fonts/`.
4. Remove old family variants from Kobo.
5. Reboot Kobo.

## Limitations

- Legacy `kern` can increase file size significantly.
- Legacy `kern` supports simpler horizontal pair spacing only.
- Not every advanced OpenType positioning behavior can be represented.

## License

GNU General Public License v3.0 (GPL-3.0).
