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
