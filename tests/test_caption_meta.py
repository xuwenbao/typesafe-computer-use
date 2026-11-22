"""The meta-device captioner must caption the same pixels the same way as the eager load.

Skipped unless JEV_CAPTION_CHECK=1: it loads Florence-2 twice, which needs the pinned weights,
PyTorch, and about a gigabyte of memory. Run it with the Windows venv that the picture-in-picture
session uses.
"""

import os

import pytest

from typesafe_computer_use.parse import caption as caption_mod

pytestmark = pytest.mark.skipif(
    os.environ.get("JEV_CAPTION_CHECK") != "1", reason="set JEV_CAPTION_CHECK=1 to load Florence-2 twice"
)
# Kept before the suite's guard replaces it. This test puts the real loader back for itself only.
_real_load = caption_mod.load_model


def test_the_meta_device_load_captions_like_the_eager_load(monkeypatch):
    monkeypatch.setattr(caption_mod, "load_model", _real_load)
    import torch
    from PIL import Image, ImageDraw
    from safetensors.torch import load_file
    from transformers import AutoProcessor, Florence2Config, Florence2ForConditionalGeneration

    from typesafe_computer_use.parse import weights
    from typesafe_computer_use.parse.runtime import torch_device

    pages = [Image.new("RGB", (64, 64), color) for color in ("white", "black", "red", "blue")]
    ruled = Image.new("RGB", (64, 64), "white")
    for y in range(4, 64, 8):
        ImageDraw.Draw(ruled).line((0, y, 63, y), fill="black")
    blob = Image.new("RGB", (64, 64), "white")
    ImageDraw.Draw(blob).ellipse((8, 8, 56, 56), fill="black")
    pages += [ruled, blob]

    device = torch_device(allow_mps=True)
    dtype = torch.float16 if device == "cuda" else torch.float32
    florence = weights.folder(weights.FLORENCE)
    checkpoint = load_file(str(weights.path(weights.CAPTIONER, weights.CAPTIONER.files[0])))
    trained = checkpoint["language_model.model.shared.weight"].shape[0]
    config = Florence2Config.from_pretrained(florence)
    config.tie_word_embeddings = config.text_config.tie_word_embeddings = False
    eager = Florence2ForConditionalGeneration(config)
    eager.load_state_dict(caption_mod.native_state(checkpoint, config.text_config.vocab_size), strict=True)
    processor = AutoProcessor.from_pretrained(florence)
    processor.num_image_tokens = caption_mod.IMAGE_TOKENS
    padding = list(range(trained, config.text_config.vocab_size))
    eager = eager.to(device, dtype).eval()

    def read(model):
        inputs = processor(images=pages, text=[caption_mod.PROMPT] * len(pages), return_tensors="pt", do_resize=False)
        inputs = {k: v.to(device, dtype) if v.is_floating_point() else v.to(device) for k, v in inputs.items()}
        with torch.inference_mode():
            ids = model.generate(
                **inputs,
                max_new_tokens=caption_mod.MAX_NEW_TOKENS,
                num_beams=1,
                do_sample=False,
                suppress_tokens=padding,
            )
        return [text.strip() for text in processor.batch_decode(ids, skip_special_tokens=True)]

    assert read(caption_mod.load_model()[0]) == read(eager)
