# Lumix RAW to HLG

Convert Panasonic Lumix RW2 RAW files, Olympus / OM System ORF RAW files, and Panasonic V-Log/V-Gamut stills into HDR **HEIC**, **JPEG XL**, or both.

This is a batch-ready, cross-platform command-line tool for creating BT.2100 HLG / BT.2020 HDR photos without Lightroom or other commercial RAW editors.

> **Status:** Experimental. RAW development uses LibRaw via rawpy, so rendering will not exactly match Panasonic or Olympus in-camera JPEGs, macOS Preview, Olympus Workspace, Lightroom, or other proprietary RAW processors.

## Features

- Converts Panasonic Lumix `.RW2` RAW files
- Converts Olympus / OM System `.ORF` RAW files
- Converts V-Log / V-Gamut rendered `.jpg`, `.jpeg`, `.png`, `.tif`, and `.tiff` images
- Creates HDR output in:
  - **HEIC** — 10-bit HLG / BT.2020 with HDR metadata
  - **JPEG XL** — 16-bit HLG / BT.2020
  - **Both** formats in one run
- Batch-processes multiple files or a folder
- Supports exposure, highlight roll-off, contrast, and saturation adjustments
- Runs on macOS, Linux, and Windows
- Uses temporary intermediate files only; no TIFF output is retained

## Requirements

- Python 3.10 or newer
- Python packages listed in `requirements.txt`
- `heif-enc` for HEIC output
- `cjxl` for JPEG XL output

## Installation

Clone the repository:

```bash
git clone https://github.com/certiday/LUMIX2HLG.git
cd LUMIX2HLG
```

### macOS

Install dependencies with Homebrew:

```bash
brew install python libheif jpeg-xl
```

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Verify the encoders:

```bash
heif-enc --help
cjxl --version
```

### Ubuntu / Debian

Install system dependencies:

```bash
sudo apt update
sudo apt install -y \
  python3 \
  python3-venv \
  python3-pip \
  libheif-examples \
  libjxl-tools
```

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Verify the encoders:

```bash
heif-enc --help
cjxl --version
```

### Windows

The recommended Windows approach is Miniforge because it installs Python and image-codec dependencies in one isolated environment.

