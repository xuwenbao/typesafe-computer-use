"""Icon boxes on a capture: OmniParser's YOLOv9-E detector, as TorchScript, without Ultralytics.

A port of OmniParser's `util/yolov9.py`: letterbox the image to a square multiple of 32, run the
model, decode its three feature maps (strides 8, 16, 32) into boxes, keep the scores above `CONF`,
and run class-aware NMS at `NMS_IOU`. The thresholds are OmniParser's own: it detects at 0.05 and
suppresses at 0.1, and leaves nested boxes to the fusion step.

PyTorch is imported inside the functions that need it, so this module imports on an Intel Mac.
"""

from __future__ import annotations

import functools

import numpy as np
from PIL import Image

from . import weights
from .runtime import INFERENCE

Box = tuple[float, float, float, float]

CONF = 0.05
NMS_IOU = 0.1
IMAGE_SIZE = 640
MAX_DETECTIONS = 300
STRIDES = (8, 16, 32)
PAD_GRAY = (114, 114, 114)


def target_size(size: int) -> int:
    """The square side the model reads: `size` rounded up to a multiple of 32."""
    return (int(size) + 31) // 32 * 32


def letterbox(image: Image.Image, size: int) -> tuple[np.ndarray, float, int, int]:
    """The image scaled to fit a `size` square, centered on gray, as a CHW float array in 0..1; the
    scale, and the left and top padding, to map boxes back onto the image."""
    side = target_size(size)
    scale = min(side / image.width, side / image.height)
    width, height = int(image.width * scale), int(image.height * scale)
    left, top = (side - width) // 2, (side - height) // 2
    padded = Image.new("RGB", (side, side), PAD_GRAY)
    padded.paste(image.convert("RGB").resize((width, height), Image.Resampling.LANCZOS), (left, top))
    return np.asarray(padded, dtype=np.float32).transpose(2, 0, 1) / 255.0, scale, left, top


def unletterbox(boxes: np.ndarray, scale: float, left: int, top: int, width: int, height: int) -> np.ndarray:
    """Boxes from the letterboxed square back onto the original image, clamped to it."""
    out = boxes.copy()
    out[:, [0, 2]] = ((out[:, [0, 2]] - left) / scale).clip(0, width)
    out[:, [1, 3]] = ((out[:, [1, 3]] - top) / scale).clip(0, height)
    return out


@functools.cache
def load_model():
    """The TorchScript detector, on the device it runs on. Loaded once, on the first detection."""
    import torch

    from .runtime import torch_device

    device = torch_device(allow_mps=False)  # TorchScript's YOLOv9 graph is only proven on CUDA and the CPU
    # The profiling executor re-optimizes the graph over its first calls: 24 s for the first
    # detection on a 6-core CPU against 14 s without it, and slower and erratic for a few after.
    torch._C._jit_set_profiling_executor(False)
    return torch.jit.load(str(weights.path(weights.DETECTOR, weights.DETECTOR.files[0])), map_location=device).eval(), device


def _decode(outputs, device):
    """The class scores and xyxy boxes of every anchor, from the three (logits, distances) pairs."""
    import torch

    scores, boxes = [], []
    for k, stride in zip(range(0, len(outputs), 2), STRIDES, strict=False):
        logits, distances = outputs[k : k + 2]
        batch, _, height, width = logits.shape
        logits = logits.permute(0, 2, 3, 1).reshape(batch, -1, logits.shape[1])
        distances = distances.permute(0, 2, 3, 1).reshape(batch, -1, 4) * stride
        grid_y, grid_x = torch.meshgrid(torch.arange(height, device=device), torch.arange(width, device=device), indexing="ij")
        anchors = (torch.stack((grid_x, grid_y), dim=-1).reshape(-1, 2) + 0.5) * stride
        left_top, right_bottom = distances.chunk(2, dim=-1)
        boxes.append(torch.cat((anchors - left_top, anchors + right_bottom), dim=-1))
        scores.append(logits)
    return torch.cat(scores, dim=1).sigmoid(), torch.cat(boxes, dim=1)


def detect(image: Image.Image, size: int = IMAGE_SIZE, conf: float = CONF, iou: float = NMS_IOU) -> list[tuple[Box, float]]:
    """Icon boxes on `image`, in its own pixels, each with its score, most confident first."""
    import torch
    from torchvision.ops import batched_nms

    model, device = load_model()
    array, scale, left, top = letterbox(image, size)
    with INFERENCE, torch.inference_mode():
        class_scores, boxes = _decode(model(torch.from_numpy(array).unsqueeze(0).to(device)), device)
        scores, classes = class_scores[0].max(dim=-1)
        keep = scores > conf
        scores, classes, boxes = scores[keep], classes[keep], boxes[0][keep]
        order = batched_nms(boxes, scores, classes, iou)[:MAX_DETECTIONS]
        boxes, scores = boxes[order].float().cpu().numpy(), scores[order].float().cpu().numpy()
    placed = unletterbox(boxes, scale, left, top, image.width, image.height)
    return [(tuple(float(v) for v in box), float(score)) for box, score in zip(placed, scores, strict=True)]
