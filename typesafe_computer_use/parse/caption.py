"""A short description of each icon that has no text: OmniParser's fine-tune of Florence-2.

Each icon is cropped from the capture, resized to 64x64 (stretched, as OmniParser does), and read
with Florence-2's `<CAPTION>` task: greedy decoding, at most 20 new tokens. Greedy decoding gives
the same caption for the same pixels every time, so captions are cached by the crop's SHA-256 for
the length of a run, and a toolbar that stays on screen is read once.

Two edge cases of OmniParser's own loop are fixed here. A crop that fails is skipped silently
there, which shifts every later caption onto the wrong icon; here it gets an empty caption in its
place. And with no icon to caption, the model is never called.

The model runs in transformers' own Florence-2 class, not the remote code the checkpoint was
saved for; `native_state` renames the checkpoint into that class's layout. PyTorch and
transformers are imported inside the functions that need them.
"""

from __future__ import annotations

import functools
import hashlib
import re
from collections import OrderedDict
from collections.abc import Callable

from PIL import Image

from . import weights
from .runtime import INFERENCE

Box = tuple[float, float, float, float]
Generate = Callable[[list[Image.Image]], list[str]]

CROP_PX = 64
PROMPT = "<CAPTION>"
MAX_NEW_TOKENS = 20
BATCH = 32
CACHE_SIZE = 1024
# OmniParser hands its 64x64 crops to the processor unresized on CUDA, and lets the processor scale
# them up to 768x768 on the CPU. Unresized is its own GPU path and about fifty times cheaper on a
# CPU. The vision tower reduces each side 32-fold, and adds one pooled token to the 2x2 grid.
IMAGE_TOKENS = (CROP_PX // 32) ** 2 + 1

# OmniParser's checkpoint names, laid out for Microsoft's remote code, to transformers' own
# Florence-2 class. Applied in order to each name.
RENAMES = (
    (r"^language_model\.model\.", "model.language_model."),
    (r"^language_model\.lm_head\.", "lm_head."),
    (r"^vision_tower\.", "model.vision_tower."),
    (r"^image_pos_embed\.", "model.multi_modal_projector.image_position_embed."),
    (r"^image_proj_norm\.", "model.multi_modal_projector.image_proj_norm."),
    (r"^image_projection$", "model.multi_modal_projector.image_projection.weight"),
    (r"^visual_temporal_embed\.", "model.multi_modal_projector.visual_temporal_embed."),
    (r"\.(?:fn|dw|net)(?=\.)", ""),
    (r"channel_block\.channel_attn\.norm\.", "channel_block.norm1."),
    (r"channel_block\.ffn\.norm\.", "channel_block.norm2."),
    (r"spatial_block\.window_attn\.norm\.", "spatial_block.norm1."),
    (r"spatial_block\.ffn\.norm\.", "spatial_block.norm2."),
    (r"(convs\.\d+)\.proj\.", r"\1.conv."),
)
SHARED = "model.language_model.shared.weight"
# All zero in the checkpoint, and transformers' class has no such bias.
DROPPED = ("language_model.final_logits_bias",)


def crop(image: Image.Image, box: Box) -> Image.Image | None:
    """The icon at `box`, stretched to 64x64, or None when the box holds no pixel of the image."""
    left, top = max(0, int(box[0])), max(0, int(box[1]))
    right, bottom = min(image.width, int(box[2])), min(image.height, int(box[3]))
    if right <= left or bottom <= top:
        return None
    return image.convert("RGB").crop((left, top, right, bottom)).resize((CROP_PX, CROP_PX), Image.Resampling.BILINEAR)


def key(icon: Image.Image) -> str:
    return hashlib.sha256(icon.tobytes()).hexdigest()


class CaptionCache:
    """Captions by crop digest, the least recently used dropped past `size`. One belongs to one run."""

    def __init__(self, size: int = CACHE_SIZE) -> None:
        self.size = size
        self._entries: OrderedDict[str, str] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, digest: str) -> str | None:
        caption = self._entries.get(digest)
        if caption is not None:
            self._entries.move_to_end(digest)
        return caption

    def put(self, digest: str, caption: str) -> None:
        self._entries[digest] = caption
        self._entries.move_to_end(digest)
        while len(self._entries) > self.size:
            self._entries.popitem(last=False)


