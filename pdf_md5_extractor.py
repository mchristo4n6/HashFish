"""
PDF MD5 Hash Extractor
----------------------
Scans folders and/or archive files for PDFs, extracts every 32-character hex
string (MD5 hash) that stands alone (surrounded by whitespace), and writes:

  * One <pdfname>.txt per source PDF containing that PDF's unique hashes.
    For PDFs inside archives, the output is named after the archive (or
    <archive>__<pdfname>.txt if an archive holds multiple PDFs).
  * One _master_hashes.txt containing the deduplicated union of all hashes
    found across every PDF in the run.

Supported archive formats: .zip, .7z, .rar, .tar, .tar.gz / .tgz,
.tar.bz2 / .tbz2, .tar.xz / .txz. Only .pdf files and the listed archive
types are processed; all other file types in scanned folders are ignored.

Dependencies:
    Required:
        pip install pypdf
    Recommended (for drag-and-drop support):
        pip install tkinterdnd2
    Optional (for additional archive formats):
        pip install py7zr      # for .7z
        pip install rarfile    # for .rar (also requires the 'unrar' system tool)
"""

import argparse
import io
import os
import re
import sys
import tarfile
import threading
import zipfile
from pathlib import Path

# tkinter is only needed for GUI mode. On headless systems (e.g. CLI runs on a
# server) it may not be available; in that case we still want the CLI to work.
try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    HAS_TK = True
except ImportError:
    HAS_TK = False
    tk = None
    filedialog = messagebox = ttk = None

# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

# Hard dependency: pypdf (with PyPDF2 fallback for older environments)
try:
    from pypdf import PdfReader
except ImportError:
    try:
        from PyPDF2 import PdfReader
    except ImportError:
        raise SystemExit("Missing dependency. Install with: pip install pypdf")

# Optional: drag-and-drop support
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except ImportError:
    HAS_DND = False
    TkinterDnD = None
    DND_FILES = None

# Optional: 7-Zip support
try:
    import py7zr
    HAS_7Z = True
except ImportError:
    HAS_7Z = False

# Optional: RAR support
try:
    import rarfile
    HAS_RAR = True
except ImportError:
    HAS_RAR = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# 32 hex chars surrounded by whitespace (or string boundaries). Whitespace
# lookarounds avoid false positives from filenames/identifiers that embed
# hash-like sequences (e.g. report_a1b2...f6_v2.pdf).
MD5_PATTERN = re.compile(r"(?<![^\s])[a-fA-F0-9]{32}(?![^\s])")

MASTER_FILENAME = "_master_hashes.txt"