1. Install [Miniforge](https://github.com/conda-forge/miniforge).
2. Open **Miniforge Prompt**.
3. Run:

```bat
git clone https://github.com/certiday/LUMIX2HLG.git
cd LUMIX2HLG

conda create -n lumix-hlg python=3.13 -y
conda activate lumix-hlg

conda install -c conda-forge libheif jpeg-xl -y
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Verify the commands are available:

```bat
where heif-enc
where cjxl

heif-enc --help
cjxl --version
```

If `heif-enc` is unavailable or cannot encode HEIC on your Windows installation, use JPEG XL output:

```bat
python LUMIX2HLG.py P1000000.RW2 --format jxl
```

## Python Dependencies

Create `requirements.txt` with:

```text
numpy>=1.26
Pillow>=10
rawpy>=0.25
tifffile>=2024.1
```

## Usage

### Convert One RW2 File

Create both HEIC and JPEG XL outputs:

```bash
python LUMIX2HLG.py P1000000.RW2
```

Files are written to:

```text
HLG_exports/
├── P1000000_BT2100_HLG.heic
└── P1000000_BT2100_HLG.jxl
```

### Convert One ORF File

Create both outputs from an Olympus or OM System RAW file:

```bash
python LUMIX2HLG.py OM1_0001.ORF
```

### HEIC Only

```bash
python LUMIX2HLG.py P1000000.RW2 --format heic
```

### JPEG XL Only

```bash
python LUMIX2HLG.py OM1_0001.ORF --format jxl
```

### Convert a V-Log Still

Use an image rendered with Panasonic **V-Log / V-Gamut**. Do not use a normal Rec.709 or sRGB JPEG.

```bash
python LUMIX2HLG.py P1000000_VLOG.jpg --format heic
```

### Batch Conversion

Pass several RAW files:

```bash
python LUMIX2HLG.py P1000000.RW2 OM1_0001.ORF P1000001.RW2
```

Use shell wildcards:

```bash
python LUMIX2HLG.py *.RW2 *.ORF --format both
```

Process all supported images in one folder:

```bash
python LUMIX2HLG.py "/path/to/photos" --format both
```

> Folder scans are non-recursive: files in subfolders are not included.

### Set an Output Folder

```bash
python LUMIX2HLG.py *.RW2 *.ORF \
  --format both \
  --output-dir HDR_exports
```

### Bright HDR Scenes

Reduce exposure and apply highlight roll-off for bright skies, reflections, or windows:

```bash
python LUMIX2HLG.py P1000000.RW2 \
  --format both \
  --exposure -0.7 \
  --rolloff 0.35
```

### Replace Existing Outputs

The tool does not overwrite existing files unless explicitly asked:

```bash
python LUMIX2HLG.py *.RW2 *.ORF --format both --overwrite
```

## Options

| Option | Default | Description |
|---|---:|---|
| `--format` | `both` | `heic`, `jxl`, or `both` |
| `--output-dir` | `HLG_exports` | Directory for generated files |
| `--quality` | `92` | HEIC/JXL compression quality, from 1 to 100 |
| `--effort` | `7` | JPEG XL encoder effort, from 1 to 10 |
| `--exposure` | `0.0` | Exposure adjustment in stops |
| `--contrast` | `1.0` | HLG display-referred contrast, from 0.5 to 1.5 |
| `--saturation` | `1.0` | HLG display-referred saturation, from 0.0 to 2.0 |
| `--rolloff` | `0.20` | Scene-linear highlight roll-off, from 0.0 to 1.0 |
| `--overwrite` | Off | Replace existing outputs |

View all options:

```bash
python LUMIX2HLG.py --help
```

## Input Handling

### RW2 and ORF RAW

Panasonic RW2 and Olympus / OM System ORF files are developed through `rawpy`/LibRaw using:

- Camera white balance
- 16-bit output
- Linear gamma
- Disabled automatic brightness
- Highlight blending

RAW sensor data is not V-Log. RW2 and ORF files therefore use a separate RAW-development path before conversion to BT.2020 and HLG.

### V-Log Stills

For JPG, PNG, and TIFF inputs, the program assumes pixel values are already encoded as:

- Panasonic V-Log transfer curve
- Panasonic V-Gamut primaries

Do not use ordinary sRGB, Rec.709, or standard-camera-profile JPEGs as V-Log input.

## Output Compatibility

### HEIC

HEIC output is tagged as:

- BT.2020 colour primaries
- BT.2100 HLG / ARIB STD-B67 transfer function
- BT.2020 non-constant-luminance matrix

HEIC is generally the better choice for Apple devices and HDR-aware applications.

### JPEG XL

JPEG XL retains high bit depth and carries Rec.2020/HLG colour information. It is a strong archival and HDR delivery option where JPEG XL is supported.

### Viewing HDR

Use an HDR-capable display and HDR-aware software. If an app does not recognise HLG or Rec.2020 metadata, images may look flat, overly dark, washed out, or incorrectly saturated.

## Limitations

- This tool does not reproduce Panasonic’s or Olympus’s proprietary JPEG engines, Lightroom, macOS Preview, or Olympus Workspace exactly.
- LibRaw rendering can differ in white balance, colour response, noise reduction, demosaicing, lens corrections, and tone mapping.
- RAW formats currently supported are Panasonic RW2 and Olympus / OM System ORF. Support depends on the installed LibRaw/rawpy version and the individual camera model.
- Social-media platforms may recompress, strip HDR metadata, or convert uploads to SDR.
- HEIC encoding availability depends on the locally installed `libheif` build and its HEVC encoder plugin.
- JPEG XL support varies by operating system, browser, image viewer, and social platform.
- The tool processes batches sequentially to reduce memory usage during high-resolution RAW conversion.

## Troubleshooting

### `heif-enc was not found`

Install libheif and ensure its executable directory is on `PATH`.

```bash
# macOS
brew install libheif

# Ubuntu / Debian
sudo apt install libheif-examples
```

On Windows, install `libheif` through Conda Forge or use JPEG XL output.

### `cjxl was not found`

Install JPEG XL tools and ensure the executable directory is on `PATH`.

```bash
# macOS
brew install jpeg-xl

# Ubuntu / Debian
sudo apt install libjxl-tools
```

### The Image Looks Too Dark or Too Bright

Adjust exposure. Some RAW images taken with the V-Log photo style may initially appear severely underexposed in this independent LibRaw-based development pipeline. To compensate:

```bash
python LUMIX2HLG.py P1000000.RW2 --exposure +4.0
```

For bright HDR scenes, start around:

```bash
--exposure -0.5 --rolloff 0.25
```

### The Image Looks Different From Preview, Lightroom, or Olympus Workspace

This is expected for RAW input. The tool uses LibRaw rather than Panasonic’s, Olympus’s, Adobe’s, or Apple’s proprietary RAW-rendering pipeline.

First, try reducing or disabling roll-off:

```bash
python LUMIX2HLG.py P1000000.RW2 --rolloff 0
```

Then adjust exposure, contrast, and saturation as needed.

## License

[GNU General Public License v3.0](LICENSE)
