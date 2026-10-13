"""YONDER-243 — captured emails and their attachments are scanned before
anything parses them: ClamAV over the raw message (stubbed here), static PDF
checks always on, fail closed when the scanner can't answer."""
import base64
import io
import json
import sys
import urllib.error
from email.message import EmailMessage
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pypdf = pytest.importorskip("pypdf")

from extract import filescan, pipeline  # noqa: E402


def _pdf(text="Amtrak eTicket 1234", catalog_extra=b"", extra_objects=(), stream=None):
    """A hand-assembled PDF (same shape as test_extract._tiny_pdf), with room
    for extra catalog entries and objects."""
    body = stream if stream is not None else f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R " + catalog_extra + b" >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(body)).encode() + b" >>\nstream\n" + body + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        *extra_objects,
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\n"
            f"startxref\n{xref_at}\n%%EOF").encode()
    return out


def _email(pdf=None, body="Your Amtrak trip is attached"):
    msg = EmailMessage()
    msg["Subject"] = "Your ticket"
    msg["From"] = "Amtrak <noreply@example.com>"
    msg["Date"] = "Mon, 3 Aug 2026 12:00:00 -0400"
    msg.set_content(body)
    if pdf is not None:
        msg.add_attachment(pdf, maintype="application", subtype="pdf", filename="ticket.pdf")
    return {"capture_channel": "gmail",
            "raw_message_base64": base64.b64encode(msg.as_bytes()).decode()}


class _LLM:
    """Records what the LLM was shown; returns a valid one-item batch."""

    def __init__(self):
        self.docs = []

    def extract(self, doc, context):
        self.docs.append(doc)
        from tests.test_extract import VALID_BATCH
        return json.loads(json.dumps(VALID_BATCH)), []


class _AV(filescan.Scanner):
    def __init__(self, result=None, down=False):
        super().__init__("http://av.test")
        self.result, self.down, self.calls = result, down, 0

    def scan_message(self, raw):
        self.calls += 1
        if self.down:
            raise filescan.ScanUnavailable("URLError")
        return self.result


# ------------------------------------------------------------------ static PDF checks

def test_a_plain_pdf_passes():
    assert filescan.check_pdf(_pdf()).ok


@pytest.mark.parametrize("extra, objs, want", [
    (b"/OpenAction << /S /Launch /F (cmd.exe) >>", (), "Launch"),
    (b"/OpenAction << /S /L#61unch /F (cmd.exe) >>", (), "Launch"),     # #-escaped names
    (b"/OpenAction << /S /ImportData /F (x.fdf) >>", (), "ImportData"),
    (b"/OpenAction << /S /GoToE /T << >> >>", (), "GoToE"),
    (b"/AcroForm << /XFA 6 0 R >>", (b"<< >>",), "XFA"),
])
def test_outward_actions_are_blocked(extra, objs, want):
    v = filescan.check_pdf(_pdf(catalog_extra=extra, extra_objects=objs))
    assert not v.ok and want in v.reason


def test_viewer_javascript_and_embedded_files_are_read_but_noted():
    """A real Salesforce-made booking PDF in the owner's inbox carries
    `this.zoom = 100;` — Rundere never runs it, so it's read, with a note."""
    v = filescan.check_pdf(_pdf(catalog_extra=b"/OpenAction << /S /JavaScript /JS (this.zoom = 100;) >>"))
    assert v.ok and any("JavaScript" in n for n in v.notes)
    v = filescan.check_pdf(_pdf(catalog_extra=b"/Names << /EmbeddedFiles 6 0 R >>",
                                extra_objects=(b"<< /Names [] >>",)))
    assert v.ok and any("embedded" in n for n in v.notes)
    assert filescan.check_pdf(_pdf()).notes == ()


def test_active_content_inside_compressed_object_streams_is_found():
    """pypdf-written files keep dictionaries in compressed streams the raw
    scan can't see; the structural walk does."""
    w = pypdf.PdfWriter()
    w.add_blank_page(612, 792)
    w.add_js("app.alert('hi');")
    buf = io.BytesIO()
    w.write(buf)
    reader = pypdf.PdfReader(io.BytesIO(buf.getvalue()))
    assert "JavaScript" in filescan._walk_active(reader)
    assert filescan.check_pdf(buf.getvalue()).notes


def test_names_inside_page_content_are_not_mistaken_for_actions():
    """Compressed or text stream bytes can contain '/JS' by chance."""
    v = filescan.check_pdf(_pdf(stream=b"BT /F1 12 Tf 72 720 Td (/JS /Launch) Tj ET /JS"))
    assert v.ok, v.reason


