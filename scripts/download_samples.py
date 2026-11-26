"""Fetch the evaluation PDFs into data/sample_pdfs/.

PDFs are kept out of git (see .gitignore); run this once after cloning.
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

SAMPLES = {
    "nist_csf_2.0.pdf": "https://nvlpubs.nist.gov/nistpubs/cswp/nist.cswp.29.pdf",
    "nasa_systems_handbook.pdf": (
        "https://ntrs.nasa.gov/api/citations/20170001761/downloads/"
        "20170001761.pdf"
    ),
}

DEST = Path(__file__).resolve().parent.parent / "data" / "sample_pdfs"


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    status = 0
    for name, url in SAMPLES.items():
        target = DEST / name
        if target.exists():
            print(f"skip  {name} (already downloaded)")
            continue
        try:
            print(f"fetch {name} ...", flush=True)
            request = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 DocuMind/1.0"}
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                target.write_bytes(response.read())
            print(f"  ok  {target.stat().st_size / 1024:.0f} KB")
        except Exception as exc:
            print(f"  FAILED: {exc}")
            print(f"  download it manually from {url}")
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
