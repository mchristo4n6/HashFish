# hashfish

**Extract MD5 hashes from PDF reports and archives.** A desktop and command-line
utility designed for forensic batch processing — point it at folders or archives
full of PDF reports, get back clean per-source hash files plus a deduplicated
master list, ready to import into Cellebrite Pathfinder, Magnet AXIOM, or any
hash-matching workflow.

![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Tests](https://img.shields.io/badge/tests-84%20passing-brightgreen)
![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)

---

## What it does

- Walks any combination of folders and archives looking for PDF reports.
- Extracts standalone 32-character hex strings (MD5 hashes) using a tight
  whitespace-boundary regex that won't false-positive on filenames or
  identifiers that happen to contain hash-like sequences.
- Writes one `<sourcename>_uniquehashes.txt` per source PDF, plus an aggregated
  `_master_hashes.txt` deduplicated across the entire run.
- Names outputs after the source (so `192131.pdf` becomes
  `192131_uniquehashes.txt`) for clean chain-of-custody attribution.

## Supported formats

| Source type | Status | Backed by |
|-------------|:------:|-----------|
| Folder (recursive scan) | ✅ | stdlib |
| `.zip` | ✅ | stdlib |
| `.tar`, `.tar.gz`, `.tar.bz2`, `.tar.xz`, `.tgz`, `.tbz2`, `.txz` | ✅ | stdlib |
| `.7z` | ⚙ optional | [`py7zr`](https://pypi.org/project/py7zr/) |
| `.rar` | ⚙ optional | [`rarfile`](https://pypi.org/project/rarfile/) + `unrar` |

Missing optional dependencies fail gracefully — the format is reported as
unsupported in the log without crashing the run.

## Quick start

### GUI

Download a pre-built executable from [Releases](../../releases), or run from source:

```bash
git clone https://github.com/YOUR-USERNAME/hashfish.git
cd hashfish
pip install -r requirements.txt
python hashfish.py
```

Add folders or archives (drag-and-drop if `tkinterdnd2` is installed, or via the
**Add** button), pick an output folder, click **Extract Hashes**.

### CLI

```bash
# Process a folder of PDFs
python hashfish.py -o output/ /cases/2024/inbox

# Mix folders and archives
python hashfish.py -o output/ /cases/2024 archives/old.zip evidence.tar.gz

# Quiet mode (errors and summary only)
python hashfish.py -o output/ -q /cases/inbox

# Force GUI even when sources are given
python hashfish.py --gui
```

Exit codes: `0` clean • `1` ran with per-source errors • `2` fatal (bad input).

## Build a standalone executable

```bash
# Windows
build.bat

# macOS / Linux
./build.sh
```

Output lands in `dist/hashfish[.exe]` — a single self-contained binary that
bundles Python and all required libraries. End users do not need Python
installed. Full instructions and platform notes in [BUILD.md](BUILD.md).

## Output convention

| Source                                          | Output filename                                  |
|-------------------------------------------------|--------------------------------------------------|
| `192131.pdf`                                    | `192131_uniquehashes.txt`                        |
| `bundle.zip` (one PDF inside)                   | `bundle_uniquehashes.txt`                        |
| `bundle.zip` (multiple PDFs inside)             | `bundle__<pdfname>_uniquehashes.txt`             |
| Aggregate across the whole run                  | `_master_hashes.txt`                             |

## Architecture

The code is layered so GUI and CLI consume the same extraction pipeline:

```
┌────────────────────────────┬────────────────────────────┐
│  GUI (HashExtractorApp)    │   CLI (run_cli)            │
│  thread-safe Tk via        │   stdout/stderr printer    │
│  root.after(0, …)          │   exit-code semantics      │
└─────────────┬──────────────┴────────────┬───────────────┘
              │                           │
              └────── process_sources ────┘
                     (event generator)
                            │
                  ┌─────────┴──────────┐
                  │ collect_pdf_sources│
                  │ _scan_folder       │
                  │ _read_archive      │
                  └─────────┬──────────┘
                            │
                  ┌─────────┴──────────┐
                  │ read_pdf_text      │
                  │ extract_hashes     │
                  │ (MD5_PATTERN regex)│
                  └────────────────────┘
```

Worker threads in the GUI never touch Tk widgets directly; every update is
marshalled onto the main thread, with graceful handling for the case where
the user closes the window mid-run.

## Testing

```bash
# With pytest
pip install -r requirements-test.txt
pytest tests/

# Without pytest (e.g. restricted environments)
python run_tests.py
```

**84 tests** cover the regex (positive matches + false-positive traps), path
classification, archive readers, folder scanning, end-to-end pipeline, and
CLI argument handling. A `test_fixtures.zip` is also included with sample
PDFs/archives and an `EXPECTED_RESULTS.txt` ground-truth document for manual
verification of built executables.

## Background

Built to streamline a specific forensic workflow: NCMEC CyberTipline reports
arrive as PDFs (sometimes individually, sometimes bundled in archives), and
the MD5 hashes embedded in them need to be loaded into hash-matching tools to
check against files on seized devices. Doing this by hand across dozens of
tips is tedious and error-prone. `hashfish` does the boring part fast and
correctly, with attribution preserved so analysts can trace any hit back to
the originating report.

The tool itself is content-agnostic — it works against any PDF that contains
standalone 32-character hex strings, regardless of the report format.

## License

[MIT](LICENSE)

        
<img width="1080" height="1008" alt="image" src="https://github.com/user-attachments/assets/5258e3cf-dd33-4b85-b7f7-4f94eabb459f" />