def caption(
    image: Image.Image, boxes: list[Box], cache: CaptionCache | None = None, generate: Generate | None = None
) -> list[str]:
    """One caption per box, in order: from the cache, from the model, or empty for a box with no pixels.

    Identical icons in one capture are read once. The model is called only for crops the cache does
    not hold, and not at all when there are none.
    """
    cache = cache if cache is not None else CaptionCache()
    crops = [crop(image, box) for box in boxes]
    out = [""] * len(boxes)
    pending: dict[str, list[int]] = {}
    for i, icon in enumerate(crops):
        if icon is None:
            continue
        digest = key(icon)
        cached = cache.get(digest)
        if cached is not None:
            out[i] = cached
        else:
            pending.setdefault(digest, []).append(i)
    if pending:
        digests = list(pending)
        texts = (generate or generate_captions)([crops[pending[d][0]] for d in digests])
        for digest, text in zip(digests, texts, strict=True):
            cache.put(digest, text)
            for i in pending[digest]:
                out[i] = text
    return out


def native_name(name: str) -> str:
    """The name transformers' Florence-2 class gives the checkpoint tensor `name`."""
    for pattern, repl in RENAMES:
        name = re.sub(pattern, repl, name)
    return name


def native_state(checkpoint: dict, vocab: int) -> dict:
    """OmniParser's checkpoint as a state dict for transformers' Florence-2 class with `vocab` tokens.

    The class's vocabulary is padded past the checkpoint's to a multiple of 64 and holds the image
    placeholder token, so the embedding and output rows are padded with zeros; generation suppresses
    those tokens. The output layer is trained apart from the embedding and stays untied from it. The
    encoder and decoder embeddings are the shared one, and the image projection is stored transposed.
    """
    import torch

    state = {native_name(name): tensor for name, tensor in checkpoint.items() if name not in DROPPED}
    for name in (SHARED, "lm_head.weight"):
        rows = state[name]
        state[name] = torch.cat([rows, rows.new_zeros(vocab - rows.shape[0], rows.shape[1])])
    for side in ("encoder", "decoder"):
        state[f"model.language_model.{side}.embed_tokens.weight"] = state[SHARED]
    projection = "model.multi_modal_projector.image_projection.weight"
    state[projection] = state[projection].T.contiguous()
    return state


@functools.cache
def load_model():
    """The captioner, its processor, the device, the dtype, and the token ids generation must never
    produce. Loaded once, on the first caption, from the hub cache alone.
    """
    import torch
    from safetensors.torch import load_file
    from transformers import AutoProcessor, Florence2Config, Florence2ForConditionalGeneration

    from .runtime import torch_device

    device = torch_device(allow_mps=True)
    dtype = torch.float16 if device == "cuda" else torch.float32
    florence = weights.folder(weights.FLORENCE)
    checkpoint = load_file(str(weights.path(weights.CAPTIONER, weights.CAPTIONER.files[0])))
    trained = checkpoint["language_model.model.shared.weight"].shape[0]

    config = Florence2Config.from_pretrained(florence)
    config.tie_word_embeddings = config.text_config.tie_word_embeddings = False
    model = Florence2ForConditionalGeneration(config)
    model.load_state_dict(native_state(checkpoint, config.text_config.vocab_size), strict=True)
    processor = AutoProcessor.from_pretrained(florence)
    processor.num_image_tokens = IMAGE_TOKENS
    padding = list(range(trained, config.text_config.vocab_size))
    return model.to(device, dtype).eval(), processor, device, dtype, padding


def generate_captions(icons: list[Image.Image]) -> list[str]:
    """Florence-2's caption of each 64x64 icon, in batches of `BATCH`."""
    import torch

    model, processor, device, dtype, padding = load_model()
    texts: list[str] = []
    for start in range(0, len(icons), BATCH):
        batch = icons[start : start + BATCH]
        inputs = processor(images=batch, text=[PROMPT] * len(batch), return_tensors="pt", do_resize=False)
        inputs = {k: v.to(device, dtype) if v.is_floating_point() else v.to(device) for k, v in inputs.items()}
        with INFERENCE, torch.inference_mode():
            ids = model.generate(**inputs, max_new_tokens=MAX_NEW_TOKENS, num_beams=1, do_sample=False, suppress_tokens=padding)
        texts.extend(text.strip() for text in processor.batch_decode(ids, skip_special_tokens=True))
    return texts
