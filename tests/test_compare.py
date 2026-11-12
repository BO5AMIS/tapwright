"""The picture diff of `compare_screen` (AGENT-TOOLS §14): synthetic pictures, no phone."""
import io
import time

import pytest
from PIL import Image

from tapwright import compare
from tapwright.control import Refused
from fakes import el

SIZE = (600, 1304)
WHITE, BLACK = (255, 255, 255), (0, 0, 0)


def screen(*blocks, size=SIZE, colour=WHITE):
    img = Image.new("RGB", size, colour)
    for box, paint in blocks:
        img.paste(paint, box)
    return img


def test_identical_pictures_have_no_changed_pixel():
    r = compare.compare(screen(), screen())
    assert (r.changed, r.regions, r.percent) == (0, [], 0.0)
    assert compare.shown(r.percent, r.changed) == "0%"


def test_a_changed_block_is_one_region_joined_from_16_px_cells():
    r = compare.compare(screen(), screen(((100, 200, 300, 260), BLACK)))
    assert r.changed == 200 * 60
    assert r.regions == [compare.Region(96, 192, 208, 80)]  # every cell the block touches
    assert round(r.percent, 2) == 1.53 and compare.shown(r.percent, r.changed) == "1.53%"


def test_a_tiny_change_is_never_reported_as_none_and_a_faint_one_is_below_the_tolerance():
    one = screen()
    one.putpixel((599, 1303), BLACK)  # the last pixel of a picture that doesn't divide by 16
    r = compare.compare(screen(), one)
    assert r.changed == 1 and r.regions == [compare.Region(592, 1296, 8, 8)]
    assert compare.shown(r.percent, r.changed) == "0.01%"
    assert compare.compare(screen(), screen(colour=(255 - 25, 255, 255))).changed == 0  # 25 of 255 is under 0.1
    assert compare.compare(screen(), screen(colour=(255 - 26, 255, 255))).changed == SIZE[0] * SIZE[1]


def test_cells_touching_corner_to_corner_are_one_region_and_far_ones_are_not():
    r = compare.compare(screen(), screen(((10, 10, 20, 20), BLACK), ((20, 20, 40, 40), BLACK),
                                         ((400, 800, 410, 810), BLACK)))
    assert sorted(r.regions) == [compare.Region(0, 0, 48, 48), compare.Region(400, 800, 16, 16)]
    assert r.regions[0].width * r.regions[0].height >= r.regions[1].width * r.regions[1].height  # largest first


def test_painted_out_rects_are_neutral_on_both_pictures():
    changed = screen(((100, 200, 300, 260), BLACK))
    assert compare.compare(screen(), changed, [(90, 190, 220, 80)]).changed == 0
    assert compare.compare(screen(), changed, [(90, 190, 100, 80)]).changed > 0  # half covered: the rest shows
    assert compare.compare(screen(), changed, [(0, 0, 0, 0), (-5, -5, 3, 3)]).changed == 200 * 60  # empty, off-picture


def test_the_status_bar_painted_out_hides_a_clock_change_and_nothing_below_it():
    clock = ((500, 5, 590, 30), BLACK)
    assert compare.compare(screen(), screen(clock)).changed > 0
    assert compare.compare(screen(), screen(clock), [(0, 0, 600, 75)]).changed == 0
    assert compare.compare(screen(), screen(clock, ((10, 80, 60, 90), BLACK)), [(0, 0, 600, 75)]).changed == 500


def test_pictures_of_another_size_are_refused():
    with pytest.raises(ValueError):
        compare.compare(screen(), screen(size=(600, 1200)))


def test_it_is_fast_on_a_phone_sized_picture():
    before, after = screen(), screen(((50, 100, 550, 160), BLACK), ((50, 900, 300, 1000), (200, 0, 0)))
    began = time.perf_counter()
    r = compare.compare(before, after, [(0, 0, 600, 75)])
    r.picture()
    assert time.perf_counter() - began < 0.3
    assert len(r.regions) == 2


def test_the_picture_fades_the_screen_and_tints_the_changes_red_inside_an_outline():
    r = compare.compare(screen(), screen(((100, 200, 300, 260), (0, 0, 200))))
    out = Image.open(io.BytesIO(r.picture("PNG"))).convert("RGB")
    assert out.size == SIZE
    assert out.getpixel((10, 600)) == WHITE  # unchanged white stays white
    red, green, blue = out.getpixel((200, 230))  # a changed pixel: the blue screen, tinted red
    assert red > 120 and green < 20 and blue > 70 and red > blue
    assert out.getpixel((96, 192)) == (255, 0, 0) and out.getpixel((97, 193)) == (255, 0, 0)  # the 2 px outline
    faded = compare.compare(screen(((0, 600, 50, 650), (100, 100, 100))), screen(((0, 600, 50, 650), (100, 100, 100))))
    assert faded.changed == 0
    assert Image.open(io.BytesIO(faded.picture("PNG"))).convert("RGB").getpixel((10, 610)) == (193, 193, 193)  # 100 -> towards white by 0.6


