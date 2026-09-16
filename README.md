# vresize

A single-file, **zero-dependency** command-line tool *and Python library*
that shrinks a video with ffmpeg. By resolution, by a target file size, or
to fit a known platform's upload limit (Discord, WhatsApp, Telegram, etc.).

Why another video-resizing wrapper? Most small ffmpeg wrappers on GitHub/PyPI
add a dependency (`ffmpeg-python`, `click`, `rich`, ...) for something a
100%-stdlib script can do just as well, and stop at "resize to a resolution."
`vresize` stays dependency-free (readable start to finish in a few minutes)
and adds the two things people actually ask ffmpeg forums for: *"make this
fit under 25MB"* and *"make this small enough for Discord."*

## Prerequisites

- Python 3.9+
- **FFmpeg** (`ffmpeg` and `ffprobe`) on your `PATH`
  - Windows: <https://www.gyan.dev/ffmpeg/builds/> (or `choco install ffmpeg`)
  - macOS: `brew install ffmpeg`
  - Linux: `sudo apt install ffmpeg`

`vresize` notifies with a clear error and exits if FFmpeg is not found.

## Install

```
pip install vresize
```

This gives you a `vresize` command on any OS. Prefer not to install it? It's
one file: `src/vresize.py` in this repo with no third-party imports; copy
it anywhere and run `python vresize.py ...` directly, or use the included
Windows `vresize.bat` shim.

## Use as a library

The same file is a plain importable module. Useful if you need to shrink a
video from inside another Python app rather than shelling out to the CLI:

```python
from vresize import resize, resize_directory, ResizeError

result = resize("clip.mp4", max_size="25MB")     # or target="720p", or for_platform="discord"
print(result.output, result.output_size)

batch = resize_directory("./raw_footage", "720p", recursive=True)
print(f"{batch.ok} converted, {batch.skipped} skipped, {batch.failed} failed")
```

`resize()`/`resize_directory()` take the same knobs as the CLI flags
(`codec`, `crf`, `preset`, `audio`, `fps`, `overwrite`, `dry_run`, ...) as
keyword arguments, and accept `target`/`max_size` either as the same strings
the CLI parses (`"720p"`, `"25MB"`) or as already-typed values (`720`,
`26214400`). They raise `ValueError` for a bad argument (checked before
anything touches ffmpeg or the filesystem) and `ResizeError` for a real
failure (ffmpeg missing, ffmpeg error, output already exists, an upscale
requested, ...), `resize_directory()` records a single file's failure in
the returned `BatchResult` instead of raising, so the rest of the batch
still runs.

Library calls are **silent by default**. Nothing is printed unless you
configure logging yourself:

```python
import logging
logging.basicConfig(level=logging.INFO)   # see status lines on stderr

result = resize("clip.mp4", "720p", quiet=False)  # also show the live ffmpeg progress bar
```

## Usage

```
vresize INPUT [TARGET] [options]
```

Give it **one** of a resolution, `--max-size`, or `--for <platform>` (you can
combine `--for`/`--max-size` with an explicit resolution too, e.g. cap both
size and resolution at once).

| Argument / option        | Meaning                                                        |
|--------------------------|-----------------------------------------------------------------|
| `INPUT`                  | source video file, or a directory for batch mode                |
| `TARGET`                 | `2160p` `1440p` `1080p` `720p` `480p` `360p` `240p`, a bare height like `720`, or an alias like `4k` / `hd`, optional if `--max-size`/`--for` is given |
| `--max-size SIZE`        | target output file size, e.g. `25MB`, `700KB`, `1.5GB` |
| `--for PLATFORM`         | apply a known upload-limit preset: `discord`, `discord-nitro`, `whatsapp`, `telegram`, `twitter` |
| `-o, --output PATH`      | explicit output path (single file only; overrides auto-naming)  |
| `--codec {h264,h265}`    | video codec (default `h264`)                                     |
| `--crf INT`              | quality, lower = better, 0–51 (default `23` for h264, `28` for h265); ignored when `--max-size`/`--for` is set |
| `--preset NAME`          | x264/x265 speed preset (default `medium`): `ultrafast` … `veryslow` |
| `--audio {copy,aac,none}`| audio handling (default `copy`); forced to `aac` when a size budget is active, since `copy` makes the final size unpredictable |
| `--fps FLOAT`            | set the output frame rate (forces it, does not merely cap the source rate) |
| `--recursive`            | with a directory input, recurse into subfolders                 |
| `--overwrite`            | replace an existing output file (in batch mode, an existing output is *skipped* instead, unless this is set) |
| `--dry-run`              | print the ffmpeg command(s) and planned output, run nothing      |
| `--quiet`                | suppress the progress bar                                        |

