#!/usr/bin/env python3
"""
Lumix RAW / V-Log still to BT.2100 HLG converter.

Inputs:
- Panasonic RW2 RAW files
- V-Log / V-Gamut rendered JPEG, PNG, TIFF

Outputs:
- TIFF: 16-bit HLG / BT.2020 HDR master
- JXL:  16-bit HLG / BT.2020 JPEG XL
- HEIC: 10-bit HLG / BT.2020 HEIC

External tools:
- TIFF: none
- JXL:  cjxl (optional; install libjxl tools)
- HEIC: heif-enc (optional; install libheif)

Examples:
    python LUMIX2HLG.py P1000000.RW2
    python LUMIX2HLG.py P1000000.RW2 --format jxl
    python LUMIX2HLG.py P1000000.jpg --format heic
    python LUMIX2HLG.py *.RW2 --format jxl --output-dir HLG_exports
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


SUPPORTED_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".tif",
    ".tiff",
    ".rw2",
}

OUTPUT_EXTENSIONS = {
    "tiff": ".tif",
    "jxl": ".jxl",
    "heic": ".heic",
}

# Panasonic V-Gamut linear RGB -> CIE 1931 XYZ, D65.
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
    """Apply a 3x3 colour transform to an H x W x 3 RGB image."""
    return np.einsum("ij,...j->...i", matrix, image)


def vlog_to_linear(vlog: np.ndarray) -> np.ndarray:
    """Decode normalized Panasonic V-Log RGB values into scene-linear light."""
    vlog = np.asarray(vlog, dtype=np.float64)

    return np.where(
        vlog < 0.181,
        (vlog - 0.125) / 5.6,
        np.power(10.0, (vlog - 0.598206) / 0.241514) - 0.00873,
    )


def linear_to_hlg(linear: np.ndarray) -> np.ndarray:
    """Encode scene-linear RGB with the BT.2100 HLG OETF."""
    linear = np.maximum(linear, 0.0)

    a = 0.17883277
    b = 0.28466892
    c = 0.55991073

    return np.where(
        linear <= 1.0 / 12.0,
        np.sqrt(3.0 * linear),
        a * np.log(12.0 * linear - b) + c,
    )


def load_vlog_image(path: Path) -> np.ndarray:
    """
    Load a V-Log/V-Gamut rendered JPEG, PNG, or TIFF as normalized RGB.
    """
    suffix = path.suffix.lower()

    if suffix in {".tif", ".tiff"}:
        image = tifffile.imread(path)

        if image.ndim != 3 or image.shape[-1] < 3:
            raise ValueError("Input TIFF must be an RGB image.")

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


def load_rw2_as_linear_xyz(path: Path) -> np.ndarray:
    """
    Demosaic RW2 using LibRaw/rawpy and output scene-linear CIE XYZ.

    This is intentionally separate from V-Log: sensor RAW does not contain
    V-Log-encoded RGB values.
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
    """Apply exposure and optional highlight compression in linear light."""
    linear_bt2020 = linear_bt2020 * (2.0 ** exposure_stops)
    linear_bt2020 = np.maximum(linear_bt2020, 0.0)

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
    """Apply creative adjustments after HLG transfer encoding."""
    hlg_bt2020 = np.clip(hlg_bt2020, 0.0, 1.0)

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
    """V-Log/V-Gamut RGB -> BT.2100 HLG / BT.2020 RGB."""
    linear_vgamut = vlog_to_linear(vlog_vgamut)
    linear_xyz = apply_matrix(V_GAMUT_TO_XYZ, linear_vgamut)
    linear_bt2020 = apply_matrix(XYZ_TO_BT2020, linear_xyz)

    linear_bt2020 = apply_scene_adjustments(
        linear_bt2020,
        exposure_stops,
        highlight_rolloff,
    )

    return apply_display_adjustments(
        linear_to_hlg(linear_bt2020),
        contrast,
        saturation,
    )


def convert_rw2_to_hlg(
    linear_xyz: np.ndarray,
    exposure_stops: float,
    contrast: float,
    saturation: float,
    highlight_rolloff: float,
) -> np.ndarray:
    """Linear XYZ from RW2 -> BT.2100 HLG / BT.2020 RGB."""
    linear_bt2020 = apply_matrix(XYZ_TO_BT2020, linear_xyz)

    linear_bt2020 = apply_scene_adjustments(
        linear_bt2020,
        exposure_stops,
        highlight_rolloff,
    )

    return apply_display_adjustments(
        linear_to_hlg(linear_bt2020),
        contrast,
        saturation,
    )


def as_u16(hlg_bt2020: np.ndarray) -> np.ndarray:
    """Convert normalized HLG RGB to a 16-bit RGB array."""
    return np.round(
        np.clip(hlg_bt2020, 0.0, 1.0) * 65535.0
    ).astype(np.uint16)


def write_tiff(output_path: Path, hlg_bt2020: np.ndarray) -> None:
    """Write a 16-bit BT.2020 / HLG TIFF master."""
    tifffile.imwrite(
        output_path,
        as_u16(hlg_bt2020),
        photometric="rgb",
        metadata={
            "Description": (
                "BT.2020 primaries; BT.2100 HLG transfer; "
                "16-bit RGB HDR master"
            )
        },
    )


