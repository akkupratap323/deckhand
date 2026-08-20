"""Tests for PPTX -> PDF conversion.

Locating the binary and handling its failure modes is what is worth testing:
those are the paths that decide whether a user gets a clear message or a
mystery. The conversion itself is LibreOffice's job, and a test that shells out
to it would need a real install, so the subprocess is faked. One test does run
the real thing, skipped when LibreOffice is absent.
"""

from __future__ import annotations

import asyncio

import pytest

import convert
from convert import ConversionError, find_soffice, pptx_to_pdf, soffice_available


@pytest.fixture(autouse=True)
def clear_override(monkeypatch):
    monkeypatch.delenv("SOFFICE_BIN", raising=False)


# --- locating the binary ---------------------------------------------------


def test_explicit_override_is_used_when_it_exists(monkeypatch, tmp_path):
    fake = tmp_path / "soffice.exe"
    fake.write_text("")
    monkeypatch.setenv("SOFFICE_BIN", str(fake))
    assert find_soffice() == str(fake)


def test_wrong_override_is_not_silently_replaced(monkeypatch, tmp_path):
    """A bad SOFFICE_BIN must not fall through to some other copy on the box.

    Falling back would make a typo look like it worked while quietly using a
    different binary than the one asked for.
    """
    monkeypatch.setenv("SOFFICE_BIN", str(tmp_path / "nope.exe"))
    monkeypatch.setattr(convert.shutil, "which", lambda _: "/usr/bin/soffice")
    assert find_soffice() is None


def test_falls_back_to_path(monkeypatch):
    monkeypatch.setattr(convert, "_CANDIDATES", ())
    monkeypatch.setattr(convert.shutil, "which", lambda n: "/usr/bin/soffice" if n == "soffice" else None)
    assert find_soffice() == "/usr/bin/soffice"


def test_none_when_nothing_is_installed(monkeypatch):
    monkeypatch.setattr(convert.shutil, "which", lambda _: None)
    monkeypatch.setattr(convert, "_CANDIDATES", ())
    assert find_soffice() is None
    assert soffice_available() is False


# --- failure modes ---------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_binary_gives_an_actionable_message(monkeypatch):
    monkeypatch.setattr(convert, "find_soffice", lambda: None)
    with pytest.raises(ConversionError) as excinfo:
        await pptx_to_pdf(b"whatever", filename="x.pptx")
    message = str(excinfo.value)
    assert "LibreOffice" in message
    # Must tell the user what to actually do about it.
    assert "SOFFICE_BIN" in message or "export" in message.lower()


class _FakeProc:
    """Stand-in for an asyncio subprocess."""

    def __init__(self, returncode=0, stderr=b"", on_communicate=None):
        self.returncode = returncode
        self._stderr = stderr
        self._on_communicate = on_communicate
        self.killed = False

    async def communicate(self):
        if self._on_communicate is not None:
            await self._on_communicate()
        return b"", self._stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def _fake_exec(proc, *, write_pdf_to=None, pdf_bytes=b"%PDF-1.4 fake"):
    """Build a create_subprocess_exec replacement that optionally emits a PDF."""

    async def run(*args, **kwargs):
        if write_pdf_to is not None:
            # Mimic LibreOffice writing its output into --outdir.
            outdir = args[args.index("--outdir") + 1]
            from pathlib import Path

            Path(outdir, "input.pdf").write_bytes(pdf_bytes)
        return proc

    return run


@pytest.mark.asyncio
async def test_nonzero_exit_is_reported(monkeypatch):
    monkeypatch.setattr(convert, "find_soffice", lambda: "/fake/soffice")
    proc = _FakeProc(returncode=3, stderr=b"boom")
    monkeypatch.setattr(convert.asyncio, "create_subprocess_exec", _fake_exec(proc))
    with pytest.raises(ConversionError, match="exit 3"):
        await pptx_to_pdf(b"data", filename="x.pptx")


@pytest.mark.asyncio
async def test_exit_zero_with_no_pdf_is_still_an_error(monkeypatch):
    """LibreOffice's signature failure: success exit code, nothing produced."""
    monkeypatch.setattr(convert, "find_soffice", lambda: "/fake/soffice")
    proc = _FakeProc(returncode=0)
    monkeypatch.setattr(convert.asyncio, "create_subprocess_exec", _fake_exec(proc))
    with pytest.raises(ConversionError, match="no PDF"):
        await pptx_to_pdf(b"data", filename="x.pptx")


@pytest.mark.asyncio
async def test_timeout_kills_the_process(monkeypatch):
    monkeypatch.setattr(convert, "find_soffice", lambda: "/fake/soffice")
    monkeypatch.setattr(convert, "CONVERT_TIMEOUT_S", 0.05)

    async def hang():
        await asyncio.sleep(10)

    proc = _FakeProc(on_communicate=hang)
    monkeypatch.setattr(convert.asyncio, "create_subprocess_exec", _fake_exec(proc))

    with pytest.raises(ConversionError, match="longer than"):
        await pptx_to_pdf(b"data", filename="x.pptx")
    # A wedged LibreOffice must not be left running.
    assert proc.killed is True


@pytest.mark.asyncio
async def test_successful_conversion_returns_the_pdf(monkeypatch):
    monkeypatch.setattr(convert, "find_soffice", lambda: "/fake/soffice")
    proc = _FakeProc(returncode=0)
    monkeypatch.setattr(
        convert.asyncio, "create_subprocess_exec", _fake_exec(proc, write_pdf_to=True)
    )
    out = await pptx_to_pdf(b"data", filename="deck.pptx")
    assert out == b"%PDF-1.4 fake"


@pytest.mark.asyncio
async def test_each_conversion_gets_its_own_profile(monkeypatch):
    """Concurrent soffice runs sharing a profile silently convert nothing."""
    monkeypatch.setattr(convert, "find_soffice", lambda: "/fake/soffice")
    seen: list[str] = []

    async def capture(*args, **kwargs):
        seen.extend(a for a in args if str(a).startswith("-env:UserInstallation="))
        from pathlib import Path

        Path(args[args.index("--outdir") + 1], "input.pdf").write_bytes(b"%PDF")
        return _FakeProc(returncode=0)

    monkeypatch.setattr(convert.asyncio, "create_subprocess_exec", capture)
    await pptx_to_pdf(b"a", filename="one.pptx")
    await pptx_to_pdf(b"b", filename="two.pptx")

    assert len(seen) == 2
    assert seen[0] != seen[1], "two conversions reused the same LibreOffice profile"


# --- the real thing, when it is installed ----------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(not soffice_available(), reason="LibreOffice is not installed")
async def test_real_conversion_produces_a_readable_pdf():
    """End-to-end against a real LibreOffice, if this machine has one."""
    pytest.importorskip("pptx")
    import io

    import pymupdf
    from pptx import Presentation

    buf = io.BytesIO()
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[0])
    slide.shapes.title.text = "Hello from PPTX"
    presentation.save(buf)

    pdf = await pptx_to_pdf(buf.getvalue(), filename="real.pptx")

    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        assert doc.page_count >= 1
        assert "Hello from PPTX" in doc.load_page(0).get_text("text")