`vresize` only downscales, it refuses a resolution target that isn't smaller
than the source. A file-size or platform target with no explicit resolution
keeps the source resolution and re-encodes to hit the byte budget instead
(a platform preset that also caps resolution, like `twitter`, still only
kicks in if the source actually exceeds that height).

### Output naming

The result goes in the **same folder** as the source, keeping the same container:

- If the filename already contains a resolution token, it is replaced:
  `Movie.1080p.mkv` → `Movie.480p.mkv`
- Otherwise the target tag is appended: `Movie.mkv` → `Movie_480p.mkv`,
  `Movie.mkv` → `Movie_25MB.mkv`

### Examples

```
vresize Movie_1080p.mkv 720p
vresize clip.mp4 480p --codec h265 --crf 28
vresize trailer.mov 720p --dry-run

# Fit a byte budget instead of a resolution
vresize clip.mp4 --max-size 25MB

# Fit a known platform's upload limit
vresize clip.mp4 --for discord
vresize clip.mp4 --for twitter          # also caps resolution at 1080p if needed

# Batch a whole folder
vresize ./raw_footage 720p --recursive
```

### Target file-size mode

`--max-size` (and `--for`, which sets it from a preset table) runs a real
two-pass encode: `vresize` probes the clip's duration, backs out the video
bitrate needed to hit the byte budget (accounting for audio bitrate and a
small container-overhead margin), then encodes pass 1 and pass 2 with that
bitrate. Expect the result to land within a few percent of the target for
typical footage — very simple/low-motion source content can end up smaller
than the budget, since x264/x265 won't spend bits it doesn't need even when
more are available.

### Platform presets

| `--for` value    | Limit                          |
|-------------------|--------------------------------|
| `discord`          | 25 MB (free tier)              |
| `discord-nitro`    | 500 MB                         |
| `whatsapp`         | ~16 MB                         |
| `telegram`         | ~2 GB (non-premium)            |
| `twitter`          | 512 MB, capped at 1080p        |

These are best-effort, approximate figures (checked Sept 2026). Verify current limits yourself before
relying on this for anything where going over matters.

### Batch mode

Pass a directory instead of a file to process every video in it (add
`--recursive` to also descend into subfolders). Each file is processed with
the same options; an output that already exists is **skipped** rather than
treated as an error, so a batch run is safe to re-run/resume. The command
exits non-zero if any file failed or ffmpeg errored on it, after finishing
the rest of the batch.

## Design principles

- **Zero dependencies.** Standard library only, nothing to update, nothing
  to audit beyond this one file plus ffmpeg itself.
- **Downscale-only, on purpose.** If you want an exact re-encode at the same
  size, call ffmpeg directly
- **Fails safe.** Refuses to overwrite silently, cleans up partial output on
  a failed encode, and never touches the input file.

## Development

```
python -m unittest discover -s tests
```

No ffmpeg required to run the test suite, it covers the pure-Python parsing,
naming, and command-construction logic. Exercise the actual ffmpeg pipeline
with `--dry-run` against a real file, then a real encode.

## License

MIT - see [LICENSE](LICENSE).
