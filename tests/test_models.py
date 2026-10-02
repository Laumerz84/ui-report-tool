import json

import pytest

from uireport.geometry import IntRect
from uireport.models import (
    AnnotationType,
    ArrowAnn,
    CaptureMode,
    MonitorMeta,
    PinAnn,
    RectAnn,
    RedactAnn,
    Role,
    RulerAnn,
    Session,
    Shot,
    SystemMeta,
    WindowMeta,
    annotation_from_dict,
    monitor_at_point,
    monitor_for_rect,
)


def _full_shot() -> Shot:
    mon = MonitorMeta(index=2, name=r"\\.\DISPLAY2", is_primary=False, rect=IntRect(-2560, 0, 2560, 1440), dpi=144)
    win = WindowMeta(
        title="Settings - My App",
        process_name="chrome.exe",
        exe_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        pid=4242,
        rect=IntRect(-2400, 40, 1500, 900),
        dpi=144,
        browser="chrome",
        url="http://localhost:3000/settings",
    )
    shot = Shot(
        role=Role.WANT,
        caption="Button text is clipped",
        capture_mode=CaptureMode.DELAYED,
        selection="window",
        capture_rect=IntRect(-2400, 40, 1500, 900),
        monitor=mon,
        window=win,
        original_path=r"C:\r\01.png",
        annotated_path=r"C:\r\01_annotated.png",
    )
    shot.image_width, shot.image_height = 1500, 900
    shot.add_annotation(PinAnn(x=412, y=88, note="clipped", color="#1F2937"))
    shot.add_annotation(RectAnn.from_points(10, 20, 110, 70))
    shot.add_annotation(ArrowAnn(x1=5, y1=5, x2=50, y2=60))
    shot.add_annotation(RulerAnn(x1=0, y1=0, x2=30, y2=40))
    shot.add_annotation(RedactAnn.from_points(300, 300, 200, 250))
    return shot


def test_enum_values_and_role_metadata():
    assert [r.value for r in Role] == ["shot", "problem", "want", "context", "after"]
    assert Role.SHOT.label == "Shot"
    assert Role.PROBLEM.label == "Problem"
    assert Role.AFTER.description.startswith("The result after a fix")
    assert Role.WANT.color.startswith("#")
    assert [m.value for m in CaptureMode] == ["normal", "delayed"]
    assert {t.value for t in AnnotationType} == {"pin", "rect", "arrow", "ruler", "redact"}
    assert str(Role.PROBLEM) == "problem"  # StrEnum


def test_default_role_is_the_neutral_shot():
    assert Shot().role is Role.SHOT


def test_pin_numbering_stays_contiguous():
    shot = Shot()
    a = shot.add_annotation(PinAnn(x=1, y=1))
    b = shot.add_annotation(PinAnn(x=2, y=2))
    c = shot.add_annotation(PinAnn(x=3, y=3))
    shot.add_annotation(RectAnn(x=0, y=0, w=5, h=5))  # non-pins don't take numbers
    assert [p.n for p in shot.pins] == [1, 2, 3]
    shot.remove_annotation(b.id)
    assert [(p.id, p.n) for p in shot.pins] == [(a.id, 1), (c.id, 2)]


def test_ruler_length_and_derived_fields():
    r = RulerAnn(x1=0, y1=0, x2=30, y2=40)
    assert (r.dx, r.dy, r.length_px) == (30, 40, 50.0)
    d = r.to_dict()
    assert d["length_px"] == 50.0 and d["dx"] == 30 and d["dy"] == 40


def test_rect_from_points_normalises():
    r = RectAnn.from_points(110, 70, 10, 20)
    assert (r.x, r.y, r.w, r.h) == (10, 20, 100, 50)
    rd = RedactAnn.from_points(300, 300, 200, 250)
    assert (rd.x, rd.y, rd.w, rd.h) == (200, 250, 100, 50)


def test_shot_json_round_trip_is_lossless():
    shot = _full_shot()
    d = shot.to_dict()
    text = json.dumps(d)  # must be JSON-serialisable
    shot2 = Shot.from_dict(json.loads(text))
    assert shot2.to_dict() == d
    assert shot2.role is Role.WANT
    assert shot2.capture_mode is CaptureMode.DELAYED
    assert shot2.monitor.scale_percent == 150
    assert shot2.window.url == "http://localhost:3000/settings"
    assert shot2.window.size_logical == (1000, 600)  # 1500x900 physical @150%
    assert [type(a) for a in shot2.annotations] == [PinAnn, RectAnn, ArrowAnn, RulerAnn, RedactAnn]
    assert shot2.pins[0].color == "#1F2937"
    assert shot2.image is None  # runtime-only field is not serialised


def test_shot_dict_contains_spec_metadata():
    d = _full_shot().to_dict()
    assert d["image_size"] == {"width_px": 1500, "height_px": 900}
    assert d["capture_rect"] == {"x": -2400, "y": 40, "w": 1500, "h": 900}
    assert d["monitor"]["scale_percent"] == 150
    assert d["monitor"]["width_px"] == 2560 and d["monitor"]["height_px"] == 1440
    assert d["window"]["size_logical"] == {"width": 1000, "height": 600}
    assert d["files"]["annotated"].endswith("01_annotated.png")
    assert d["capture_mode"] == "delayed"
    assert d["timestamp"]