# Recognized archive extensions (checked case-insensitively). Listed roughly
# in order of specificity so the longest match wins for compound suffixes.
ARCHIVE_TYPES = [
    ("tar", (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz", ".tar")),
    ("zip", (".zip",)),
    ("7z",  (".7z",)),
    ("rar", (".rar",)),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _classify_path(p: Path):
    """Return (category, kind). Category is 'folder', 'archive', or 'skip'.

    For 'archive', kind is one of 'zip', '7z', 'rar', 'tar'.
    For 'skip', kind is a short human-readable reason.
    """
    try:
        if p.is_dir():
            return "folder", None
        if not p.is_file():
            return "skip", "not a file or folder"
    except OSError as e:
        return "skip", f"cannot stat: {e}"

    name = p.name.lower()
    if name.endswith(".pdf"):
        # PDFs are valid contents but not valid as top-level sources — they
        # should be discovered through a folder scan, not added directly.
        return "skip", "PDFs cannot be added directly — add the containing folder"
    for kind, exts in ARCHIVE_TYPES:
        if any(name.endswith(ext) for ext in exts):
            return "archive", kind
    return "skip", "unsupported file type"


def _archive_stem(arc_path: Path) -> str:
    """Like Path.stem, but strips compound archive extensions cleanly.

    Path.stem only removes the last suffix, so 'bundle.tar.gz'.stem returns
    'bundle.tar'. We want 'bundle'.
    """
    name = arc_path.name
    lower = name.lower()
    # Check longest extensions first so .tar.gz wins over .gz
    for kind, exts in ARCHIVE_TYPES:
        for ext in sorted(exts, key=len, reverse=True):
            if lower.endswith(ext):
                return name[: -len(ext)]
    return arc_path.stem


def _build_base(arc_path: Path, inner: str, multi: bool) -> str:
    """Output base = <arc-stem> for single-PDF archives, else <arc-stem>__<pdf-stem>."""
    arc_stem = _archive_stem(arc_path)
    if not multi:
        return arc_stem
    return f"{arc_stem}__{Path(inner).stem}"


def read_pdf_text(source):
    """Return (full_text, error_or_None) from a PDF.

    `source` may be a Path or a bytes object (PDF extracted from an archive).
    """
    try:
        if isinstance(source, (bytes, bytearray)):
            reader = PdfReader(io.BytesIO(source))
        else:
            reader = PdfReader(str(source))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                return None, "encrypted (skipped)"
        return "\n".join((p.extract_text() or "") for p in reader.pages), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def extract_hashes(text: str) -> set:
    """Return set of MD5 hashes (lowercase) found anywhere in text."""
    return {m.lower() for m in MD5_PATTERN.findall(text)}


# ---------------------------------------------------------------------------
# Archive readers — each returns a list of source dicts:
#   {'display': str, 'base': str, 'source': bytes | None, 'error': str | None}
# ---------------------------------------------------------------------------

def _err_item(arc_path: Path, error: str, inner: str = None, base: str = None):
    display = arc_path.name if inner is None else f"{arc_path.name}!{inner}"
    return {
        "display": display,
        "base": base or _archive_stem(arc_path),
        "source": None,
        "error": error,
    }


def _read_zip_archive(arc_path: Path):
    items = []
    try:
        with zipfile.ZipFile(arc_path) as zf:
            inner_pdfs = [
                n for n in zf.namelist()
                if n.lower().endswith(".pdf") and not n.endswith("/")
            ]
            if not inner_pdfs:
                return [_err_item(arc_path, "no PDFs inside zip")]
            multi = len(inner_pdfs) > 1
            for inner in sorted(inner_pdfs):
                base = _build_base(arc_path, inner, multi)
                try:
                    data = zf.read(inner)
                except Exception as e:
                    items.append(_err_item(arc_path, f"zip read failed: {e}",
                                           inner=inner, base=base))
                    continue
                items.append({
                    "display": f"{arc_path.name}!{inner}",
                    "base": base, "source": data, "error": None,
                })
    except zipfile.BadZipFile:
        items.append(_err_item(arc_path, "not a valid zip / corrupt"))
    except Exception as e:
        items.append(_err_item(arc_path, f"zip error: {type(e).__name__}: {e}"))
    return items


def _read_7z_archive(arc_path: Path):
    if not HAS_7Z:
        return [_err_item(arc_path,
                          "7z support not installed — run: pip install py7zr")]
    items = []
    try:
        with py7zr.SevenZipFile(arc_path, mode="r") as z:
            all_names = z.getnames() or []
            inner_pdfs = [n for n in all_names if n.lower().endswith(".pdf")]
            if not inner_pdfs:
                return [_err_item(arc_path, "no PDFs inside 7z")]
            multi = len(inner_pdfs) > 1
            # py7zr.read() returns a dict mapping name -> BytesIO
            data_map = z.read(targets=inner_pdfs)
            for inner in sorted(inner_pdfs):
                base = _build_base(arc_path, inner, multi)
                buf = data_map.get(inner) if data_map else None
                if buf is None:
                    items.append(_err_item(arc_path, "7z entry not readable",
                                           inner=inner, base=base))
                    continue
                items.append({
                    "display": f"{arc_path.name}!{inner}",
                    "base": base, "source": buf.read(), "error": None,
                })
    except Exception as e:
        items.append(_err_item(arc_path, f"7z error: {type(e).__name__}: {e}"))
    return items


def _read_rar_archive(arc_path: Path):
    if not HAS_RAR:
        return [_err_item(arc_path,
                          "rar support not installed — run: pip install rarfile "
                          "(also requires the 'unrar' system tool)")]
    items = []
    try:
        with rarfile.RarFile(arc_path) as rf:
            inner_pdfs = [
                n for n in rf.namelist()
                if n.lower().endswith(".pdf") and not n.endswith("/")
            ]
            if not inner_pdfs:
                return [_err_item(arc_path, "no PDFs inside rar")]
            multi = len(inner_pdfs) > 1
            for inner in sorted(inner_pdfs):
                base = _build_base(arc_path, inner, multi)
                try:
                    data = rf.read(inner)
                except Exception as e:
                    items.append(_err_item(arc_path, f"rar read failed: {e}",
                                           inner=inner, base=base))
                    continue
                items.append({
                    "display": f"{arc_path.name}!{inner}",
                    "base": base, "source": data, "error": None,
                })
    except getattr(rarfile, "RarCannotExec", Exception):
        items.append(_err_item(arc_path, "rar tool not found — install 'unrar'"))
    except getattr(rarfile, "BadRarFile", Exception):
        items.append(_err_item(arc_path, "not a valid rar / corrupt"))
    except Exception as e:
        items.append(_err_item(arc_path, f"rar error: {type(e).__name__}: {e}"))
    return items


def _read_tar_archive(arc_path: Path):
    items = []
    try:
        with tarfile.open(arc_path) as tf:
            members = [m for m in tf.getmembers()
                       if m.isfile() and m.name.lower().endswith(".pdf")]
            if not members:
                return [_err_item(arc_path, "no PDFs inside tar")]
            multi = len(members) > 1
            for m in sorted(members, key=lambda x: x.name):
                base = _build_base(arc_path, m.name, multi)
                try:
                    fh = tf.extractfile(m)
                    if fh is None:
                        items.append(_err_item(arc_path, "tar entry not readable",
                                               inner=m.name, base=base))
                        continue
                    data = fh.read()
                except Exception as e:
                    items.append(_err_item(arc_path, f"tar read failed: {e}",
                                           inner=m.name, base=base))
                    continue
                items.append({
                    "display": f"{arc_path.name}!{m.name}",
                    "base": base, "source": data, "error": None,
                })
    except tarfile.ReadError:
        items.append(_err_item(arc_path, "not a valid tar / corrupt"))
    except Exception as e:
        items.append(_err_item(arc_path, f"tar error: {type(e).__name__}: {e}"))
    return items


_ARCHIVE_READERS = {
    "zip": _read_zip_archive,
    "7z":  _read_7z_archive,
    "rar": _read_rar_archive,
    "tar": _read_tar_archive,
}


def _read_archive(arc_path: Path, kind: str = None):
    """Dispatch to the correct reader. If kind is None, infer from extension."""
    if kind is None:
        _, kind = _classify_path(arc_path)
    reader = _ARCHIVE_READERS.get(kind)
    if reader is None:
        return [_err_item(arc_path, f"unsupported archive type: {kind}")]
    return reader(arc_path)


def _scan_folder(folder_path: Path, recursive: bool):
    """Walk a folder and return (loose_pdfs, archives_with_kind).

    Only files with .pdf or recognized archive extensions are collected; all
    other file types are silently ignored. Extension matching is case-insensitive.
    """
    glob = folder_path.rglob if recursive else folder_path.glob
    loose_pdfs = []
    archives = []  # list of (Path, kind_str)
    for p in glob("*"):
        try:
            if not p.is_file():
                continue
        except OSError:
            continue
        name_lower = p.name.lower()
        if name_lower.endswith(".pdf"):
            loose_pdfs.append(p)
            continue
        for kind, exts in ARCHIVE_TYPES:
            if any(name_lower.endswith(ext) for ext in exts):
                archives.append((p, kind))
                break
    loose_pdfs.sort()
    archives.sort(key=lambda x: str(x[0]))
    return loose_pdfs, archives


def collect_pdf_sources(sources, recursive: bool = True):
    """Resolve a list of user-selected sources into a flat list of PDF sources.

    `sources` is a list of (kind, Path) tuples where kind ∈
    {'folder', 'zip', '7z', 'rar', 'tar'}.

    Folders are walked for PDFs and nested archives; archives are opened and
    their inner PDFs returned as bytes.
    """
    items = []
    seen_paths = set()

    for kind, path in sources:
        try:
            rp = path.resolve()
        except OSError:
            rp = path
        if rp in seen_paths:
            continue
        seen_paths.add(rp)

        if kind == "folder":
            loose_pdfs, archives = _scan_folder(path, recursive)
            for pdf in loose_pdfs:
                try:
                    pdf_rp = pdf.resolve()
                except OSError:
                    pdf_rp = pdf
                if pdf_rp in seen_paths:
                    continue
                seen_paths.add(pdf_rp)
                items.append({
                    "display": pdf.name, "base": pdf.stem,
                    "source": pdf, "error": None,
                })
            for arc_path, arc_kind in archives:
                try:
                    arc_rp = arc_path.resolve()
                except OSError:
                    arc_rp = arc_path
                if arc_rp in seen_paths:
                    continue
                seen_paths.add(arc_rp)
                items.extend(_read_archive(arc_path, arc_kind))
        else:
            items.extend(_read_archive(path, kind))

    return items


# ---------------------------------------------------------------------------
# Processing pipeline (shared by GUI and CLI)
# ---------------------------------------------------------------------------

def process_sources(sources, output_dir, recursive=True, sort_output=True):
    """Process the given sources and write hash output files.

    Yields progress events as a generator so both the GUI and CLI can consume
    them with their own presentation. Side effects: writes <base>_uniquehashes.txt
    files plus the aggregate _master_hashes.txt into output_dir.

    Event tuples:
      ("start", total_items, n_folders, n_archives)
      ("item_ok", i, display, hash_count, output_filename)
      ("item_empty", i, display)
      ("item_error", i, display, error_message)
      ("done", summary_dict)

    summary_dict keys: total_items, files_written, files_empty, files_errored,
    master_count, output_dir.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    n_folders = sum(1 for k, _ in sources if k == "folder")
    n_archives = len(sources) - n_folders

    items = collect_pdf_sources(sources, recursive=recursive)
    yield ("start", len(items), n_folders, n_archives)

    if not items:
        yield ("done", {
            "total_items": 0, "files_written": 0, "files_empty": 0,
            "files_errored": 0, "master_count": 0, "output_dir": output_dir,
        })
        return

    used_names = {MASTER_FILENAME.lower()}
    master_hashes = set()
    files_written = files_empty = files_errored = 0

    for i, item in enumerate(items, start=1):
        display = item["display"]

        if item["error"] or item["source"] is None:
            yield ("item_error", i, display,
                   item["error"] or "no readable source")
            files_errored += 1
            continue

        text, error = read_pdf_text(item["source"])
        if error:
            yield ("item_error", i, display, error)
            files_errored += 1
            continue

        hashes = extract_hashes(text)
        if not hashes:
            yield ("item_empty", i, display)
            files_empty += 1
            continue

        base = item["base"]
        candidate = f"{base}_uniquehashes.txt"
        counter = 2
        while candidate.lower() in used_names:
            candidate = f"{base}_{counter}_uniquehashes.txt"
            counter += 1
        used_names.add(candidate.lower())

        ordered = sorted(hashes) if sort_output else list(hashes)
        with open(output_dir / candidate, "w", encoding="utf-8") as f:
            f.write("\n".join(ordered) + "\n")

        master_hashes.update(hashes)
        files_written += 1
        yield ("item_ok", i, display, len(ordered), candidate)

    if master_hashes:
        ordered_master = (sorted(master_hashes) if sort_output
                          else list(master_hashes))
        with open(output_dir / MASTER_FILENAME, "w", encoding="utf-8") as f:
            f.write("\n".join(ordered_master) + "\n")

    yield ("done", {
        "total_items": len(items),
        "files_written": files_written,
        "files_empty": files_empty,
        "files_errored": files_errored,
        "master_count": len(master_hashes),
        "output_dir": output_dir,
    })


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class HashExtractorApp:
    KIND_TAGS = {
        "folder": "[FOLDER]",
        "zip":    "[ZIP]   ",
        "7z":     "[7Z]    ",
        "rar":    "[RAR]   ",
        "tar":    "[TAR]   ",
    }

    def __init__(self, root):
        self.root = root
        root.title("PDF MD5 Hash Extractor")
        root.geometry("720x660")
        root.minsize(600, 560)

        pad = {"padx": 10, "pady": 6}

        # --- Header & Sources list -------------------------------------------
        self.sources = []  # list of (kind, Path)

        hdr = tk.Frame(root)
        hdr.pack(fill=tk.X, padx=10, pady=(10, 0))
        tk.Label(hdr, text="Sources", font=("Arial", 10, "bold"),
                 anchor="w").pack(side=tk.LEFT)
        supported_exts = [".zip"]
        if HAS_7Z:   supported_exts.append(".7z")
        if HAS_RAR:  supported_exts.append(".rar")
        supported_exts.append(".tar/.tar.gz/etc.")
        hint = "  (folders or archives: " + ", ".join(supported_exts) + ")"
        tk.Label(hdr, text=hint, fg="#666", anchor="w").pack(side=tk.LEFT)

        if HAS_DND:
            dnd_hint = tk.Label(
                root,
                text="Drag folders or archives onto the list below, or use the Add button.",
                fg="#888", anchor="w",
            )
            dnd_hint.pack(fill=tk.X, padx=10, pady=(0, 2))

        src_list_frame = tk.Frame(root)
        src_list_frame.pack(fill=tk.X, padx=10, pady=(4, 0))
        self.sources_listbox = tk.Listbox(
            src_list_frame, height=7, selectmode=tk.EXTENDED,
            font=("Courier", 10),
        )
        src_scroll = tk.Scrollbar(src_list_frame, command=self.sources_listbox.yview)
        self.sources_listbox.configure(yscrollcommand=src_scroll.set)
        self.sources_listbox.pack(side=tk.LEFT, fill=tk.X, expand=True)
        src_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Register drag-and-drop on the listbox if available
        if HAS_DND:
            try:
                self.sources_listbox.drop_target_register(DND_FILES)
                self.sources_listbox.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass  # silently fall back if registration fails

        src_btns = tk.Frame(root)
        src_btns.pack(fill=tk.X, padx=10, pady=(2, 6))
        # Unified Add — single visible button with a small dropdown menu so we
        # don't need separate "Add Folder" / "Add Archives" buttons.
        add_btn = tk.Menubutton(
            src_btns, text="Add\u2026  \u25be",
            relief="raised", width=12, indicatoron=False,
        )
        add_menu = tk.Menu(add_btn, tearoff=0)
        add_menu.add_command(label="Folder\u2026", command=self.add_folder)
        add_menu.add_command(label="Archive(s)\u2026", command=self.add_archives)
        add_btn.config(menu=add_menu)
        add_btn.pack(side=tk.LEFT, padx=(0, 4))
        tk.Button(src_btns, text="Remove Selected",
                  command=self.remove_selected, width=15).pack(side=tk.LEFT, padx=4)
        tk.Button(src_btns, text="Clear",
                  command=self.clear_sources, width=8).pack(side=tk.LEFT, padx=4)

        # --- Output folder ---------------------------------------------------
        f2 = tk.Frame(root)
        f2.pack(fill=tk.X, **pad)
        tk.Label(f2, text="Output folder:", width=12, anchor="w").pack(side=tk.LEFT)
        self.output_var = tk.StringVar(value=str(Path.cwd() / "extracted_hashes"))
        tk.Entry(f2, textvariable=self.output_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        tk.Button(f2, text="Browse\u2026", command=self.browse_output,
                  width=10).pack(side=tk.LEFT)

        # --- Options ---------------------------------------------------------
        f3 = tk.Frame(root)
        f3.pack(fill=tk.X, **pad)
        self.recursive_var = tk.BooleanVar(value=True)
        tk.Checkbutton(f3, text="Include subfolders (when scanning folders)",
                       variable=self.recursive_var).pack(side=tk.LEFT)
        self.sort_var = tk.BooleanVar(value=True)
        tk.Checkbutton(f3, text="Sort output", variable=self.sort_var).pack(
            side=tk.LEFT, padx=20)

        # --- Run button ------------------------------------------------------
        self.run_button = tk.Button(
            root, text="Extract Hashes", command=self.start_extraction,
            bg="#2e7d32", fg="white", font=("Arial", 11, "bold"), height=2,
        )
        self.run_button.pack(fill=tk.X, padx=10, pady=(4, 6))

        # --- Progress & status ----------------------------------------------
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(
            root, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill=tk.X, padx=10)
        self.status_var = tk.StringVar(value="Ready.")
        tk.Label(root, textvariable=self.status_var, anchor="w").pack(
            fill=tk.X, padx=10, pady=(2, 4))

        # --- Log -------------------------------------------------------------
        log_frame = tk.Frame(root)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.log = tk.Text(log_frame, height=10, wrap="word")
        scroll = tk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        # Surface missing-optional-dependency hints in the log on startup
        if not HAS_DND:
            self.log_message("Note: tkinterdnd2 not installed — drag-and-drop disabled. "
                             "Install with: pip install tkinterdnd2")
        if not HAS_7Z:
            self.log_message("Note: py7zr not installed — .7z archives unsupported. "
                             "Install with: pip install py7zr")
        if not HAS_RAR:
            self.log_message("Note: rarfile not installed — .rar archives unsupported. "
                             "Install with: pip install rarfile (also requires 'unrar')")

    # --- Source list management ------------------------------------------------
    def _format_source_row(self, kind: str, path: Path) -> str:
        tag = self.KIND_TAGS.get(kind, f"[{kind.upper()}]")
        if kind == "folder":
            return f"{tag}  {path}"
        return f"{tag}  {path.name}   \u2014   {path.parent}"

    def _source_already_added(self, resolved_path) -> bool:
        for _, p in self.sources:
            try:
                if p.resolve() == resolved_path:
                    return True
            except OSError:
                continue
        return False

    def _add_path(self, p: Path):
        """Try to add p as a new source. Returns (added: bool, reason: str)."""
        try:
            rp = p.resolve()
        except OSError as e:
            return False, f"cannot resolve path: {e}"
        if self._source_already_added(rp):
            return False, "already in list"
        category, kind = _classify_path(p)
        if category == "folder":
            self.sources.append(("folder", p))
            self.sources_listbox.insert(tk.END, self._format_source_row("folder", p))
            return True, ""
        if category == "archive":
            self.sources.append((kind, p))
            self.sources_listbox.insert(tk.END, self._format_source_row(kind, p))
            return True, ""
        return False, kind  # 'skip' — kind holds the reason

    def add_folder(self):
        folder = filedialog.askdirectory(
            title="Choose a folder to scan for PDFs and archives")
        if not folder:
            return
        added, reason = self._add_path(Path(folder))
        if not added and reason != "already in list":
            messagebox.showwarning("Couldn't add", reason)

    def add_archives(self):
        # Build file-type filter only from formats we actually support
        all_patterns = ["*.zip", "*.ZIP"]
        if HAS_7Z:   all_patterns += ["*.7z", "*.7Z"]
        if HAS_RAR:  all_patterns += ["*.rar", "*.RAR"]
        all_patterns += ["*.tar", "*.tar.gz", "*.tar.bz2", "*.tar.xz",
                         "*.tgz", "*.tbz2", "*.txz"]
        filetypes = [("Supported archives", " ".join(all_patterns)),
                     ("ZIP", "*.zip *.ZIP")]
        if HAS_7Z:   filetypes.append(("7-Zip", "*.7z *.7Z"))
        if HAS_RAR:  filetypes.append(("RAR", "*.rar *.RAR"))
        filetypes.append(("TAR/gzip/bzip2/xz",
                          "*.tar *.tar.gz *.tar.bz2 *.tar.xz *.tgz *.tbz2 *.txz"))
        filetypes.append(("All files", "*.*"))

        paths = filedialog.askopenfilenames(
            title="Select archive file(s) containing PDFs",
            filetypes=filetypes)
        if not paths:
            return
        skipped = []
        for raw in paths:
            added, reason = self._add_path(Path(raw))
            if not added and reason != "already in list":
                skipped.append(f"{Path(raw).name} — {reason}")
        if skipped:
            messagebox.showwarning("Some files skipped", "\n".join(skipped))

    def remove_selected(self):
        sel = list(self.sources_listbox.curselection())
        for idx in reversed(sel):
            self.sources_listbox.delete(idx)
            del self.sources[idx]

    def clear_sources(self):
        self.sources_listbox.delete(0, tk.END)
        self.sources.clear()

    def _on_drop(self, event):
        """Handle drag-and-drop. event.data is a string of paths, possibly
        with curly braces around paths containing spaces."""
        try:
            raw_paths = self.root.tk.splitlist(event.data)
        except Exception:
            raw_paths = [event.data]
        skipped = []
        for raw in raw_paths:
            raw = raw.strip()
            if raw.startswith("{") and raw.endswith("}"):
                raw = raw[1:-1]
            if not raw:
                continue
            p = Path(raw)
            added, reason = self._add_path(p)
            if not added and reason and reason != "already in list":
                skipped.append(f"{p.name} — {reason}")
        if skipped:
            self.log_message(f"Drag-and-drop: skipped {len(skipped)} item(s):")
            for s in skipped:
                self.log_message(f"  - {s}")

    # --- Other helpers --------------------------------------------------------
    def browse_output(self):
        folder = filedialog.askdirectory(title="Choose folder for output text files")
        if folder:
            self.output_var.set(folder)

    def log_message(self, msg):
        self.log.insert(tk.END, msg + "\n")
        self.log.see(tk.END)

    # --- Worker ---------------------------------------------------------------
    def start_extraction(self):
        output = self.output_var.get().strip()
        if not self.sources:
            messagebox.showerror(
                "Error",
                "Please add at least one source (folder or archive) using the "
                "Add button" + (" or by dragging onto the list" if HAS_DND else "") + ".")
            return
        if not output:
            messagebox.showerror("Error", "Please specify an output folder.")
            return

        # Verify every source still exists at run time
        missing = []
        valid = []
        for kind, p in self.sources:
            if kind == "folder":
                if p.is_dir():
                    valid.append((kind, p))
                else:
                    missing.append(f"[FOLDER] {p}")
            else:
                if p.is_file():
                    valid.append((kind, p))
                else:
                    missing.append(f"[{kind.upper()}] {p}")
        if missing:
            messagebox.showerror(
                "Error", "These sources no longer exist on disk:\n" + "\n".join(missing))
            return

        self.run_button.config(state=tk.DISABLED)
        self.log.delete("1.0", tk.END)
        self.progress_var.set(0)
        thread = threading.Thread(
            target=self._extract, args=(valid, output), daemon=True)
        thread.start()

    # --- Thread-safe UI update helpers -----------------------------------
    # All of these can be called from any thread; they marshal the actual
    # widget update back onto the Tk main thread via root.after(). The
    # _safe_after wrapper swallows TclError so the worker doesn't crash if
    # the user closes the window mid-run.
    def _safe_after(self, fn, *args):
        try:
            self.root.after(0, fn, *args)
        except (tk.TclError, RuntimeError):
            pass  # window destroyed; nothing to do

    def _ui_log(self, msg):
        self._safe_after(self._do_log, msg)

    def _do_log(self, msg):
        self.log.insert(tk.END, msg + "\n")
        self.log.see(tk.END)

    def _ui_status(self, text):
        self._safe_after(self.status_var.set, text)

    def _ui_progress(self, value):
        self._safe_after(self.progress_var.set, value)

    def _ui_enable_run(self):
        self._safe_after(self._do_enable_run)

    def _do_enable_run(self):
        self.run_button.config(state=tk.NORMAL)

    def _ui_info(self, title, msg):
        self._safe_after(messagebox.showinfo, title, msg)

    def _ui_error(self, title, msg):
        self._safe_after(messagebox.showerror, title, msg)

    def _extract(self, sources, output):
        """Run extraction in this background thread; route UI updates back to
        the main thread via _ui_* helpers so Tk widgets are never touched from
        a non-main thread."""
        try:
            total = 0
            for event in process_sources(
                sources, output,
                recursive=self.recursive_var.get(),
                sort_output=self.sort_var.get(),
            ):
                tag = event[0]
                if tag == "start":
                    total = event[1]
                    n_folders, n_archives = event[2], event[3]
                    if total == 0:
                        self._ui_log("No PDFs found in any of the configured sources.")
                        self._ui_status("No PDFs found.")
                    else:
                        self._ui_log(
                            f"Resolved {total} PDF source(s) "
                            f"from {n_folders} folder(s) and {n_archives} archive(s). "
                            f"Scanning\u2026\n")
                elif tag == "item_ok":
                    i, display, count, filename = event[1], event[2], event[3], event[4]
                    self._ui_status(f"[{i}/{total}] {display}")
                    self._ui_log(f"  \u2713 {display}: {count} hash(es) \u2192 {filename}")
                    self._ui_progress(i / total * 100)
                elif tag == "item_empty":
                    i, display = event[1], event[2]
                    self._ui_status(f"[{i}/{total}] {display}")
                    self._ui_log(f"  \u2013 {display}: no hashes found")
                    self._ui_progress(i / total * 100)
                elif tag == "item_error":
                    i, display, err = event[1], event[2], event[3]
                    self._ui_status(f"[{i}/{total}] {display}")
                    self._ui_log(f"  \u26a0 {display}: {err}")
                    self._ui_progress(i / total * 100)
                elif tag == "done":
                    summary = event[1]
                    self._ui_log("")
                    self._ui_log(f"Output folder: {summary['output_dir']}")
                    self._ui_log(f"Per-source hash files: {summary['files_written']}")
                    if summary["master_count"]:
                        self._ui_log(
                            f"Master list: {summary['master_count']} unique hash(es) "
                            f"\u2192 {MASTER_FILENAME}")
                    if summary["files_empty"]:
                        self._ui_log(
                            f"{summary['files_empty']} source(s) had no hashes (skipped).")
                    if summary["files_errored"]:
                        self._ui_log(
                            f"{summary['files_errored']} source(s) could not be read.")
                    self._ui_status("Done.")
                    if summary["total_items"] > 0:
                        self._ui_info(
                            "Complete",
                            f"Wrote {summary['files_written']} per-source file(s) "
                            f"plus master list ({summary['master_count']} unique "
                            f"hashes) to:\n{summary['output_dir']}")
        except Exception as e:
            self._ui_log(f"Fatal error: {e}")
            self._ui_status("Error.")
            self._ui_error("Error", str(e))
        finally:
            self._ui_enable_run()


def make_root():
    """Return the appropriate root window — TkinterDnD's if available, else plain Tk."""
    if HAS_DND and TkinterDnD is not None:
        try:
            return TkinterDnD.Tk()
        except Exception:
            pass
    return tk.Tk()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_cli(source_paths, output, recursive=True, sort_output=True, quiet=False):
    """Run the extractor headlessly, printing progress to stdout.

    `source_paths` is a list of strings; each is classified into folder/archive
    automatically. Returns an exit code: 0 = clean run, 1 = run completed but
    with errors on some sources, 2 = fatal failure (bad input).
    """
    if not output:
        print("error: --output is required in CLI mode", file=sys.stderr)
        return 2
    if not source_paths:
        print("error: at least one source path is required in CLI mode",
              file=sys.stderr)
        return 2

    # Classify each input path
    sources = []
    bad = []
    for raw in source_paths:
        p = Path(raw)
        if not p.exists():
            bad.append(f"  {raw}: does not exist")
            continue
        category, kind = _classify_path(p)
        if category == "folder":
            sources.append(("folder", p))
        elif category == "archive":
            sources.append((kind, p))
        else:
            bad.append(f"  {raw}: {kind}")

    if bad:
        print("error: invalid source(s):", file=sys.stderr)
        for line in bad:
            print(line, file=sys.stderr)
        return 2

    if not quiet:
        print(f"Output: {output}")
    final_summary = None
    total = 0
    for event in process_sources(sources, output,
                                 recursive=recursive,
                                 sort_output=sort_output):
        tag = event[0]
        if tag == "start":
            total = event[1]
            n_folders, n_archives = event[2], event[3]
            if total == 0:
                print("No PDFs found in any of the configured sources.")
            elif not quiet:
                print(f"Resolved {total} PDF source(s) "
                      f"from {n_folders} folder(s) and {n_archives} archive(s).")
                print()
        elif tag == "item_ok":
            i, display, count, filename = event[1], event[2], event[3], event[4]
            if not quiet:
                print(f"  [{i}/{total}] OK    {display}: {count} hashes -> {filename}")
        elif tag == "item_empty":
            i, display = event[1], event[2]
            if not quiet:
                print(f"  [{i}/{total}] EMPTY {display}: no hashes")
        elif tag == "item_error":
            i, display, err = event[1], event[2], event[3]
            print(f"  [{i}/{total}] ERROR {display}: {err}", file=sys.stderr)
        elif tag == "done":
            final_summary = event[1]

    if not quiet and final_summary and final_summary["total_items"] > 0:
        print()
        print(f"Done. Files written: {final_summary['files_written']}, "
              f"empty: {final_summary['files_empty']}, "
              f"errored: {final_summary['files_errored']}")
        if final_summary["master_count"]:
            print(f"Master list: {final_summary['master_count']} unique hashes "
                  f"-> {MASTER_FILENAME}")

    if final_summary and final_summary["files_errored"] > 0:
        return 1
    return 0


def run_gui():
    """Launch the Tk GUI. Returns 0 on clean exit, 2 if Tk unavailable."""
    if not HAS_TK:
        print("error: GUI mode requires tkinter, which is not installed on this system.",
              file=sys.stderr)
        print("Use CLI mode instead — see --help.", file=sys.stderr)
        return 2
    root = make_root()
    HashExtractorApp(root)
    root.mainloop()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="pdf_md5_extractor",
        description="Extract MD5 hashes from PDF reports. "
                    "Runs as a GUI by default; pass source paths for CLI mode.",
        epilog="Examples:\n"
               "  pdf_md5_extractor                          # launch GUI\n"
               "  pdf_md5_extractor -o out/ cases/inbox/      # CLI: process a folder\n"
               "  pdf_md5_extractor -o out/ tip.zip bundle.7z # CLI: multiple archives",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("sources", nargs="*",
                        help="Folders or archive files to process. "
                             "If omitted (and --gui not used), launches GUI.")
    parser.add_argument("-o", "--output",
                        help="Output folder for hash files (required for CLI mode)")
    parser.add_argument("--no-recursive", action="store_true",
                        help="Don't recurse into subfolders when scanning folders")
    parser.add_argument("--no-sort", action="store_true",
                        help="Don't sort hashes alphabetically in output files")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="Suppress per-item progress output (errors still shown)")
    parser.add_argument("--gui", action="store_true",
                        help="Force GUI even if source paths are provided")

    args = parser.parse_args(argv)

    # Decision: GUI mode if --gui flag, or if no sources AND no output specified.
    # CLI mode otherwise.
    if args.gui or (not args.sources and not args.output):
        return run_gui()

    return run_cli(
        source_paths=args.sources,
        output=args.output,
        recursive=not args.no_recursive,
        sort_output=not args.no_sort,
        quiet=args.quiet,
    )


if __name__ == "__main__":
    sys.exit(main())
