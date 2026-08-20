"""PowerPoint to PDF, via headless LibreOffice.

Everything downstream of ingestion - page rendering, text extraction, the
images the viewer shows - is built on PyMuPDF and therefore on PDF. Rather
than grow a second parallel pipeline for PPTX, this converts PPTX to PDF once
at the door and lets the existing, tested path do the rest.

Why LibreOffice and not ``python-pptx``: ``python-pptx`` reads shapes, text
and embedded media, but it is a *parser*, not a *renderer* - it cannot produce
an image of a slide as laid out, because laying it out is the hard part
(theme, masters, fonts, autofit, charts). The UI shows real slide images, so a
renderer is required, and LibreOffice is the only free one that handles real
PowerPoint fidelity. The cost is a large binary dependency, which is why the
absence of it is reported as a clear, actionable error rather than a crash.

The conversion runs in a subprocess with a timeout and a private user profile
directory. The profile matters: concurrent ``soffice`` invocations sharing the
default profile will silently refuse to start, one of them attaching to the
other's process instead of converting anything.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

from loguru import logger

# Long enough for a big deck on a cold LibreOffice start (the first run in a
# container pays one-off font cache costs), short enough that a wedged process
# does not hold an upload open forever.
CONVERT_TIMEOUT_S = float(os.environ.get("SOFFICE_TIMEOUT_S", "180"))

PPTX_SUFFIXES = (".pptx", ".ppt")

# Where LibreOffice usually lands per platform. Checked in order, after $PATH
# and $SOFFICE_BIN.
_CANDIDATES = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/usr/bin/soffice",
    "/usr/bin/libreoffice",
    "/usr/local/bin/soffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
)

INSTALL_HINT = (
    "PowerPoint conversion needs LibreOffice, which was not found. "
    "Install it (https://www.libreoffice.org/download/) or set SOFFICE_BIN to "
    "the full path of the soffice binary. Alternatively, export the deck to "
    "PDF and upload that instead."
)


class ConversionError(Exception):
    """Raised when a PPTX could not be turned into a PDF."""


def find_soffice() -> str | None:
    """Locate the LibreOffice binary, or ``None`` if it is not installed.

    Order: ``SOFFICE_BIN``, then ``$PATH``, then the usual per-platform
    install locations.
    """
    override = os.environ.get("SOFFICE_BIN", "").strip()
    if override:
        # An explicit override that is wrong should be loud, not silently
        # skipped in favour of some other copy on the system.
        return override if Path(override).is_file() else None

    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found

    for candidate in _CANDIDATES:
        if Path(candidate).is_file():
            return candidate

    return None


def soffice_available() -> bool:
    """Whether PPTX upload can work at all right now."""
    return find_soffice() is not None


async def pptx_to_pdf(data: bytes, *, filename: str = "deck.pptx") -> bytes:
    """Convert PowerPoint bytes to PDF bytes.

    Args:
        data: The raw ``.pptx``/``.ppt`` file.
        filename: Original name, used only for the temp file's extension and
            for log lines.

    Returns:
        The converted PDF as bytes.

    Raises:
        ConversionError: If LibreOffice is missing, times out, exits non-zero,
            or produces no output file.
    """
    binary = find_soffice()
    if binary is None:
        raise ConversionError(INSTALL_HINT)

    suffix = Path(filename).suffix.lower()
    if suffix not in PPTX_SUFFIXES:
        suffix = ".pptx"

    with tempfile.TemporaryDirectory(prefix="deckconv-") as tmp:
        tmpdir = Path(tmp)
        source = tmpdir / f"input{suffix}"
        source.write_bytes(data)

        # A profile per conversion. Without this, a second concurrent soffice
        # attaches to the first instance and exits 0 having converted nothing.
        profile = tmpdir / "profile"
        outdir = tmpdir / "out"
        outdir.mkdir()

        args = [
            binary,
            "--headless",
            "--norestore",
            "--invisible",
            "--nodefault",
            "--nolockcheck",
            "--nologo",
            f"-env:UserInstallation={profile.as_uri()}",
            "--convert-to",
            "pdf",
            "--outdir",
            str(outdir),
            str(source),
        ]

        logger.info(f"Converting {filename!r} to PDF with LibreOffice")
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise ConversionError(f"Could not run LibreOffice at {binary!r}: {exc}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=CONVERT_TIMEOUT_S
            )
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise ConversionError(
                f"LibreOffice took longer than {CONVERT_TIMEOUT_S:.0f}s to convert that "
                "file. It may be very large or corrupt."
            ) from exc

        if proc.returncode != 0:
            detail = (stderr or stdout or b"").decode(errors="replace").strip()
            raise ConversionError(
                f"LibreOffice failed to convert that file (exit {proc.returncode}). "
                f"{detail[:300]}".strip()
            )

        produced = sorted(outdir.glob("*.pdf"))
        if not produced:
            # Exit code 0 with no output is LibreOffice's signature failure
            # mode for a file it cannot parse.
            detail = (stderr or stdout or b"").decode(errors="replace").strip()
            raise ConversionError(
                "LibreOffice produced no PDF from that file - it may be corrupt or "
                f"not really a PowerPoint file. {detail[:300]}".strip()
            )

        pdf = produced[0].read_bytes()

    if not pdf:
        raise ConversionError("The converted PDF came out empty.")

    logger.info(f"Converted {filename!r}: {len(data):,}B pptx -> {len(pdf):,}B pdf")
    return pdf