def test_unknown_shot_keys_and_annotation_types_are_tolerated():
    d = _full_shot().to_dict()
    d["future_field"] = {"a": 1}
    d["annotations"].append({"type": "hologram", "id": "zz"})
    shot = Shot.from_dict(d)
    assert len(shot.annotations) == 5  # unknown type skipped
    assert shot.to_dict()["future_field"] == {"a": 1}  # unknown key preserved


def test_annotation_from_dict_rejects_unknown_type():
    with pytest.raises(ValueError):
        annotation_from_dict({"type": "nope"})


def test_session_round_trip_and_fields():
    sess = Session.new(project_path=r"C:\dev\app", framework_hint="React + Tailwind")
    sess.goal = "Fix the clipped button"
    sess.expected = "Full label visible"
    sess.actual = "Label cut off"
    sess.system = SystemMeta(
        windows_version="Windows 11 Pro 10.0.26200",
        windows_build=26200,
        theme_apps="dark",
        theme_system="dark",
        monitors=[MonitorMeta(index=1, name=r"\\.\DISPLAY1", is_primary=True, rect=IntRect(0, 0, 1920, 1080), dpi=96)],
    )
    sess.add_shot(_full_shot())
    sess.add_shot(Shot(caption="second"))
    d = sess.to_dict()
    assert d["schema_version"] == 1
    assert d["tool"]["name"] and d["tool"]["version"]
    assert d["session"]["shot_count"] == 2
    assert d["session"]["system"]["theme_apps"] == "dark"
    sess2 = Session.from_dict(json.loads(json.dumps(d)))
    assert sess2.to_dict() == d
    assert sess2.project_path == r"C:\dev\app" and sess2.framework_hint == "React + Tailwind"
    assert [s.index for s in sess2.shots] == [1, 2]


def test_session_add_remove_move_reorder():
    sess = Session()
    a, b, c, d = (Shot(caption=x) for x in "abcd")
    for s in (a, b, c, d):
        sess.add_shot(s)
    assert [s.index for s in sess.shots] == [1, 2, 3, 4]
    assert sess.move_shot(d.id, 0)
    assert [s.caption for s in sess.shots] == ["d", "a", "b", "c"]
    assert [s.index for s in sess.shots] == [1, 2, 3, 4]
    sess.reorder([c.id, b.id, a.id, d.id])
    assert [s.caption for s in sess.shots] == ["c", "b", "a", "d"]
    assert sess.remove_shot(b.id) is b
    assert [s.caption for s in sess.shots] == ["c", "a", "d"]
    assert [s.index for s in sess.shots] == [1, 2, 3]
    assert sess.remove_shot("nope") is None
    assert sess.move_shot("nope", 0) is False
    with pytest.raises(ValueError):
        sess.reorder([a.id])  # not a permutation
    assert sess.get_shot(a.id) is a and sess.index_of(a.id) == 1


def test_session_handles_many_shots():
    sess = Session()
    for i in range(25):
        sess.add_shot(Shot(caption=str(i)))
    sess.move_shot(sess.shots[24].id, 3)
    sess.remove_shot(sess.shots[10].id)
    assert len(sess.shots) == 24
    assert [s.index for s in sess.shots] == list(range(1, 25))


def test_monitor_meta_scale_and_logical_size():
    m = MonitorMeta(rect=IntRect(0, 0, 3840, 2160), dpi=144)
    assert m.scale_factor == 1.5 and m.scale_percent == 150
    assert m.logical_size == (2560, 1440)
    assert MonitorMeta(dpi=120).scale_percent == 125


def test_shot_to_logical_uses_monitor_scale():
    shot = _full_shot()
    assert shot.to_logical(150) == 100.0
    assert Shot().to_logical(150) == 150.0  # no monitor -> scale 1.0


def test_monitor_lookup_helpers():
    mons = [
        MonitorMeta(index=1, name="A", is_primary=True, rect=IntRect(0, 0, 1920, 1080), dpi=96),
        MonitorMeta(index=2, name="B", rect=IntRect(1920, 0, 2560, 1440), dpi=144),
        MonitorMeta(index=3, name="C", rect=IntRect(-1280, 100, 1280, 1024), dpi=96),
    ]
    assert monitor_at_point(mons, 10, 10).name == "A"
    assert monitor_at_point(mons, 2000, 10).name == "B"
    assert monitor_at_point(mons, -5, 200).name == "C"
    assert monitor_at_point(mons, 1920, 0).name == "B"  # right edge is exclusive
    assert monitor_at_point(mons, 9999, 9999).name == "B"  # nearest fallback
    assert monitor_for_rect(mons, IntRect(1800, 0, 400, 100)).name == "B"  # larger overlap
    assert monitor_at_point([], 0, 0) is None