def test_at_most_eight_region_lines_and_the_rest_counted():
    blocks = [((20 + 60 * i, 20, 40 + 60 * i, 40), BLACK) for i in range(10)]  # ten blocks, far enough apart
    r = compare.compare(screen(), screen(*blocks))
    assert len(r.regions) == 10
    lines = compare.region_lines(r.regions, 1.0, [])
    assert len(lines) == 9 and lines[-1] == "(+2 more regions)" and lines[0].startswith("- @")
    text = compare.failure("home", r, 0.5, 1.0)
    assert text.startswith('0.51% changed from the reference "home" (threshold 0.5%), in 10 regions (points): @') \
        and text.endswith("(+2 more regions).")
    one = compare.failure("home", compare.compare(screen(), screen(((100, 200, 300, 260), BLACK))), 0.5, 2.0)
    assert one == ('1.53% changed from the reference "home" (threshold 0.5%), in 1 region (points): @48,96 104x40.')


def test_regions_name_the_two_smallest_named_elements_they_touch():
    box = (16, 300, 193, 48)
    items = [el("e1", "other", "", "", rect=(0, 0, 400, 800)),  # a page-sized container with no name
             el("e2", "button", "Save", "lab-save", rect=(16, 329, 193, 48)),
             el("e3", "text", "Total", "", rect=(16, 320, 300, 100)),
             el("e4", "text", "Page", "", rect=(0, 0, 400, 800)),  # named but far larger than the change
             el("e5", "text", "Elsewhere", "", rect=(300, 600, 50, 20))]
    assert compare.named_near(box, items) == ['button "Save" id=lab-save', 'text "Total"']
    r = compare.Region(24, 450, 290, 72)  # 1.5 px per point: @16,300 193x48
    assert compare.region_lines([r], 1.5, items) == ['- @16,300 193x48: button "Save" id=lab-save, text "Total"']
    assert compare.region_lines([r], 1.5, []) == ["- @16,300 193x48"]


def test_names_and_parameters_are_checked():
    for good in ("home", "a", "Cart.v2", "step-1_ok", "a" * 61):
        compare.check_params({"name": good})
    for bad in ("", "-x", ".x", "a b", "a/b", "a" * 62, 5, None):
        with pytest.raises(Refused) as e:
            compare.check_params({"name": bad})
        assert e.value.code == "bad_params"
    compare.check_params({"name": "x", "threshold": 0, "update": True, "ignore": [{"id": "c"}, {"x": 1, "y": 2, "width": 3,
                                                                                               "height": 4}]})
    for extra in ({"threshold": -1}, {"threshold": "1"}, {"update": 1}, {"ignore": "clock"}, {"ignore": ["clock"]},
                  {"ignore": [{"id": "a", "label": "b"}]}):
        with pytest.raises(Refused):
            compare.check_params({"name": "x", **extra})


def test_ignored_targets_resolve_on_the_list_and_a_missing_one_is_skipped_with_a_note():
    items = [el("e1", "text", "9:41", "clock", rect=(150, 400, 100, 20)), el("e2", "button", "Save", "", rect=(0, 0, 50, 20))]
    rects, notes = compare.ignored(items, [{"id": "clock"}, {"label": "Save"}, {"text": "gone"},
                                           {"x": 1, "y": 2, "width": 3, "height": 4}, {"ref": "e1"}])
    assert rects == [(150, 400, 100, 20), (0, 0, 50, 20), (1.0, 2.0, 3.0, 4.0), (150, 400, 100, 20)]
    assert notes == ['ignore text "gone" isn\'t on the screen; skipped.']
    with pytest.raises(Refused) as e:
        compare.ignored([*items, el("e3", "text", "9:41", "clock", rect=(0, 0, 5, 5))], [{"id": "clock"}])
    assert e.value.code == "ambiguous"


def test_references_live_in_the_store_per_app_and_a_scripts_folder_comes_first(tmp_path, monkeypatch):
    monkeypatch.setattr(compare, "references_dir", lambda: tmp_path / "refs")
    assert compare.reference_path("com.example.app", "home") == tmp_path / "refs" / "com.example.app" / "home.png"
    assert compare.find("home", "com.example.app") is None
    compare.save(compare.reference_path("com.example.app", "home"), screen())
    script = tmp_path / "flows" / "buy.json"
    assert compare.find("home", "com.example.app") == compare.reference_path("com.example.app", "home")
    assert compare.find("home", "com.example.app", script) == compare.reference_path("com.example.app", "home")
    compare.save(compare.script_references(script) / "home.png", screen())
    assert compare.find("home", "com.example.app", script) == tmp_path / "flows" / "buy.references" / "home.png"
    assert compare.script_diffs(script) == tmp_path / "flows" / "buy.diff"
    folder, copied = compare.copy_to_script(["home", "home", "other"], "com.example.app", tmp_path / "x" / "s.json")
    assert (folder, copied) == (tmp_path / "x" / "s.references", 1) and (folder / "home.png").is_file()
    assert compare.load(folder / "home.png").size == SIZE
    (folder / "bad.png").write_bytes(b"not a picture")
    with pytest.raises(Refused) as e:
        compare.load(folder / "bad.png")
    assert e.value.code == "bad_params"