def write_jxl(
    output_path: Path,
    hlg_bt2020: np.ndarray,
    quality: int,
    effort: int,
) -> None:
    """
    Write a 16-bit BT.2020 / HLG JPEG XL.

    cjxl receives a temporary 16-bit PPM file. `color_space` explicitly marks
    the input as D65 / Rec.2020 / relative HLG.
    """
    if shutil.which("cjxl") is None:
        raise RuntimeError(
            "cjxl was not found.\n"
            "Install JPEG XL tools, then ensure cjxl is on PATH."
        )

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_dir = Path(temp_dir)
        ppm_path = temp_dir / "hlg_rec2020.ppm"

        image = as_u16(hlg_bt2020)
        height, width, _ = image.shape

        # Binary PPM supports 16-bit big-endian RGB pixels.
        with open(ppm_path, "wb") as ppm:
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
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(
                "JPEG XL encoding failed:\n"
                f"{detail or 'No diagnostic returned by cjxl.'}"
            )


def write_heic(
    output_path: Path,
    hlg_bt2020: np.ndarray,
    quality: int,
) -> None:
    """
    Write a 10-bit BT.2020 / HLG HEIC using libheif's heif-enc utility.
    """
    if shutil.which("heif-enc") is None:
        raise RuntimeError(
            "heif-enc was not found.\n"
            "Install libheif with an HEVC encoder, then add it to PATH."
        )

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_dir = Path(temp_dir)
        tiff_path = temp_dir / "hlg_rec2020.tif"

        tifffile.imwrite(
            tiff_path,
            as_u16(hlg_bt2020),
            photometric="rgb",
        )

        command = [
            "heif-enc",
            str(tiff_path),
            "-o",
            str(output_path),

            # CICP / NCLX:
            # 9  = BT.2020 primaries
            # 18 = HLG (ARIB STD-B67)
            # 9  = BT.2020 non-constant-luminance matrix
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
            detail = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(
                "HEIC encoding failed:\n"
                f"{detail or 'No diagnostic returned by heif-enc.'}"
            )


def collect_input_files(items: list[str]) -> list[Path]:
    """Accept files and top-level folders, remove duplicate paths."""
    candidates: list[Path] = []

    for item in items:
        path = Path(item).expanduser()

        if path.is_file():
            candidates.append(path)
        elif path.is_dir():
            candidates.extend(
                file
                for file in sorted(path.iterdir())
                if file.is_file()
                and file.suffix.lower() in SUPPORTED_EXTENSIONS
            )
        else:
            print(f"Warning: not found, skipped: {path}", file=sys.stderr)

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
    output_dir: Path,
    output_format: str,
) -> Path:
    """Build the output filename without modifying input files."""
    extension = OUTPUT_EXTENSIONS[output_format]
    return output_dir / f"{input_path.stem}_BT2100_HLG{extension}"


def convert_one(input_path: Path, args: argparse.Namespace) -> Path:
    """Load, convert, and encode one image."""
    suffix = input_path.suffix.lower()

    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported input type: {suffix}")

    print(f"Processing: {input_path.name}")

    if suffix == ".rw2":
        linear_xyz = load_rw2_as_linear_xyz(input_path)

        hlg_bt2020 = convert_rw2_to_hlg(
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

    output_path = make_output_path(
        input_path,
        args.output_dir,
        args.format,
    )

    if output_path.exists() and not args.overwrite:
        raise FileExistsError(
            f"Output already exists: {output_path}\n"
            "Use --overwrite to replace it."
        )

    if args.format == "tiff":
        write_tiff(output_path, hlg_bt2020)
    elif args.format == "jxl":
        write_jxl(
            output_path,
            hlg_bt2020,
            args.quality,
            args.effort,
        )
    else:
        write_heic(
            output_path,
            hlg_bt2020,
            args.quality,
        )

    return output_path


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Convert Lumix RW2 RAW or V-Log/V-Gamut stills to "
            "BT.2100 HLG / BT.2020 TIFF, JPEG XL, or HEIC."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "inputs",
        nargs="+",
        help="One or more source files and/or folders.",
    )

    parser.add_argument(
        "--format",
        choices=("tiff", "jxl", "heic"),
        default="tiff",
        help="Output format.",
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("HLG_exports"),
        help="Directory for converted files.",
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
        "--quality",
        type=int,
        default=92,
        help="HEIC/JXL quality from 1 to 100; ignored for TIFF.",
    )

    parser.add_argument(
        "--effort",
        type=int,
        default=7,
        help="JPEG XL encoder effort from 1 to 10; ignored for TIFF/HEIC.",
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace output files with matching names.",
    )

    args = parser.parse_args()

    args.output_dir = args.output_dir.expanduser()
    args.exposure = max(-5.0, min(5.0, args.exposure))
    args.contrast = max(0.5, min(1.5, args.contrast))
    args.saturation = max(0.0, min(2.0, args.saturation))
    args.rolloff = max(0.0, min(1.0, args.rolloff))
    args.quality = max(1, min(100, args.quality))
    args.effort = max(1, min(10, args.effort))

    return args


def main() -> int:
    args = parse_arguments()
    input_files = collect_input_files(args.inputs)

    if not input_files:
        print(
            "No supported source files found. Supported types: "
            + ", ".join(sorted(SUPPORTED_EXTENSIONS)),
            file=sys.stderr,
        )
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)

    successful = 0
    failed: list[tuple[str, str]] = []

    for input_path in input_files:
        try:
            output = convert_one(input_path, args)
            print(f"  Created: {output}")
            successful += 1
        except Exception as error:
            failed.append((input_path.name, str(error)))
            print(
                f"  Failed: {input_path.name}\n"
                f"  {error}",
                file=sys.stderr,
            )

    print(f"\nFinished: {successful}/{len(input_files)} converted.")

    if failed:
        print("\nFailures:", file=sys.stderr)
        for filename, message in failed:
            print(f"- {filename}: {message}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
