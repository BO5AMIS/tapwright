"""Screenshots: resizing, the label boxes, JPEG output. Checked by size and pixel values, never by eye."""
import io

from PIL import Image

from tapwright import shot

SCREEN = (400.0, 800.0)


def png(size=(800, 1600), color=(128, 128, 128), fmt="PNG", mode="RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, fmt)
    return buf.getvalue()


def decode(jpeg: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(jpeg))
    assert img.format == "JPEG"
    return img.convert("RGB")


def red(p) -> bool:
    return p[0] > 180 and p[1] < 80 and p[2] < 80


def grey(p) -> bool:
    return all(abs(c - 128) < 12 for c in p)


def test_resizes_to_the_width_and_reports_the_size():
    jpeg, size, _ = shot.render(png(), SCREEN, 600, None)
    assert size == (600, 1200) and decode(jpeg).size == (600, 1200)
    assert shot.render(png(), SCREEN, 160, None)[1] == (160, 320)


def test_takes_jpeg_and_png_and_other_modes():
    frame = png(fmt="JPEG")
    assert shot.render(frame, SCREEN, 300, None)[1] == (300, 600)
    assert shot.render(png(mode="RGBA", color=(1, 2, 3, 255)), SCREEN, 300, None)[1] == (300, 600)


def test_quality_is_75():
    assert shot.JPEG_QUALITY == 75 and (shot.MIN_WIDTH, shot.MAX_WIDTH, shot.DEFAULT_WIDTH) == (160, 1320, 600)


def test_a_box_is_drawn_at_its_rect():
    jpeg, *_ = shot.render(png(), SCREEN, 400, [("e1", 100, 200, 100, 50)])
    img = decode(jpeg)
    assert red(img.getpixel((101, 225)))      # the left edge: x 100..102
    assert red(img.getpixel((198, 225)))      # the right edge: x 197..199
    assert grey(img.getpixel((150, 225)))     # inside stays untouched
    assert grey(img.getpixel((50, 225)))      # outside too
    assert red(img.getpixel((102, 192)))      # the ref's chip sits just above the box


def test_boxes_scale_with_the_width():
    jpeg, *_ = shot.render(png(), SCREEN, 200, [("e1", 100, 200, 100, 50)])
    img = decode(jpeg)
    assert red(img.getpixel((51, 112))) and grey(img.getpixel((75, 112))) and grey(img.getpixel((25, 112)))


def test_no_boxes_means_no_red():
    img = decode(shot.render(png(), SCREEN, 400, [])[0])
    assert all(grey(img.getpixel(p)) for p in [(101, 225), (150, 225), (102, 192)])


def test_a_chip_at_the_top_edge_goes_inside_the_box():
    img = decode(shot.render(png(), SCREEN, 400, [("e9", 10, 0, 100, 40)])[0])
    assert red(img.getpixel((11, 20)))        # the left edge, no crash for the chip that has no room above


def test_a_box_partly_off_screen_is_clipped():
    jpeg, size, _ = shot.render(png(), SCREEN, 400, [("e2", 350, 780, 100, 60)])
    assert size == (400, 800) and red(decode(jpeg).getpixel((351, 790)))


def test_a_big_jpeg_still_comes_out_at_the_width_and_the_flag_is_false():
    jpeg, size, skipped = shot.render(png(size=(1600, 3200), fmt="JPEG"), SCREEN, 300, [("e1", 100, 200, 100, 50)])
    assert size == (300, 600) and decode(jpeg).size == (300, 600) and skipped is False


def test_boxes_are_skipped_when_the_orientation_does_not_match():
    jpeg, size, skipped = shot.render(png(size=(1600, 800)), SCREEN, 400, [("e1", 100, 200, 100, 50)])
    assert skipped is True and size == (400, 200)
    img = decode(jpeg)
    assert all(grey(img.getpixel(p)) for p in [(101, 100), (50, 100), (150, 100)])
    assert shot.render(png(size=(1600, 800)), SCREEN, 400, None)[2] is False  # nothing to skip