def test_not_a_pdf_too_big_too_long_malformed(monkeypatch):
    assert filescan.check_pdf(b"MZ\x90\x00 an exe").reason == "not actually a PDF"
    assert filescan.check_pdf(b"%PDF-1.4 garbage").reason == "malformed PDF"
    monkeypatch.setattr(filescan, "MAX_PDF_PAGES", 0)
    assert "pages" in filescan.check_pdf(_pdf()).reason
    monkeypatch.setattr(filescan, "MAX_FILE_BYTES", 100)
    assert "larger than" in filescan.check_pdf(_pdf()).reason


def test_password_protected_is_blocked_empty_password_is_fine():
    for pw, ok in (("secret", False), ("", True)):
        w = pypdf.PdfWriter()
        w.add_blank_page(612, 792)
        w.encrypt(user_password=pw, owner_password="owner")
        buf = io.BytesIO()
        w.write(buf)
        assert filescan.check_pdf(buf.getvalue()).ok is ok, pw


# ------------------------------------------------------------------ clamav-rest client

class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_clamav_answers(monkeypatch):
    def answer(status, body):
        def urlopen(req, timeout=0):
            assert req.full_url == "http://av.test/scan"
            if status == 200:
                return _Resp(200, body)
            raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(body))
        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    answer(200, b'{"Status":"OK","Description":""}')
    assert filescan.clamav_scan("http://av.test/", "m.eml", b"x") is None
    answer(406, b'{"Status":"FOUND","Description":"Eicar-Test-Signature"}')
    assert filescan.clamav_scan("http://av.test", "m.eml", b"x") == "Eicar-Test-Signature"
    answer(406, b'[{"Status":"FOUND","Description":"Win.Trojan"}]')        # the v2 shape
    assert filescan.clamav_scan("http://av.test", "m.eml", b"x") == "Win.Trojan"
    for status, body in ((500, b"oops"), (200, b'"what"'), (200, b'{"Status":"ERROR"}')):
        answer(status, body)
        with pytest.raises(filescan.ScanUnavailable):
            filescan.clamav_scan("http://av.test", "m.eml", b"x")

    def down(req, timeout=0):
        raise urllib.error.URLError("refused")
    monkeypatch.setattr(urllib.request, "urlopen", down)
    with pytest.raises(filescan.ScanUnavailable):
        filescan.clamav_scan("http://av.test", "m.eml", b"x")
    assert filescan.Scanner("").scan_message(b"x") is None      # no AV configured


# ------------------------------------------------------------------ the pipeline

def test_clean_email_and_pdf_are_extracted():
    llm, av = _LLM(), _AV()
    r = pipeline.extract_ingest_payload(_email(_pdf()), llm=llm, scanner=av)
    assert r.ok and av.calls == 1
    assert "Amtrak eTicket 1234" in llm.docs[0]


def test_infected_email_is_quarantined_and_nothing_extracted():
    llm = _LLM()
    r = pipeline.extract_ingest_payload(_email(_pdf()), llm=llm,
                                        scanner=_AV("Eicar-Test-Signature"))
    assert not r.ok and r.needs_review and llm.docs == []
    assert any("quarantined" in x and "Eicar-Test-Signature" in x for x in r.reasons)


def test_scanner_down_skips_attachments_but_reads_the_body():
    llm = _LLM()
    r = pipeline.extract_ingest_payload(_email(_pdf()), llm=llm, scanner=_AV(down=True))
    assert r.ok and "Amtrak eTicket 1234" not in llm.docs[0]
    assert "Your Amtrak trip" in llm.docs[0]
    assert any("antivirus unavailable" in t for t in r.trace)


def test_active_pdf_is_dropped_and_flagged_for_review():
    llm = _LLM()
    bad = _pdf(catalog_extra=b"/OpenAction << /S /Launch /F (cmd.exe) >>")
    r = pipeline.extract_ingest_payload(_email(bad), llm=llm, scanner=_AV())
    assert r.needs_review and "Amtrak eTicket 1234" not in llm.docs[0]
    assert any("'ticket.pdf' blocked" in x and "Launch" in x for x in r.reasons)


def test_a_ticket_with_a_viewer_script_is_read_with_a_note():
    llm = _LLM()
    pdf = _pdf(catalog_extra=b"/OpenAction << /S /JavaScript /JS (this.zoom = 100;) >>")
    r = pipeline.extract_ingest_payload(_email(pdf), llm=llm, scanner=_AV())
    assert r.ok and "Amtrak eTicket 1234" in llm.docs[0]
    assert any("JavaScript" in t for t in r.trace)


def test_static_checks_run_without_antivirus():
    llm = _LLM()
    bad = _pdf(catalog_extra=b"/OpenAction << /S /Launch /F (x) >>")
    r = pipeline.extract_ingest_payload(_email(bad), llm=llm)     # no scanner passed
    assert any("blocked" in x for x in r.reasons)
    assert "Amtrak eTicket 1234" not in llm.docs[0]
