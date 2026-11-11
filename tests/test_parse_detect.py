import numpy as np
from PIL import Image

from typesafe_computer_use.parse import detect


def test_the_side_rounds_up_to_a_multiple_of_32():
    assert detect.target_size(640) == 640
    assert detect.target_size(1000) == 1024


def test_a_wide_capture_is_scaled_to_fit_and_centered_vertically():
    image = Image.new("RGB", (1280, 640), "white")
    array, scale, left, top = detect.letterbox(image, 640)
    assert array.shape == (3, 640, 640) and array.dtype == np.float32
    assert (scale, left, top) == (0.5, 0, 160)
    assert np.allclose(array[:, 0, 0], 114 / 255)  # the padding above
    assert np.allclose(array[:, 320, 320], 1.0)  # the image


def test_boxes_map_back_onto_the_capture_and_are_clamped():
    boxes = np.array([[10.0, 170.0, 30.0, 190.0], [600.0, 150.0, 700.0, 500.0]])
    placed = detect.unletterbox(boxes, 0.5, 0, 160, 1280, 640)
    assert placed[0].tolist() == [20.0, 20.0, 60.0, 60.0]
    assert placed[1].tolist() == [1200.0, 0.0, 1280.0, 640.0]
