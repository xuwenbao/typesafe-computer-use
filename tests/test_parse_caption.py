import pytest
from PIL import Image, ImageDraw

from typesafe_computer_use.parse.caption import CROP_PX, IMAGE_TOKENS, CaptionCache, caption, crop, key, native_name, native_state


def toolbar():
    """Three icons on a white bar: a red square, a blue square, and another red square."""
    image = Image.new("RGB", (300, 60), "white")
    draw = ImageDraw.Draw(image)
    for x, color in ((10, "red"), (110, "blue"), (210, "red")):
        draw.rectangle((x, 10, x + 39, 49), fill=color)
    return image


RED, BLUE, RED_AGAIN = (10.0, 10.0, 50.0, 50.0), (110.0, 10.0, 150.0, 50.0), (210.0, 10.0, 250.0, 50.0)


class Model:
    """A stand-in for Florence-2 that names each crop by its center pixel and counts what it read."""

    def __init__(self):
        self.calls: list[int] = []

    def __call__(self, icons):
        self.calls.append(len(icons))
        names = {(255, 0, 0): "red square", (0, 0, 255): "blue square"}
        return [names.get(icon.getpixel((CROP_PX // 2, CROP_PX // 2)), "?") for icon in icons]


def test_each_icon_is_cropped_and_stretched_to_64_pixels():
    icon = crop(toolbar(), (10.0, 10.0, 50.0, 30.0))
    assert icon.size == (CROP_PX, CROP_PX)


def test_a_box_with_no_pixels_has_no_crop():
    assert crop(toolbar(), (100.0, 10.0, 100.0, 50.0)) is None
    assert crop(toolbar(), (400.0, 10.0, 450.0, 50.0)) is None


def test_captions_come_back_one_per_box_in_order():
    model = Model()
    assert caption(toolbar(), [BLUE, RED], generate=model) == ["blue square", "red square"]


def test_a_failed_crop_gets_an_empty_caption_and_the_rest_stay_aligned():
    model = Model()
    captions = caption(toolbar(), [RED, (100.0, 10.0, 100.0, 50.0), BLUE], generate=model)
    assert captions == ["red square", "", "blue square"]


def test_with_no_icon_the_model_is_not_called():
    model = Model()
    assert caption(toolbar(), [], generate=model) == []
    assert model.calls == []


def test_identical_icons_in_one_capture_are_read_once():
    model = Model()
    assert caption(toolbar(), [RED, BLUE, RED_AGAIN], generate=model) == ["red square", "blue square", "red square"]
    assert model.calls == [2]


def test_a_cached_icon_is_not_read_again():
    model, cache = Model(), CaptionCache()
    caption(toolbar(), [RED, BLUE], cache, model)
    assert caption(toolbar(), [BLUE, RED], cache, model) == ["blue square", "red square"]
    assert model.calls == [2]


def test_the_cache_drops_the_least_recently_used_past_its_size():
    cache = CaptionCache(size=2)
    cache.put("a", "A")
    cache.put("b", "B")
    assert cache.get("a") == "A"  # now b is the oldest
    cache.put("c", "C")
    assert (cache.get("a"), cache.get("b"), cache.get("c")) == ("A", None, "C")
    assert len(cache) == 2


def test_the_key_is_the_crops_pixels():
    a, b = crop(toolbar(), RED), crop(toolbar(), RED_AGAIN)
    assert key(a) == key(b) != key(crop(toolbar(), BLUE))


@pytest.mark.parametrize(
    "checkpoint, native",
    [
        ("language_model.model.shared.weight", "model.language_model.shared.weight"),
        ("language_model.lm_head.weight", "lm_head.weight"),
        ("language_model.model.decoder.layers.0.fc2.bias", "model.language_model.decoder.layers.0.fc2.bias"),
        ("image_projection", "model.multi_modal_projector.image_projection.weight"),
        ("image_pos_embed.row_embeddings.weight", "model.multi_modal_projector.image_position_embed.row_embeddings.weight"),
        ("visual_temporal_embed.pos_idx_to_embed", "model.multi_modal_projector.visual_temporal_embed.pos_idx_to_embed"),
        ("vision_tower.blocks.2.3.spatial_block.conv1.fn.dw.weight", "model.vision_tower.blocks.2.3.spatial_block.conv1.weight"),
        ("vision_tower.blocks.0.0.channel_block.ffn.fn.net.fc1.bias", "model.vision_tower.blocks.0.0.channel_block.ffn.fc1.bias"),
        (
            "vision_tower.blocks.1.0.channel_block.channel_attn.norm.weight",
            "model.vision_tower.blocks.1.0.channel_block.norm1.weight",
        ),
        ("vision_tower.blocks.1.0.channel_block.ffn.norm.bias", "model.vision_tower.blocks.1.0.channel_block.norm2.bias"),
        (
            "vision_tower.blocks.3.0.spatial_block.window_attn.norm.weight",
            "model.vision_tower.blocks.3.0.spatial_block.norm1.weight",
        ),
        (
            "vision_tower.blocks.3.0.spatial_block.window_attn.fn.qkv.weight",
            "model.vision_tower.blocks.3.0.spatial_block.window_attn.qkv.weight",
        ),
        ("vision_tower.blocks.3.0.spatial_block.ffn.norm.weight", "model.vision_tower.blocks.3.0.spatial_block.norm2.weight"),
        ("vision_tower.convs.2.proj.bias", "model.vision_tower.convs.2.conv.bias"),
        ("vision_tower.convs.2.norm.bias", "model.vision_tower.convs.2.norm.bias"),
    ],
)
def test_checkpoint_names_map_to_transformers_florence2(checkpoint, native):
    assert native_name(checkpoint) == native


def test_an_unresized_crop_is_five_image_tokens():
    assert IMAGE_TOKENS == 5


def test_native_state_pads_the_vocabulary_unties_the_output_and_transposes_the_projection():
    torch = pytest.importorskip("torch")
    shared, head = torch.ones(3, 2), torch.full((3, 2), 2.0)
    projection = torch.arange(6.0).reshape(2, 3)
    state = native_state(
        {
            "language_model.model.shared.weight": shared,
            "language_model.lm_head.weight": head,
            "language_model.final_logits_bias": torch.zeros(1, 3),
            "image_projection": projection,
        },
        vocab=5,
    )
    assert "language_model.final_logits_bias" not in state and not any("final_logits_bias" in k for k in state)
    assert state["model.language_model.shared.weight"].shape == (5, 2)
    assert torch.equal(state["model.language_model.shared.weight"][3:], torch.zeros(2, 2))
    assert torch.equal(state["lm_head.weight"][:3], head)
    for side in ("encoder", "decoder"):
        assert state[f"model.language_model.{side}.embed_tokens.weight"] is state["model.language_model.shared.weight"]
    assert torch.equal(state["model.multi_modal_projector.image_projection.weight"], projection.T)
