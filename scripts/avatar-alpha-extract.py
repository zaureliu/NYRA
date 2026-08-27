"""Extract a baked light checkerboard into real alpha for approved NYRA art.

This is intentionally narrow: it only accepts a light, near-neutral background
connected to the canvas border. Interior highlights are never selected merely
because they are bright.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage


def extract_alpha(source: Path, destination: Path, crop: tuple[int, int, int, int] | None = None) -> None:
    image = Image.open(source).convert("RGB")
    rgb = np.asarray(image, dtype=np.uint8)
    low = rgb.min(axis=2)
    high = rgb.max(axis=2)
    chroma = high - low

    possible_background = (low >= 224) & (chroma <= 14)
    labels, _ = ndimage.label(possible_background)
    component_sizes = np.bincount(labels.ravel())
    # Enclosed gaps between long strands are background. Small bright components
    # in eyes, jewelry and skin are retained as foreground.
    background = possible_background & (component_sizes[labels] >= 180)

    alpha = np.full(possible_background.shape, 255, dtype=np.uint8)
    alpha[background] = 0

    # Recover antialiasing that was composited against the generated checker.
    # Distance from a neutral matte estimates coverage; RGB decontamination
    # prevents a white fringe over dark desktops.
    edge = ndimage.binary_dilation(background, iterations=3) & ~background
    matte = np.full(rgb.shape, 248.0, dtype=np.float32)
    distance = np.linalg.norm(rgb.astype(np.float32) - matte, axis=2)
    coverage = np.clip((distance - 7.0) / 92.0, 0.0, 1.0)
    alpha[edge] = np.minimum(alpha[edge], np.round(coverage[edge] * 255).astype(np.uint8))
    # Remove the final one-pixel light matte while keeping the dark/color core
    # of fine strands. At 1024 px this is visually sub-pixel at overlay scale.
    alpha = ndimage.grey_erosion(alpha, size=(3, 3)).astype(np.uint8)
    alpha_float = alpha.astype(np.float32) / 255.0
    decontaminated = rgb.astype(np.float32)
    partial = edge & (alpha > 8) & (alpha < 250)
    for channel in range(3):
        decontaminated[..., channel][partial] = np.clip(
            (decontaminated[..., channel][partial] - (1.0 - alpha_float[partial]) * 248.0)
            / alpha_float[partial],
            0,
            255,
        )

    rgba = np.dstack((decontaminated.astype(np.uint8), alpha))
    output = Image.fromarray(rgba, "RGBA")
    if crop:
        output = output.crop(crop)
    destination.parent.mkdir(parents=True, exist_ok=True)
    output.save(destination, optimize=True)


def parse_crop(value: str | None) -> tuple[int, int, int, int] | None:
    if not value:
        return None
    parts = tuple(int(part) for part in value.split(","))
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("crop must be left,top,right,bottom")
    return parts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--crop", type=parse_crop)
    args = parser.parse_args()
    extract_alpha(args.source, args.destination, args.crop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
