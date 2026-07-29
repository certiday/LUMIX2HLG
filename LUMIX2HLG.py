#!/usr/bin/env python3
"""
Lumix / Olympus RAW and V-Log still to BT.2100 HLG converter.

Supported inputs:
  - Panasonic Lumix RW2 RAW files
  - Olympus / OM System ORF RAW files
  - Panasonic V-Log / V-Gamut rendered JPEG, PNG, TIFF

Outputs:
  - HEIC: BT.2020 + BT.2100 HLG metadata
  - JPEG XL: BT.2020 + BT.2100 HLG colour encoding
  - both: creates HEIC and JPEG XL for each input

Examples:
  python LUMIX2HLG.py DEMO.RW2 --format both
  python LUMIX2HLG.py OM1_0001.ORF --format jxl
  python LUMIX2HLG.py VLOG_0001.jpg --format heic
  python LUMIX2HLG.py *.RW2 *.ORF --format both
  python LUMIX2HLG.py "/path/to/photos" --format both
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import rawpy
import tifffile
from PIL import Image, ImageOps


# RAW formats use the rawpy/LibRaw development path.
RAW_EXTENSIONS = {
    ".rw2",
    ".orf",
}

# These inputs are assumed to be already V-Log / V-Gamut rendered images.
VLOG_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".tif",
    ".tiff",
}

SUPPORTED_INPUTS = RAW_EXTENSIONS | VLOG_EXTENSIONS

# Panasonic V-Gamut linear RGB -> CIE 1931 XYZ, D65.
# Source: Panasonic V-Log / V-Gamut Reference Manual.
V_GAMUT_TO_XYZ = np.array(
    [
        [0.679644, 0.152211, 0.118600],
        [0.260686, 0.774894, -0.035580],
        [-0.009310, -0.004612, 1.102980],
    ],
    dtype=np.float64,
)

# CIE 1931 XYZ, D65 -> BT.2020 linear RGB.
XYZ_TO_BT2020 = np.array(
    [
        [1.716651187971268, -0.355670783776392, -0.253366281373660],
        [-0.666684351832489, 1.616481236634939, 0.015768545813911],
        [0.017639857445311, -0.042770613257809, 0.942103121235474],
    ],
    dtype=np.float64,
)


def apply_matrix(matrix: np.ndarray, image: np.ndarray) -> np.ndarray:
    """Apply a 3x3 colour matrix to an H x W x 3 RGB image."""
    return np.einsum("ij,...j->...i", matrix, image)


def vlog_to_linear(vlog: np.ndarray) -> np.ndarray:
    """Decode normalized Panasonic V-Log RGB values into scene-linear RGB."""
    vlog = np.asarray(vlog, dtype=np.float64)

    return np.where(
        vlog < 0.181,
        (vlog - 0.125) / 5.6,
        np.power(10.0, (vlog - 0.598206) / 0.241514) - 0.00873,
    )


def linear_to_hlg(linear: np.ndarray) -> np.ndarray:
    """Encode scene-linear RGB using the BT.2100 HLG OETF."""
    linear = np.maximum(linear, 0.0)

    a = 0.17883277
    b = 0.28466892
    c = 0.55991073

    return np.where(
        linear <= (1.0 / 12.0),
        np.sqrt(3.0 * linear),
        a * np.log(12.0 * linear - b) + c,
    )


def load_vlog_image(path: Path) -> np.ndarray:
    """
    Load a V-Log/V-Gamut JPEG, PNG, or TIFF as normalized RGB.

    This function does not inspect or transform embedded ICC metadata. It
    assumes input RGB values are already Panasonic V-Log / V-Gamut values.
    """
    suffix = path.suffix.lower()

    if suffix in {".tif", ".tiff"}:
        image = tifffile.imread(path)

        if image.ndim != 3 or image.shape[-1] < 3:
            raise ValueError("Input TIFF must be a three-channel RGB image.")

        image = image[..., :3]

        if image.dtype == np.uint8:
            return image.astype(np.float64) / 255.0

        if image.dtype == np.uint16:
            return image.astype(np.float64) / 65535.0

        if np.issubdtype(image.dtype, np.floating):
            return image.astype(np.float64)

        raise ValueError(f"Unsupported TIFF pixel type: {image.dtype}")

    with Image.open(path) as source:
        source = ImageOps.exif_transpose(source).convert("RGB")
        image = np.asarray(source)

    return image.astype(np.float64) / 255.0


def load_raw_as_linear_xyz(path: Path) -> np.ndarray:
    """
    Demosaic RW2 or ORF sensor RAW data with rawpy/LibRaw.

    - Uses as-shot camera white balance
    - Disables automatic brightness
    - Requests linear gamma
    - Returns 16-bit CIE XYZ output normalized to 0-1

    RAW files are not V-Log; they must follow this independent RAW pipeline.
    """
    with rawpy.imread(str(path)) as raw:
        xyz_u16 = raw.postprocess(
            use_camera_wb=True,
            use_auto_wb=False,
            no_auto_bright=True,
            gamma=(1.0, 1.0),
            output_bps=16,
            output_color=rawpy.ColorSpace.XYZ,
            highlight_mode=rawpy.HighlightMode.Blend,
        )

    return xyz_u16.astype(np.float64) / 65535.0


def apply_scene_adjustments(
    linear_bt2020: np.ndarray,
    exposure_stops: float,
    highlight_rolloff: float,
) -> np.ndarray:
    """
    Apply exposure and optional highlight compression in scene-linear light.

    Rolloff is 0-1. A value of 0 disables highlight compression.
    """
    linear_bt2020 = np.maximum(
        linear_bt2020 * (2.0 ** exposure_stops),
        0.0,
    )

    if highlight_rolloff > 0.0:
        knee = 1.0 + (1.0 - highlight_rolloff) * 3.0
        linear_bt2020 = linear_bt2020 / (
            1.0 + linear_bt2020 / knee
        )

    return linear_bt2020


def apply_display_adjustments(
    hlg_bt2020: np.ndarray,
    contrast: float,
    saturation: float,
) -> np.ndarray:
    """Apply restrained creative controls after HLG encoding."""
    hlg_bt2020 = np.clip(hlg_bt2020, 0.0, 1.0)

    # ITU-R BT.2020 luma coefficients.
    luma = (
        hlg_bt2020[..., 0] * 0.2627
        + hlg_bt2020[..., 1] * 0.6780
        + hlg_bt2020[..., 2] * 0.0593
    )[..., np.newaxis]

    hlg_bt2020 = luma + (hlg_bt2020 - luma) * saturation
    hlg_bt2020 = 0.5 + (hlg_bt2020 - 0.5) * contrast

    return np.clip(hlg_bt2020, 0.0, 1.0)


def convert_vlog_to_hlg(
    vlog_vgamut: np.ndarray,
    exposure_stops: float,
    contrast: float,
    saturation: float,
    highlight_rolloff: float,
) -> np.ndarray:
    """Convert V-Log/V-Gamut image pixels into BT.2100 HLG / BT.2020."""
    linear_vgamut = vlog_to_linear(vlog_vgamut)
    linear_xyz = apply_matrix(V_GAMUT_TO_XYZ, linear_vgamut)
    linear_bt2020 = apply_matrix(XYZ_TO_BT2020, linear_xyz)

    linear_bt2020 = apply_scene_adjustments(
        linear_bt2020,
        exposure_stops,
        highlight_rolloff,
    )

    hlg_bt2020 = linear_to_hlg(linear_bt2020)

    return apply_display_adjustments(
        hlg_bt2020,
        contrast,
        saturation,
    )


def convert_raw_to_hlg(
    linear_xyz: np.ndarray,
    exposure_stops: float,
    contrast: float,
    saturation: float,
    highlight_rolloff: float,
) -> np.ndarray:
    """Convert linear CIE XYZ from RW2/ORF into BT.2100 HLG / BT.2020."""
    linear_bt2020 = apply_matrix(XYZ_TO_BT2020, linear_xyz)

    linear_bt2020 = apply_scene_adjustments(
        linear_bt2020,
        exposure_stops,
        highlight_rolloff,
    )

    hlg_bt2020 = linear_to_hlg(linear_bt2020)

    return apply_display_adjustments(
        hlg_bt2020,
        contrast,
        saturation,
    )


def hlg_to_u16(hlg_bt2020: np.ndarray) -> np.ndarray:
    """Convert normalized HLG RGB pixels to unsigned 16-bit RGB."""
    return np.round(
        np.clip(hlg_bt2020, 0.0, 1.0) * 65535.0
    ).astype(np.uint16)


def require_command(command: str, help_text: str) -> None:
    """Require an external encoder to be present on PATH."""
    if shutil.which(command) is None:
        raise RuntimeError(
            f"'{command}' was not found on PATH.\n\n{help_text}"
        )


def write_temporary_tiff(
    path: Path,
    hlg_bt2020: np.ndarray,
) -> None:
    """
    Write a temporary 16-bit TIFF used only as HEIC encoder input.

    It is created inside a temporary directory and automatically deleted.
    """
    tifffile.imwrite(
        path,
        hlg_to_u16(hlg_bt2020),
        photometric="rgb",
    )


def write_heic(
    output_path: Path,
    hlg_bt2020: np.ndarray,
    quality: int,
) -> None:
    """
    Encode 10-bit HEIC with BT.2020 / BT.2100 HLG colour metadata.

    Requires heif-enc from libheif plus an HEVC encoding plugin.
    """
    require_command(
        "heif-enc",
        "Install libheif with HEVC encoding support, then make sure "
        "'heif-enc' is on PATH.",
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        source_tiff = Path(temp_dir) / "hlg_bt2020.tif"
        write_temporary_tiff(source_tiff, hlg_bt2020)

        command = [
            "heif-enc",
            str(source_tiff),
            "-o",
            str(output_path),

            # CICP / NCLX signalling:
            # 9  = BT.2020 colour primaries
            # 18 = HLG / ARIB STD-B67 transfer characteristic
            # 9  = BT.2020 non-constant-luminance matrix coefficients
            "--colour_primaries",
            "9",
            "--transfer_characteristic",
            "18",
            "--matrix_coefficients",
            "9",

            "-p",
            "lossless=false",
            "-p",
            f"quality={quality}",
            "-p",
            "chroma=420",
        ]

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            details = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(
                "HEIC encoding failed:\n"
                f"{details or 'No diagnostic returned by heif-enc.'}"
            )


def write_jxl(
    output_path: Path,
    hlg_bt2020: np.ndarray,
    quality: int,
    effort: int,
) -> None:
    """
    Encode JPEG XL with explicit D65 / Rec.2020 / relative-HLG colour data.

    Requires cjxl from libjxl. A temporary 16-bit PPM is used because it
    preserves the 16-bit RGB samples supplied to cjxl.
    """
    require_command(
        "cjxl",
        "Install JPEG XL tools, then make sure 'cjxl' is on PATH.",
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        ppm_path = Path(temp_dir) / "hlg_bt2020.ppm"
        image = hlg_to_u16(hlg_bt2020)
        height, width, _ = image.shape

        with ppm_path.open("wb") as ppm:
            ppm.write(f"P6\n{width} {height}\n65535\n".encode("ascii"))
            ppm.write(image.astype(">u2", copy=False).tobytes())

        command = [
            "cjxl",
            str(ppm_path),
            str(output_path),
            "--quality",
            str(quality),
            "--effort",
            str(effort),
            "-x",
            "color_space=RGB_D65_202_Rel_HLG",
        ]

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            details = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(
                "JPEG XL encoding failed:\n"
                f"{details or 'No diagnostic returned by cjxl.'}"
            )


def find_input_files(items: list[str]) -> list[Path]:
    """
    Get files passed directly, or supported files in a folder's top level.

    Folder searching is deliberately non-recursive.
    """
    candidates: list[Path] = []

    for item in items:
        path = Path(item).expanduser()

        if path.is_file():
            candidates.append(path)

        elif path.is_dir():
            candidates.extend(
                child
                for child in sorted(path.iterdir())
                if child.is_file()
                and child.suffix.lower() in SUPPORTED_INPUTS
            )

        else:
            print(
                f"Warning: path not found, skipped: {path}",
                file=sys.stderr,
            )

    unique: list[Path] = []
    seen: set[Path] = set()

    for path in candidates:
        resolved = path.resolve()

        if resolved not in seen:
            unique.append(path)
            seen.add(resolved)

    return unique


def make_output_path(
    input_path: Path,
    output_directory: Path,
    output_format: str,
) -> Path:
    """Return a non-destructive output filename for HEIC or JPEG XL."""
    extension = ".heic" if output_format == "heic" else ".jxl"

    return output_directory / (
        f"{input_path.stem}_BT2100_HLG{extension}"
    )


def convert_one(
    input_path: Path,
    args: argparse.Namespace,
) -> list[Path]:
    """Convert one input photo and return its created output file paths."""
    suffix = input_path.suffix.lower()

    if suffix not in SUPPORTED_INPUTS:
        raise ValueError(f"Unsupported input type: {suffix}")

    print(f"Processing: {input_path.name}")

    if suffix in RAW_EXTENSIONS:
        linear_xyz = load_raw_as_linear_xyz(input_path)

        hlg_bt2020 = convert_raw_to_hlg(
            linear_xyz,
            args.exposure,
            args.contrast,
            args.saturation,
            args.rolloff,
        )
    else:
        vlog_vgamut = load_vlog_image(input_path)

        hlg_bt2020 = convert_vlog_to_hlg(
            vlog_vgamut,
            args.exposure,
            args.contrast,
            args.saturation,
            args.rolloff,
        )

    requested_formats = (
        ("heic", "jxl")
        if args.format == "both"
        else (args.format,)
    )

    outputs: list[Path] = []

    for output_format in requested_formats:
        output_path = make_output_path(
            input_path,
            args.output_dir,
            output_format,
        )

        if output_path.exists() and not args.overwrite:
            raise FileExistsError(
                f"Output already exists: {output_path}\n"
                "Use --overwrite to replace it."
            )

        if output_format == "heic":
            write_heic(
                output_path,
                hlg_bt2020,
                args.quality,
            )
        else:
            write_jxl(
                output_path,
                hlg_bt2020,
                args.quality,
                args.effort,
            )

        outputs.append(output_path)

    return outputs


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Convert Lumix RW2, Olympus/OM System ORF, or V-Log/V-Gamut "
            "stills to BT.2100 HLG / BT.2020 HEIC and JPEG XL."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "inputs",
        nargs="+",
        help="Source photo file(s) and/or folder(s).",
    )

    parser.add_argument(
        "--format",
        choices=("heic", "jxl", "both"),
        default="both",
        help="Output format.",
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("HLG_exports"),
        help="Folder for converted files.",
    )

    parser.add_argument(
        "--quality",
        type=int,
        default=92,
        help="HEIC/JPEG XL quality from 1 to 100.",
    )

    parser.add_argument(
        "--effort",
        type=int,
        default=7,
        help="JPEG XL effort from 1 to 10.",
    )

    parser.add_argument(
        "--exposure",
        type=float,
        default=0.0,
        help="Exposure adjustment in stops.",
    )

    parser.add_argument(
        "--contrast",
        type=float,
        default=1.0,
        help="Post-HLG contrast from 0.5 to 1.5.",
    )

    parser.add_argument(
        "--saturation",
        type=float,
        default=1.0,
        help="Post-HLG saturation from 0.0 to 2.0.",
    )

    parser.add_argument(
        "--rolloff",
        type=float,
        default=0.20,
        help="Scene-linear highlight roll-off from 0.0 to 1.0.",
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace matching output files.",
    )

    args = parser.parse_args()

    args.output_dir = args.output_dir.expanduser()
    args.quality = max(1, min(100, args.quality))
    args.effort = max(1, min(10, args.effort))
    args.exposure = max(-5.0, min(5.0, args.exposure))
    args.contrast = max(0.5, min(1.5, args.contrast))
    args.saturation = max(0.0, min(2.0, args.saturation))
    args.rolloff = max(0.0, min(1.0, args.rolloff))

    return args


def main() -> int:
    args = parse_arguments()
    input_files = find_input_files(args.inputs)

    if not input_files:
        extensions = ", ".join(sorted(SUPPORTED_INPUTS))
        print(
            f"No supported input files found. Supported types: {extensions}",
            file=sys.stderr,
        )
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)

    successes = 0
    failures: list[tuple[str, str]] = []

    for input_path in input_files:
        try:
            outputs = convert_one(input_path, args)

            for output in outputs:
                print(f"  Created: {output}")

            successes += 1

        except Exception as error:
            failures.append((input_path.name, str(error)))
            print(
                f"  Failed: {input_path.name}\n  {error}",
                file=sys.stderr,
            )

    print(f"\nFinished: {successes}/{len(input_files)} input file(s) converted.")

    if failures:
        print("\nFailures:", file=sys.stderr)

        for filename, reason in failures:
            print(f"- {filename}: {reason}", file=sys.stderr)

        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
