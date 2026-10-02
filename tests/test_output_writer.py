"""writer / render / report (package C, output half). Synthetic images only."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest
from PySide6.QtGui import QColor, QImage

from uireport import imageops
from uireport.geometry import IntRect
from uireport.models import (
    ArrowAnn,
    CaptureMode,
    MonitorMeta,
    PinAnn,
    RectAnn,
    RedactAnn,
    Role,
    RulerAnn,
    Session,
    SystemMeta,
    WindowMeta,
)
from uireport.output import render as render_mod
from uireport.output import writer as writer_mod
from uireport.output.render import render_annotated, render_original, save_png
from uireport.output.report import build_report_dict, build_report_md
from uireport.output.writer import SessionOutput, write_latest_pointer, write_session

NOW = datetime(2026, 9, 29, 14, 32, 5)


def load_png(path: Path) -> QImage:
    img = QImage(str(path))
    assert not img.isNull(), f"cannot read {path}"
    return img


def hex_at(img: QImage, x: int, y: int) -> str:
    return imageops.sample_hex(img, x, y)


def noisy_image(w: int, h: int) -> QImage:
    """High-frequency, deterministic content that pixelation visibly destroys."""
    img = QImage(w, h, QImage.Format.Format_ARGB32)
    for y in range(h):
        for x in range(w):
            r = (x * 37 + y * 11) % 256
            g = (x * 5 + y * 71) % 256
            b = ((x ^ y) * 13) % 256
            img.setPixelColor(x, y, QColor(r, g, b))
    return img


def flat_image(w: int, h: int, color: str) -> QImage:
    img = QImage(w, h, QImage.Format.Format_ARGB32)
    img.fill(QColor(color))
    return img


def diff_bbox(a: QImage, b: QImage, threshold: int = 8):
    """Bounding box (min_x, min_y, max_x, max_y) of pixels whose channels differ by > threshold."""
    assert a.size() == b.size()
    xs, ys = [], []
    for y in range(a.height()):
        for x in range(a.width()):
            ca, cb = a.pixelColor(x, y), b.pixelColor(x, y)
            if max(abs(ca.red() - cb.red()), abs(ca.green() - cb.green()), abs(ca.blue() - cb.blue())) > threshold:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def make_rich_session(make_shot, n: int = 13) -> Session:
    s = Session(
        goal="Fix the clipped Save button",
        project_path=r"C:\dev\app",
        framework_hint="React + Tailwind",
        expected="Full label visible",
        actual="Label cut off at 150% scale",
    )
    s.system = SystemMeta(
        windows_version="Windows 11 Pro 10.0.26200",
        windows_build=26200,
        theme_apps="dark",
        theme_system="dark",
        monitors=[
            MonitorMeta(index=1, name=r"\\.\DISPLAY1", is_primary=True, rect=IntRect(0, 0, 5120, 1440), dpi=96),
            MonitorMeta(index=2, name=r"\\.\DISPLAY2", rect=IntRect(5120, 0, 3840, 2160), dpi=144),
        ],
    )
    roles = [Role.PROBLEM, Role.WANT, Role.CONTEXT, Role.AFTER]
    for i in range(n):
        scale = 150 if i % 3 == 1 else 100
        shot = make_shot(160 + i * 4, 100 + i * 2, scale, caption=f"Shot {i + 1} caption", role=roles[i % 4],
                         capture_mode=CaptureMode.DELAYED if i % 2 else CaptureMode.NORMAL)
        shot.add_annotation(PinAnn(x=10 + i, y=20, note=f"note {i}", color="#123456"))
        if i % 2 == 0:
            shot.add_annotation(RectAnn(x=5, y=5, w=40, h=30))
            shot.add_annotation(ArrowAnn(x1=5, y1=5, x2=60, y2=50))
        if i % 3 == 0:
            shot.add_annotation(RulerAnn(x1=10, y1=60, x2=110, y2=60))
        if i % 4 == 0:
            shot.add_annotation(RedactAnn(x=100, y=60, w=30, h=20))
        s.add_shot(shot)
    return s


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------
def test_render_original_only_redacts_and_never_touches_the_input(qapp, make_shot):
    shot = make_shot(120, 80)
    shot.set_image(noisy_image(120, 80))
    shot.add_annotation(PinAnn(x=10, y=10))
    shot.add_annotation(RedactAnn(x=20, y=20, w=60, h=30))
    before = shot.image.copy()
    out = render_original(shot)
    assert out.size() == shot.image.size()
    assert shot.image == before  # input untouched
    assert out != before  # redaction applied
    # pin is NOT drawn in the "original"
    assert hex_at(out, 10, 10) == hex_at(before, 10, 10)


def test_render_annotated_paints_marks_and_matches_size(qapp, make_shot):
    shot = make_shot(120, 80, color="#FFFFFF")
    shot.add_annotation(PinAnn(x=30, y=30))
    out = render_annotated(shot)
    assert out.size() == shot.image.size()
    assert hex_at(out, 30 + 9, 30) != "#FFFFFF"


def test_render_without_image_raises(qapp, make_shot):
    shot = make_shot(10, 10)
    shot.image = None
    with pytest.raises(ValueError):
        render_original(shot)
    with pytest.raises(ValueError):
        render_annotated(shot)


def test_save_png_opaque_rgb_and_creates_parents(qapp, test_out):
    img = flat_image(20, 10, "#336699")  # ARGB32 buffer, fully opaque
    target = test_out / "a" / "b" / "x.png"
    save_png(img, target)
    assert target.is_file()
    back = load_png(target)
    assert back.size() == img.size()
    assert not back.hasAlphaChannel()  # stored as plain RGB
    assert hex_at(back, 3, 3) == "#336699"
    assert not list(target.parent.glob("*.tmp"))


def test_save_png_keeps_real_alpha(qapp, test_out):
    img = QImage(10, 10, QImage.Format.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    img.setPixelColor(1, 1, QColor(255, 0, 0, 128))
    target = test_out / "alpha.png"
    save_png(img, target)
    back = load_png(target).convertToFormat(QImage.Format.Format_ARGB32)
    assert back.pixelColor(1, 1).alpha() == 128
    assert back.pixelColor(5, 5).alpha() == 0


def test_save_png_raises_oserror_on_failure(qapp, test_out):
    blocker = test_out / "afile"
    blocker.write_text("x")
    with pytest.raises(OSError):
        save_png(flat_image(4, 4, "#000000"), blocker / "x.png")  # parent is a file
    with pytest.raises(OSError):
        save_png(QImage(), test_out / "empty.png")


def test_has_real_alpha(qapp):
    assert not render_mod.has_real_alpha(flat_image(8, 8, "#102030"))
    img = flat_image(8, 8, "#102030")
    img.setPixelColor(7, 7, QColor(1, 2, 3, 254))
    assert render_mod.has_real_alpha(img)
    assert not render_mod.has_real_alpha(img.convertToFormat(QImage.Format.Format_RGB32))


# ---------------------------------------------------------------------------
# write_session: files, sizes, reports
# ---------------------------------------------------------------------------
def test_write_session_12_plus_shots_exact_file_set_and_sizes(qapp, test_out, make_shot):
    session = make_rich_session(make_shot, 13)
    root = test_out / "reports"
    progress: list[tuple[int, int]] = []
    out = write_session(session, root, now=NOW, progress=lambda d, t: progress.append((d, t)))

    assert isinstance(out, SessionOutput)
    assert out.folder == root / "2026-09-29_1432_fix-the-clipped-save-button"
    assert out.folder.is_dir()
    expected = {"report.md", "report.json"}
    for i in range(1, 14):
        expected |= {f"{i:02d}.png", f"{i:02d}_annotated.png"}
    assert {p.name for p in out.folder.iterdir()} == expected  # exact set, no stray temp files
    assert len(out.image_files) == 26
    assert [p.name for p in out.image_files[:4]] == ["01.png", "01_annotated.png", "02.png", "02_annotated.png"]
    assert out.report_md == out.folder / "report.md" and out.report_json == out.folder / "report.json"
    assert progress == [(i, 13) for i in range(1, 14)]

    # every PNG has exactly the source size (never upscaled), also for the 150% shots
    for i, shot in enumerate(session.shots, start=1):
        for name in (f"{i:02d}.png", f"{i:02d}_annotated.png"):
            img = load_png(out.folder / name)
            assert (img.width(), img.height()) == (shot.image.width(), shot.image.height()), name
            assert (img.width(), img.height()) == (shot.image_width, shot.image_height)
    assert session.shots[1].monitor.scale_percent == 150
    assert session.shots[4].monitor.scale_percent == 150

    # latest pointer
    latest = json.loads((root / "latest.json").read_text(encoding="utf-8"))
    assert latest["folder"] == str(out.folder)
    assert latest["report_md"] == str(out.report_md)
    assert latest["report_json"] == str(out.report_json)
    assert latest["shot_count"] == 13
    assert latest["goal"] == "Fix the clipped Save button"
    assert latest["created_at"].startswith("2026-09-29T14:32:05")
    assert all(Path(latest[k]).is_absolute() for k in ("folder", "report_md", "report_json"))

    # only inside output_root
    assert [p.name for p in test_out.iterdir()] == ["reports"]
    assert {p.name for p in root.iterdir()} == {out.folder.name, "latest.json"}


def test_write_session_sets_absolute_backslash_paths(qapp, test_out, make_shot):
    session = make_rich_session(make_shot, 3)
    out = write_session(session, test_out / "r", now=NOW)
    assert session.folder == str(out.folder)
    assert "/" not in session.folder
    for i, shot in enumerate(session.shots, start=1):
        assert shot.original_path == str(out.folder / f"{i:02d}.png")
        assert shot.annotated_path == str(out.folder / f"{i:02d}_annotated.png")
        assert Path(shot.original_path).is_absolute() and Path(shot.original_path).is_file()
        assert Path(shot.annotated_path).is_file()


def test_report_json_round_trips_through_session_from_dict(qapp, test_out, make_shot):
    session = make_rich_session(make_shot, 6)
    out = write_session(session, test_out / "r", now=NOW)
    raw = out.report_json.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")  # no BOM
    doc = json.loads(raw.decode("utf-8"))
    assert doc == build_report_dict(session) == session.to_dict()
    assert doc["schema_version"] == 1
    back = Session.from_dict(doc)
    assert back.to_dict() == doc
    assert back.folder == session.folder
    assert [s.index for s in back.shots] == [1, 2, 3, 4, 5, 6]
    assert back.shots[0].pins[0].note == "note 0"
    assert back.shots[0].annotated_path.endswith("01_annotated.png")
    assert back.shots[3].role == Role.AFTER


def test_report_md_has_every_required_element(qapp, test_out, make_shot):
    session = make_rich_session(make_shot, 4)
    # a shot with a browser URL and a window on a 150% monitor, plus multiline caption
    shot = session.shots[1]
    shot.caption = "Zażółć gęślą\nsecond line of the caption"
    shot.window = WindowMeta(
        title="Settings - My App",
        process_name="chrome.exe",
        exe_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        rect=IntRect(100, 50, 1500, 900),
        dpi=144,
        browser="chrome",
        url="http://localhost:3000/settings",
    )
    shot.add_annotation(RulerAnn(x1=10, y1=20, x2=110, y2=20))
    out = write_session(session, test_out / "r", now=NOW)
    raw = out.report_md.read_bytes()
    assert b"\r" not in raw  # LF only
    md = raw.decode("utf-8")
    folder = str(out.folder)

    assert md.startswith("# Report: Fix the clipped Save button\n")
    for needle in (
        "- **Goal:** Fix the clipped Save button",
        r"- **Project folder:** C:\dev\app",
        "- **App / framework:** React + Tailwind",
        "- **Expected:** Full label visible",
        "- **Actual:** Label cut off at 150% scale",
        "- **Created:** ",
        "4 screenshots",
        "Windows 11 Pro 10.0.26200",
        "apps theme dark",
        "tool v0.1.0",
        r"1: \\.\DISPLAY1 (5120x1440 @ 100%, primary",
        r"2: \\.\DISPLAY2 (3840x2160 @ 150%",
        "How to read this",
        "Problem",
        "Want",
        "Context",
        "After",
        "ORIGINAL image",
    ):
        assert needle in md, needle

    assert "## 1. [Problem] Shot 1 caption" in md
    assert "## 2. [Want] Zażółć gęślą" in md  # headline is the first caption line only
    assert "## 3. [Context] Shot 3 caption" in md
    assert "## 4. [After] Shot 4 caption" in md
    assert md.index("## 1.") < md.index("## 2.") < md.index("## 3.") < md.index("## 4.")
    assert "Zażółć gęślą\n  second line of the caption" in md  # continuation indented under the bullet

    for i in range(1, 5):
        assert f"- **Annotated image:** {folder}\\{i:02d}_annotated.png" in md
        assert f"- **Original image:** {folder}\\{i:02d}.png" in md
    assert "/" not in md.split("- **Annotated image:** ")[1].split("\n")[0]

    # capture info
    assert "normal capture" in md and "delayed capture" in md
    assert "region 160x100 px at screen (100, 50)" in md
    assert "- **Monitor:** 1 (" in md and "scale 100%" in md
    # window: title, process, exe, px + logical @150%
    assert '"Settings - My App" - chrome.exe (C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe)' in md
    assert "1500x900 px (1000x600 logical @150%)" in md
    assert "- **URL:** http://localhost:3000/settings" in md
    assert "Demo - Example App" in md and "example.exe" in md  # default demo window from make_shot

    # pins / measurements / rects / arrows / redactions
    assert "**Pins**" in md
    assert "- Pin 1 at (10, 20), color #123456: note 0" in md
    assert "1 at (10, 20), color #123456: note 0" in md  # the spec's literal form
    assert "**Measurements**" in md
    assert "- Ruler 1: (10, 60) -> (110, 60) = 100 px" in md  # shot 1, 100% => no logical suffix
    assert "- Ruler 1: (10, 20) -> (110, 20) = 100 px (66.7 logical px at 150%)" in md  # shot 2, 150%
    assert "**Rectangles**" in md and "- Rect 1: x=5, y=5, 40x30" in md
    assert "**Arrows**" in md and "- Arrow 1: (5, 5) -> (60, 50)" in md
    assert "**Redactions:** 1 region redacted in both images." in md
    assert "*No annotations on this shot.*" not in md  # every shot here has a pin


def test_report_md_omits_empty_session_fields_and_handles_bare_shots(qapp, make_shot):
    session = Session()
    shot = make_shot(50, 50)
    shot.window = None
    shot.monitor = None
    session.add_shot(shot)
    md = build_report_md(session, Path(r"C:\out\2026-09-29_1432_session"))
    assert md.startswith("# Screenshots\n")
    for label in ("Goal", "Project folder", "App / framework", "Expected", "Actual"):
        assert f"**{label}:**" not in md
    assert "## 1. [Shot] (no caption)" in md
    assert r"C:\out\2026-09-29_1432_session\01_annotated.png" in md  # falls back to folder + name
    assert "1 screenshot" in md and "1 screenshots" not in md
    assert "*No annotations on this shot.*" in md
    assert "**Monitor:**" not in md and "**Window:**" not in md and "**URL:**" not in md


def test_write_session_report_pin_without_note_and_singular_redaction(qapp, test_out, make_shot):
    session = Session(goal="g")
    shot = make_shot(80, 60)
    shot.add_annotation(PinAnn(x=3, y=4, color="#00FF00"))
    shot.add_annotation(RedactAnn(x=0, y=0, w=10, h=10))
    shot.add_annotation(RedactAnn(x=20, y=0, w=10, h=10))
    session.add_shot(shot)
    out = write_session(session, test_out / "r", now=NOW)
    md = out.report_md.read_text(encoding="utf-8")
    assert "- Pin 1 at (3, 4), color #00FF00\n" in md
    assert "2 regions redacted in both images." in md


def test_same_minute_folders_get_numeric_suffixes(qapp, test_out, make_shot):
    root = test_out / "r"
    names = []
    for _ in range(3):
        session = Session(goal="Same goal")
        session.add_shot(make_shot(20, 20))
        names.append(write_session(session, root, now=NOW).folder.name)
    assert names == [
        "2026-09-29_1432_same-goal",
        "2026-09-29_1432_same-goal-2",
        "2026-09-29_1432_same-goal-3",
    ]
    latest = json.loads((root / "latest.json").read_text(encoding="utf-8"))
    assert latest["folder"].endswith("same-goal-3")


def test_100_plus_shots_are_zero_padded_to_three_digits(qapp, test_out, make_shot):
    session = Session(goal="many")
    for _ in range(101):
        session.add_shot(make_shot(8, 8))
    out = write_session(session, test_out / "r", now=NOW)
    names = {p.name for p in out.folder.iterdir()}
    assert {"001.png", "001_annotated.png", "101.png", "101_annotated.png", "050.png"} <= names
    assert "01.png" not in names
    assert len(names) == 101 * 2 + 2
    assert "\\001_annotated.png" in out.report_md.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# pin alignment: report.json <-> 01.png <-> 01_annotated.png
# ---------------------------------------------------------------------------
def test_pin_alignment_between_json_and_both_pngs(qapp, test_out, make_shot):
    PX, PY = 412, 88
    marker = "#3C8D2F"  # distinctive: not the pin red, not the flat background
    base = flat_image(640, 360, "#F4F1DE")
    base.setPixelColor(PX, PY, QColor(marker))
    shot = make_shot(640, 360)
    shot.set_image(base)
    color = imageops.sample_hex(shot.image, PX, PY)
    assert color == marker
    shot.add_annotation(PinAnn(x=PX, y=PY, note="button text is clipped", color=color))
    session = Session(goal="pin alignment")
    session.add_shot(shot)
    original_before = shot.image.copy()

    out = write_session(session, test_out / "r", now=NOW)

    # 1) report.json says pin at (412, 88) with the sampled colour
    doc = json.loads(out.report_json.read_text(encoding="utf-8"))
    pin = doc["shots"][0]["annotations"][0]
    assert (pin["type"], pin["n"], pin["x"], pin["y"], pin["color"]) == ("pin", 1, PX, PY, marker)
    md = out.report_md.read_text(encoding="utf-8")
    assert f"1 at ({PX}, {PY}), color {marker}: button text is clipped" in md

    # 2) 01.png (original) has that colour at exactly that pixel, and is otherwise untouched
    orig_png = load_png(out.folder / "01.png")
    assert hex_at(orig_png, PX, PY) == marker
    assert hex_at(orig_png, PX + 1, PY) == "#F4F1DE"
    assert diff_bbox(orig_png, original_before, threshold=0) is None

    # 3) 01_annotated.png: the marker is drawn centred on (412.5, 88.5), i.e. symmetric about pixel (412, 88)
    ann_png = load_png(out.folder / "01_annotated.png")
    box = diff_bbox(ann_png, orig_png)
    assert box is not None
    x0, y0, x1, y1 = box
    assert abs((x0 + x1 + 1) / 2 - (PX + 0.5)) <= 0.5, box
    assert abs((y0 + y1 + 1) / 2 - (PY + 0.5)) <= 0.5, box
    assert 20 <= (x1 - x0 + 1) <= 34  # ~ 2 * radius 12 plus halo
    assert hex_at(ann_png, PX + 9, PY) == "#E5484D"  # inside the pin disc (clear of the digit)
    assert hex_at(ann_png, PX - 9, PY) == "#E5484D"
    assert hex_at(ann_png, PX, PY - 9) == "#E5484D"
    assert hex_at(ann_png, PX, PY + 9) == "#E5484D"
    assert hex_at(ann_png, PX + 40, PY + 40) == "#F4F1DE"  # far away: untouched


def test_pin_alignment_on_a_150_percent_shot_scales_the_marker_not_the_file(qapp, test_out, make_shot):
    shot = make_shot(300, 200, scale_percent=150, color="#FFFFFF")
    shot.add_annotation(PinAnn(x=100, y=60, color="#FFFFFF"))
    session = Session(goal="hidpi")
    session.add_shot(shot)
    out = write_session(session, test_out / "r", now=NOW)
    orig = load_png(out.folder / "01.png")
    ann = load_png(out.folder / "01_annotated.png")
    assert (orig.width(), orig.height()) == (300, 200) == (ann.width(), ann.height())
    x0, y0, x1, y1 = diff_bbox(ann, orig)
    assert abs((x0 + x1 + 1) / 2 - 100.5) <= 0.5 and abs((y0 + y1 + 1) / 2 - 60.5) <= 0.5
    assert (x1 - x0 + 1) >= 32  # marker is 1.5x bigger on a 150% monitor (radius 18)


# ---------------------------------------------------------------------------
# redaction lands in BOTH images
# ---------------------------------------------------------------------------
def unique_colors(img: QImage, x: int, y: int, w: int, h: int) -> int:
    return len({img.pixel(i, j) for j in range(y, y + h) for i in range(x, x + w)})


def test_redaction_is_applied_to_both_saved_images(qapp, test_out, make_shot):
    W, H = 220, 140
    box = (60, 40, 100, 60)  # x, y, w, h
    shot = make_shot(W, H)
    shot.set_image(noisy_image(W, H))
    shot.add_annotation(RedactAnn(x=box[0], y=box[1], w=box[2], h=box[3]))
    src = shot.image.copy()
    session = Session(goal="secrets")
    session.add_shot(shot)

    out = write_session(session, test_out / "r", now=NOW)

    assert shot.image == src  # in-memory original untouched: re-editing still works
    orig = load_png(out.folder / "01.png")
    ann = load_png(out.folder / "01_annotated.png")
    assert unique_colors(src, *box) > 500  # the source really has detail there
    for name, img in (("01.png", orig), ("01_annotated.png", ann)):
        assert img.size() == src.size(), name
        assert unique_colors(img, *box) <= 20, f"{name}: redacted region still has detail"
        assert diff_bbox(img, src, threshold=0) is not None
        # pixels outside the box are identical to the source
        for y in range(H):
            for x in range(W):
                inside = box[0] <= x < box[0] + box[2] and box[1] <= y < box[1] + box[3]
                if not inside:
                    assert img.pixel(x, y) == src.pixel(x, y) | 0xFF000000, (name, x, y)
    # a redaction-only shot: both saved images are the same
    assert orig == ann


def test_redaction_plus_pin_keeps_both_effects(qapp, test_out, make_shot):
    shot = make_shot(220, 140)
    shot.set_image(noisy_image(220, 140))
    shot.add_annotation(RedactAnn(x=10, y=10, w=90, h=60))
    shot.add_annotation(PinAnn(x=180, y=100, color="#000000"))
    src = shot.image.copy()
    session = Session(goal="both")
    session.add_shot(shot)
    out = write_session(session, test_out / "r", now=NOW)
    orig = load_png(out.folder / "01.png")
    ann = load_png(out.folder / "01_annotated.png")
    assert unique_colors(ann, 10, 10, 90, 60) <= 20 and unique_colors(orig, 10, 10, 90, 60) <= 20
    # pin only in the annotated image
    assert hex_at(orig, 180 + 9, 100) == hex_at(src, 180 + 9, 100)
    assert hex_at(ann, 180 + 9, 100) == "#E5484D"
    # redaction identical in both
    for x in range(10, 100):
        for y in range(10, 70):
            assert orig.pixel(x, y) == ann.pixel(x, y)


# ---------------------------------------------------------------------------
# failure behaviour
# ---------------------------------------------------------------------------
def test_write_session_rejects_empty_session_and_missing_images(qapp, test_out, make_shot):
    root = test_out / "r"
    with pytest.raises(ValueError):
        write_session(Session(goal="x"), root, now=NOW)
    s = Session(goal="x")
    shot = make_shot(10, 10)
    shot.image = None
    s.add_shot(make_shot(10, 10))
    s.add_shot(shot)
    with pytest.raises(ValueError):
        write_session(s, root, now=NOW)
    assert not root.exists()  # validated up front: nothing was created


def test_write_failure_propagates_and_keeps_partial_output(qapp, test_out, make_shot, monkeypatch):
    session = make_rich_session(make_shot, 5)
    root = test_out / "r"
    real_save = writer_mod.save_png
    calls = {"n": 0}

    def flaky(image, path):
        calls["n"] += 1
        if calls["n"] == 5:
            raise OSError("disk full (simulated)")
        return real_save(image, path)

    monkeypatch.setattr(writer_mod, "save_png", flaky)
    with pytest.raises(OSError, match="disk full"):
        write_session(session, root, now=NOW)
    folders = [p for p in root.iterdir() if p.is_dir()]
    assert len(folders) == 1  # partial folder left in place
    names = {p.name for p in folders[0].iterdir()}
    assert {"01.png", "01_annotated.png", "02.png", "02_annotated.png"} <= names
    assert "report.md" not in names
    assert [p.name for p in test_out.iterdir()] == ["r"]
    assert not (root / "latest.json").exists()


def test_write_failure_when_root_is_a_file(qapp, test_out, make_shot):
    blocker = test_out / "not_a_folder"
    blocker.write_text("x")
    session = Session(goal="x")
    session.add_shot(make_shot(10, 10))
    with pytest.raises(OSError):
        write_session(session, blocker, now=NOW)


def test_broken_progress_callback_does_not_lose_the_report(qapp, test_out, make_shot):
    session = Session(goal="x")
    session.add_shot(make_shot(10, 10))

    def boom(done, total):
        raise RuntimeError("ui gone")

    out = write_session(session, test_out / "r", now=NOW, progress=boom)
    assert out.report_md.is_file()


def test_write_latest_pointer_overwrites(qapp, test_out, make_shot):
    root = test_out / "r"
    s1 = Session(goal="first")
    s1.add_shot(make_shot(10, 10))
    write_session(s1, root, now=NOW)
    s2 = Session(goal="second")
    s2.add_shot(make_shot(10, 10))
    s2.add_shot(make_shot(10, 10))
    out2 = write_session(s2, root, now=datetime(2026, 9, 29, 15, 0, 0))
    latest = json.loads((root / "latest.json").read_text(encoding="utf-8"))
    assert latest["goal"] == "second" and latest["shot_count"] == 2
    assert latest["folder"] == str(out2.folder)
    p = write_latest_pointer(root, out2, s2)
    assert p == root / "latest.json" and p.is_file()
    assert not list(root.glob("*.tmp"))


def test_write_session_uses_relative_output_root_as_absolute(qapp, test_out, make_shot, monkeypatch):
    monkeypatch.chdir(test_out)
    session = Session(goal="rel")
    session.add_shot(make_shot(10, 10))
    out = write_session(session, Path("rel_root"), now=NOW)
    assert out.folder.is_absolute()
    assert os.path.isabs(session.shots[0].original_path)
