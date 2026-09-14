#!/usr/bin/env python3
"""vresize - shrink a video using ffmpeg: by resolution, by target file size,
or to fit a known platform's upload limit.

Single-file, standard-library-only tool - usable both as a CLI and as a
Python library. Requires ffmpeg and ffprobe on PATH.

CLI examples:
    vresize Movie_1080p.mkv 720p
    vresize clip.mp4 480p --codec h265 --crf 28 --dry-run
    vresize clip.mp4 --max-size 25MB
    vresize clip.mp4 --for discord
    vresize ./raw_footage 720p --recursive

Library example:
    from vresize import resize
    result = resize("clip.mp4", max_size="25MB")
    print(result.output, result.output_size)

Library calls are silent by default (stdlib logging, no handler attached
unless you configure one yourself - see logging.basicConfig). Pass
quiet=False to also get the live ffmpeg progress bar on stderr.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

# --- exit codes (CLI) -------------------------------------------------------
EXIT_OK = 0
EXIT_USAGE = 1      # bad args / missing prereq / output exists / would upscale
EXIT_FFMPEG = 2      # ffmpeg itself failed

logger = logging.getLogger("vresize")
logger.addHandler(logging.NullHandler())  # silent by default for library use


class ResizeError(Exception):
    """A file failed to process. Carries the exit code the CLI maps it to."""

    def __init__(self, msg: str, code: int = EXIT_USAGE) -> None:
        super().__init__(msg)
        self.code = code


class SkipExisting(Exception):
    """Internal signal: in batch mode, an output already exists and isn't
    being overwritten. Never raised outside a batch run; surfaced to callers
    of resize_directory() as a FileOutcome(status="skipped") instead."""


# --- shared encode settings --------------------------------------------
X264_PRESETS = (
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
)


@dataclass
class EncodeOptions:
    """Encode settings shared by the CLI and the public resize()/resize_directory()
    functions - keeps both paths running the exact same logic."""
    codec: str = "h264"
    crf: int | None = None
    preset: str = "medium"
    audio: str = "copy"
    fps: float | None = None
    overwrite: bool = False
    dry_run: bool = False
    quiet: bool = True

    def __post_init__(self) -> None:
        if self.crf is None:
            self.crf = 28 if self.codec == "h265" else 23
        if not (0 <= self.crf <= 51):
            raise ValueError(f"crf must be between 0 and 51 (got {self.crf})")
        if self.preset not in X264_PRESETS:
            raise ValueError(
                f"preset {self.preset!r} is not a valid x264/x265 preset; "
                f"choose one of: {', '.join(X264_PRESETS)}"
            )


# --- public result types -----------------------------------------------
@dataclass
class ResizeResult:
    output: Path
    input_size: int
    output_size: int | None  # None when dry_run - nothing was written
    source_width: int
    source_height: int
    output_height: int | None  # None if the source resolution was kept
    duration: float | None
    dry_run: bool


@dataclass
class FileOutcome:
    input: Path
    status: str  # "ok" | "skipped" | "failed"
    result: ResizeResult | None = None
    error: str | None = None


@dataclass
class BatchResult:
    files: list[FileOutcome]
    ok: int
    skipped: int
    failed: int


# --- resolution handling ------------------------------------------------
ALIASES = {
    "8k": 4320,
    "4k": 2160, "uhd": 2160,
    "2k": 1440, "qhd": 1440,
    "fhd": 1080, "fullhd": 1080,
    "hd": 720,
    "sd": 480,
}

# matches a resolution-ish token inside a filename stem, e.g. 1080p, 720P,
# 1920x1080, 4k, uhd - with non-alphanumeric boundaries on both sides
RES_TOKEN_RE = re.compile(
    r"(?<![0-9A-Za-z])"
    r"(?:\d{3,4}p|\d{3,4}x\d{3,4}|4k|8k|2k|uhd|qhd|fhd|fullhd)"
    r"(?![0-9A-Za-z])",
    re.IGNORECASE,
)

FASTSTART_EXTS = {".mp4", ".m4v", ".mov"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".ts", ".mpg", ".mpeg"}

# Approximate upload/attachment size limits as of Sept 2026. Platforms change
# these without notice - if hitting the limit exactly matters, verify first.
# name -> (max_size_bytes, max_height_or_None, human note)
PLATFORM_PRESETS: dict[str, tuple[int | None, int | None, str]] = {
    "discord": (25 * 1024 ** 2, None, "Discord free upload limit (25 MB)"),
    "discord-nitro": (500 * 1024 ** 2, None, "Discord Nitro upload limit (500 MB)"),
    "whatsapp": (16 * 1024 ** 2, None, "WhatsApp media limit (~16 MB)"),
    "telegram": (2000 * 1024 ** 2, None, "Telegram file limit (~2 GB, non-premium)"),
    "twitter": (512 * 1024 ** 2, 1080, "X/Twitter video limit (512 MB, 1080p)"),
}

SIZE_RE = re.compile(r"(?i)^\s*([0-9.]+)\s*(b|kb|mb|gb)?\s*$")


def status(msg: str = "") -> None:
    """Human-facing progress/status text, at INFO level. Silent unless a
    handler is attached - the CLI attaches one; a library caller can too via
    logging.basicConfig(), or leave it silent and just use the return value."""
    logger.info(msg)


def die(msg: str, code: int = EXIT_USAGE) -> "NoReturn":  # type: ignore[valid-type]
    """CLI-only: print a fatal error and exit the process. Never called from
    the public resize()/resize_directory() functions - they raise instead."""
    print(f"vresize: error: {msg}", file=sys.stderr)
    sys.exit(code)


def parse_target(value: str) -> int:
    """Turn '720p' / '720' / '4k' into a pixel height."""
    v = value.strip().lower()
    if v in ALIASES:
        return ALIASES[v]
    m = re.fullmatch(r"(\d{3,4})p?", v)
    if m:
        return int(m.group(1))
    raise argparse.ArgumentTypeError(
        f"unrecognized resolution {value!r}; use e.g. 1080p, 720p, 480p or 4k"
    )


def parse_size(value: str) -> int:
    """Turn '25MB' / '700KB' / '8GB' / a bare byte count into an int of bytes."""
    m = SIZE_RE.fullmatch(value)
    if not m:
        raise argparse.ArgumentTypeError(
            f"unrecognized size {value!r}; use e.g. 25MB, 700KB, 1.5GB"
        )
    num = float(m.group(1))
    unit = (m.group(2) or "mb").lower()
    mult = {"b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3}[unit]
    size = int(num * mult)
    if size <= 0:
        raise argparse.ArgumentTypeError("size must be positive")
    return size


def _coerce_target(target: str | int | None) -> int | None:
    """Library-facing: accept either an already-parsed height or a CLI-style
    string ('720p', '4k'). Raises ValueError (not argparse's exception type)
    on a bad string, matching normal Python API conventions."""
    if target is None or isinstance(target, int):
        return target
    try:
        return parse_target(target)
    except argparse.ArgumentTypeError as e:
        raise ValueError(str(e)) from e


def _coerce_size(size: str | int | None) -> int | None:
    if size is None or isinstance(size, int):
        return size
    try:
        return parse_size(size)
    except argparse.ArgumentTypeError as e:
        raise ValueError(str(e)) from e


def _preset_lookup(for_platform: str | None) -> tuple[int | None, int | None, str | None]:
    if for_platform is None:
        return None, None, None
    if for_platform not in PLATFORM_PRESETS:
        raise ValueError(
            f"unknown platform {for_platform!r}; choose one of: {', '.join(sorted(PLATFORM_PRESETS))}"
        )
    return PLATFORM_PRESETS[for_platform]


def human_size_tag(n: int) -> str:
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= div:
            val = n / div
            s = f"{val:.0f}" if val == int(val) else f"{val:.1f}"
            return f"{s}{unit}"
    return f"{n}B"


def make_tag(height: int | None, max_size: int | None) -> str:
    if height is not None:
        return f"{height}p"
    if max_size is not None:
        return human_size_tag(max_size)
    return "out"


def quote_cmd(cmd: list[str]) -> list[str]:
    """Quote a command for display, using the quoting rules of the current OS
    shell so a --dry-run line can actually be pasted back in."""
    if os.name == "nt":
        def q(s: str) -> str:
            if s == "" or any(c in s for c in ' \t"'):
                return '"' + s.replace('"', '\\"') + '"'
            return s
        return [q(c) for c in cmd]
    return [shlex.quote(c) for c in cmd]


def require_tools() -> None:
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        raise ResizeError(
            f"{' and '.join(missing)} not found on PATH.\n"
            "  Install FFmpeg and add it to PATH:\n"
            "    Windows : https://www.gyan.dev/ffmpeg/builds/  (or: choco install ffmpeg)\n"
            "    macOS   : brew install ffmpeg\n"
            "    Linux   : sudo apt install ffmpeg"
        )


def probe(path: Path) -> dict:
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError as e:
        raise ResizeError(
            f"ffprobe could not read {path.name!r}: {e.stderr.strip() or e}", EXIT_FFMPEG
        )
    data = json.loads(out)

    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise ResizeError(f"no video stream found in {path.name!r}")
    has_audio = any(s.get("codec_type") == "audio" for s in data["streams"])

    duration = None
    for src in (video.get("duration"), data.get("format", {}).get("duration")):
        try:
            duration = float(src)
            break
        except (TypeError, ValueError):
            continue

    return {
        "width": int(video["width"]),
        "height": int(video["height"]),
        "vcodec": video.get("codec_name", "?"),
        "duration": duration,
        "has_audio": has_audio,
    }


def fmt_time(seconds: float | None) -> str:
    if seconds is None:
        return "??:??"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def resolve_height(explicit: int | None, preset_max_height: int | None, source_height: int) -> int | None:
    """Decide the output height: an explicit target wins outright; otherwise a
    platform preset's height cap applies only if the source actually exceeds
    it; otherwise no resize (None) - the source resolution is kept."""
    if explicit is not None:
        return explicit
    if preset_max_height is not None and source_height > preset_max_height:
        return preset_max_height
    return None


def find_videos(root: Path, recursive: bool) -> list[Path]:
    it = root.rglob("*") if recursive else root.glob("*")
    return sorted(p for p in it if p.is_file() and p.suffix.lower() in VIDEO_EXTS)


def derive_output_path(src: Path, tag: str, explicit: str | None, do_token_replace: bool) -> Path:
    if explicit:
        return Path(explicit).expanduser()

    stem = src.stem
    if do_token_replace and RES_TOKEN_RE.search(stem):
        new_stem = RES_TOKEN_RE.sub(tag, stem, count=1)
    else:
        new_stem = f"{stem}_{tag}"
    return src.with_name(new_stem + src.suffix)


def build_cmd(src: Path, dst: Path, height: int | None, opts: EncodeOptions,
              has_audio: bool) -> list[str]:
    """Single-pass, constant-quality (CRF) encode - used when no file-size
    budget is in play."""
    vcodec = "libx265" if opts.codec == "h265" else "libx264"

    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(src)]
    if height is not None:
        cmd += ["-vf", f"scale=-2:{height}"]
    cmd += ["-c:v", vcodec, "-crf", str(opts.crf), "-preset", opts.preset]
    cmd += ["-pix_fmt", "yuv420p"]
    if opts.fps:
        cmd += ["-r", str(opts.fps)]

    if opts.audio == "none" or not has_audio:
        cmd += ["-an"]
    elif opts.audio == "aac":
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    else:  # copy
        cmd += ["-c:a", "copy"]

    if dst.suffix.lower() in FASTSTART_EXTS:
        cmd += ["-movflags", "+faststart"]

    cmd += ["-map_metadata", "0"]
    cmd += ["-progress", "pipe:1", "-nostats"]
    cmd += [str(dst)]
    return cmd


def compute_bitrate_kbps(max_size_bytes: int, duration: float | None, audio_bps: int,
                          overhead_frac: float = 0.02) -> float:
    """Back out a target video bitrate (kbps) from a byte budget, container
    duration and the audio bitrate that will also eat into that budget."""
    if not duration or duration <= 0:
        raise ResizeError("cannot target a file size: source duration is unknown")
    total_bits = max_size_bytes * 8 * (1 - overhead_frac)
    video_bits = total_bits - audio_bps * duration
    kbps = video_bits / duration / 1000
    if kbps < 50:
        raise ResizeError(
            f"--max-size ({human_size_tag(max_size_bytes)}) is too small for a "
            f"{fmt_time(duration)} clip; increase it or pass --audio none"
        )
    return kbps


def build_pass_cmd(src: Path, dst: Path, height: int | None, vcodec: str, preset: str,
                    kbps: float, pass_num: int, passlogfile: str, fps: float | None,
                    audio_mode: str, has_audio: bool, faststart: bool) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(src)]
    if height is not None:
        cmd += ["-vf", f"scale=-2:{height}"]
    cmd += ["-c:v", vcodec, "-b:v", f"{kbps:.0f}k", "-preset", preset]
    cmd += ["-pix_fmt", "yuv420p"]
    if fps:
        cmd += ["-r", str(fps)]
    cmd += ["-pass", str(pass_num), "-passlogfile", passlogfile]
    cmd += ["-progress", "pipe:1", "-nostats"]

    if pass_num == 1:
        cmd += ["-an", "-f", "null", os.devnull]
        return cmd

    if audio_mode == "none" or not has_audio:
        cmd += ["-an"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    if faststart:
        cmd += ["-movflags", "+faststart"]
    cmd += ["-map_metadata", "0", str(dst)]
    return cmd


def run_with_progress(cmd: list[str], duration: float | None, quiet: bool, label: str = "") -> int:
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    stderr_tail: list[str] = []
    start = time.monotonic()
    last_render = 0.0

    # drain stderr on a thread so it can't deadlock the pipe
    import threading

    def _pump_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_tail.append(line.rstrip("\n"))
            del stderr_tail[:-25]

    t = threading.Thread(target=_pump_stderr, daemon=True)
    t.start()

    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        if not line.startswith("out_time_us=") or quiet:
            continue
        now = time.monotonic()
        if now - last_render < 0.2:
            continue
        last_render = now
        try:
            out_us = int(line.split("=", 1)[1])
        except ValueError:
            continue
        done = out_us / 1_000_000
        elapsed = now - start
        speed = done / elapsed if elapsed > 0 else 0.0
        if duration:
            frac = min(done / duration, 1.0)
            bars = int(frac * 30)
            print(
                f"\r  {label}[{'#' * bars}{'-' * (30 - bars)}] {frac * 100:5.1f}%  "
                f"{fmt_time(done)}/{fmt_time(duration)}  {speed:4.1f}x   ",
                end="", file=sys.stderr, flush=True,
            )
        else:
            print(
                f"\r  {label}encoded {fmt_time(done)}  {speed:4.1f}x   ",
                end="", file=sys.stderr, flush=True,
            )

    proc.wait()
    t.join(timeout=1)
    proc.stdout.close()
    proc.stderr.close()
    if not quiet:
        print("\r" + " " * 70 + "\r", end="", file=sys.stderr, flush=True)

    if proc.returncode != 0:
        print("ffmpeg failed:\n  " + "\n  ".join(stderr_tail[-20:]), file=sys.stderr)
    return proc.returncode


def run_two_pass(src: Path, dst: Path, height: int | None, opts: EncodeOptions,
                  info: dict, max_size: int, audio_mode: str) -> int:
    audio_bps = 0 if (audio_mode == "none" or not info["has_audio"]) else 128_000
    kbps = compute_bitrate_kbps(max_size, info["duration"], audio_bps)
    vcodec = "libx265" if opts.codec == "h265" else "libx264"
    faststart = dst.suffix.lower() in FASTSTART_EXTS

    if opts.dry_run:
        placeholder_log = str(Path(tempfile.gettempdir()) / "vresize-2pass" / "pass")
        for n in (1, 2):
            cmd = build_pass_cmd(src, dst, height, vcodec, opts.preset, kbps, n,
                                  placeholder_log, opts.fps, audio_mode, info["has_audio"], faststart)
            print(" ".join(quote_cmd(cmd)))
        return EXIT_OK

    status(f"  bitrate: ~{kbps:.0f} kbps video (2-pass, targeting {human_size_tag(max_size)})")
    tmpdir = tempfile.mkdtemp(prefix="vresize-2pass-")
    try:
        passlog = str(Path(tmpdir) / "pass")
        for n in (1, 2):
            cmd = build_pass_cmd(src, dst, height, vcodec, opts.preset, kbps, n,
                                  passlog, opts.fps, audio_mode, info["has_audio"], faststart)
            rc = run_with_progress(cmd, info["duration"], opts.quiet, label=f"[pass {n}/2] ")
            if rc != 0:
                return rc
        return EXIT_OK
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def process_one(src: Path, opts: EncodeOptions, target: int | None, preset_max_height: int | None,
                 effective_max_size: int | None, explicit_output: str | None,
                 batch: bool = False) -> ResizeResult:
    """Process one file end to end. Returns a ResizeResult on success; raises
    ResizeError (or SkipExisting, in batch mode) on failure."""
    if not src.is_file():
        raise ResizeError(f"input file not found: {src}")

    info = probe(src)
    status(
        f"  source : {info['width']}x{info['height']}  {info['vcodec']}  "
        f"{fmt_time(info['duration'])}  {'audio' if info['has_audio'] else 'no audio'}"
    )

    if target is not None and target >= info["height"]:
        raise ResizeError(
            f"target {target}p is not smaller than the source ({info['height']}p). "
            "vresize only downscales."
        )

    height = resolve_height(target, preset_max_height, info["height"])

    audio_mode = opts.audio
    if effective_max_size is not None and audio_mode == "copy":
        status("  note   : a size budget forces audio to aac (copy would make the size unpredictable)")
        audio_mode = "aac"

    tag = make_tag(height, effective_max_size)
    dst = derive_output_path(src, tag, explicit_output, do_token_replace=(height is not None))

    try:
        same = dst.resolve() == src.resolve()
    except OSError:
        same = str(dst) == str(src)
    if same:
        raise ResizeError("output path would be the same as the input; choose another output path")

    if dst.exists() and not opts.overwrite:
        if batch:
            raise SkipExisting(f"{dst.name} already exists (pass overwrite=True to replace)")
        raise ResizeError(f"output already exists: {dst}  (pass overwrite=True to replace)")

    if not dst.parent.exists():
        raise ResizeError(f"output directory does not exist: {dst.parent}")

    if height is not None and effective_max_size is None:
        status(f"  target : {height}p  ({opts.codec}, crf {opts.crf}, preset {opts.preset})")
    elif height is not None:
        status(f"  target : {height}p, budget {human_size_tag(effective_max_size)}  ({opts.codec})")
    else:
        status(f"  target : source resolution, budget {human_size_tag(effective_max_size)}  ({opts.codec})")
    status(f"  output : {dst}")

    if effective_max_size is not None:
        rc = run_two_pass(src, dst, height, opts, info, effective_max_size, audio_mode)
    else:
        cmd = build_cmd(src, dst, height, opts, info["has_audio"])
        if opts.dry_run:
            print(" ".join(quote_cmd(cmd)))
            rc = EXIT_OK
        else:
            rc = run_with_progress(cmd, info["duration"], opts.quiet)

    if opts.dry_run:
        return ResizeResult(
            output=dst, input_size=src.stat().st_size, output_size=None,
            source_width=info["width"], source_height=info["height"],
            output_height=height, duration=info["duration"], dry_run=True,
        )

    if rc != 0:
        if dst.exists():
            try:
                dst.unlink()
            except OSError:
                pass
        raise ResizeError("ffmpeg failed (see output above)", EXIT_FFMPEG)

    # keep timestamps sane in file listings
    try:
        st = src.stat()
        os.utime(dst, (st.st_atime, st.st_mtime))
    except OSError:
        pass

    out_size = dst.stat().st_size
    status(f"  done   : {dst}  ({out_size / (1024 * 1024):.1f} MB)")
    return ResizeResult(
        output=dst, input_size=src.stat().st_size, output_size=out_size,
        source_width=info["width"], source_height=info["height"],
        output_height=height, duration=info["duration"], dry_run=False,
    )


def _run_batch(videos: list[Path], opts: EncodeOptions, target: int | None,
               preset_max_height: int | None, effective_max_size: int | None) -> BatchResult:
    outcomes: list[FileOutcome] = []
    ok = skipped = failed = 0
    for i, src in enumerate(videos, 1):
        status(f"\n[{i}/{len(videos)}] {src.name}")
        try:
            result = process_one(src, opts, target, preset_max_height, effective_max_size, None, batch=True)
            outcomes.append(FileOutcome(input=src, status="ok", result=result))
            ok += 1
        except SkipExisting as e:
            status(f"  skip   : {e}")
            outcomes.append(FileOutcome(input=src, status="skipped", error=str(e)))
            skipped += 1
        except ResizeError as e:
            status(f"  error  : {e}")
            outcomes.append(FileOutcome(input=src, status="failed", error=str(e)))
            failed += 1
    return BatchResult(files=outcomes, ok=ok, skipped=skipped, failed=failed)


# --- Public API -----------------------------------------------------------
__all__ = [
    "resize", "resize_directory",
    "ResizeResult", "FileOutcome", "BatchResult", "ResizeError",
    "main",
]


def resize(
    input: str | Path,
    target: str | int | None = None,
    *,
    max_size: str | int | None = None,
    for_platform: str | None = None,
    codec: str = "h264",
    crf: int | None = None,
    preset: str = "medium",
    audio: str = "copy",
    fps: float | None = None,
    output: str | Path | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
    quiet: bool = True,
) -> ResizeResult:
    """Shrink a single video file. Parameters mirror the CLI flags of the
    same name (target: '720p'/'4k'/720; max_size: '25MB'/25000000;
    for_platform: 'discord', 'whatsapp', ...).

    Raises ValueError for a bad argument (before touching ffmpeg or the
    filesystem), ResizeError for anything that goes wrong once the file is
    being processed (ffmpeg missing, ffmpeg failure, output already exists,
    upscale requested, ...).

    Silent by default - call logging.basicConfig() first (or attach your own
    handler to logging.getLogger("vresize")) to see progress, and/or pass
    quiet=False for the live ffmpeg progress bar on stderr.
    """
    opts = EncodeOptions(codec=codec, crf=crf, preset=preset, audio=audio, fps=fps,
                          overwrite=overwrite, dry_run=dry_run, quiet=quiet)
    height = _coerce_target(target)
    size = _coerce_size(max_size)
    preset_size, preset_max_height, preset_note = _preset_lookup(for_platform)
    if size is None:
        size = preset_size
    if height is None and size is None:
        raise ValueError("specify target, max_size, or for_platform")

    require_tools()
    src = Path(input).expanduser()
    if not src.is_file():
        raise ResizeError(f"input not found: {src}")

    if preset_note:
        status(f"  preset : for_platform={for_platform!r}  ({preset_note})")

    return process_one(src, opts, height, preset_max_height, size,
                        str(output) if output is not None else None, batch=False)


def resize_directory(
    input_dir: str | Path,
    target: str | int | None = None,
    *,
    recursive: bool = False,
    max_size: str | int | None = None,
    for_platform: str | None = None,
    codec: str = "h264",
    crf: int | None = None,
    preset: str = "medium",
    audio: str = "copy",
    fps: float | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
    quiet: bool = True,
) -> BatchResult:
    """Shrink every video in a directory (optionally recursive). Same
    parameters as resize() minus output/input-file-specific ones. An output
    that already exists is skipped (not an error) unless overwrite=True, so
    a batch run is safe to call again to resume. Raises ValueError/ResizeError
    the same way resize() does for setup problems; a single file's ffmpeg
    failure is recorded in the returned BatchResult rather than raised, so
    the rest of the batch still runs.
    """
    opts = EncodeOptions(codec=codec, crf=crf, preset=preset, audio=audio, fps=fps,
                          overwrite=overwrite, dry_run=dry_run, quiet=quiet)
    height = _coerce_target(target)
    size = _coerce_size(max_size)
    preset_size, preset_max_height, preset_note = _preset_lookup(for_platform)
    if size is None:
        size = preset_size
    if height is None and size is None:
        raise ValueError("specify target, max_size, or for_platform")

    require_tools()
    root = Path(input_dir).expanduser()
    if not root.is_dir():
        raise ResizeError(f"not a directory: {root}")

    videos = find_videos(root, recursive)
    if not videos:
        raise ResizeError(f"no video files found in {root}" + ("" if recursive else " (try recursive=True)"))

    status(f"  batch  : {len(videos)} file(s) in {root}" + ("  (recursive)" if recursive else ""))
    if preset_note:
        status(f"  preset : for_platform={for_platform!r}  ({preset_note})")

    return _run_batch(videos, opts, height, preset_max_height, size)


# --- CLI --------------------------------------------------------------------
_cli_logging_configured = False


def _configure_cli_logging() -> None:
    """Make status() visible on stderr, matching the CLI's original
    print-based output. Only called from main() - library use stays silent
    unless the caller configures logging themselves."""
    global _cli_logging_configured
    if _cli_logging_configured:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    _cli_logging_configured = True


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vresize",
        description="Shrink a video with ffmpeg: by resolution, by target file size, or to fit a platform's upload limit.",
    )
    p.add_argument("input", help="source video file, or a directory for batch mode")
    p.add_argument(
        "target", nargs="?", type=parse_target, default=None,
        help="target resolution: 1080p, 720p, 480p, 4k, ... (optional if --max-size or --for is given)",
    )
    p.add_argument("-o", "--output", help="explicit output path (overrides auto-naming; single-file only)")
    p.add_argument("--codec", choices=("h264", "h265"), default="h264",
                    help="video codec (default: h264)")
    p.add_argument("--crf", type=int,
                    help="quality; lower = better, 0-51 (default: 23 for h264, 28 for h265)")
    p.add_argument("--preset", default="medium",
                    help=f"x264/x265 speed preset (default: medium): {', '.join(X264_PRESETS)}")
    p.add_argument("--audio", choices=("copy", "aac", "none"), default="copy",
                    help="audio handling (default: copy)")
    p.add_argument("--fps", type=float,
                    help="set output frame rate (forces this rate; does not merely cap it)")
    p.add_argument("--max-size", dest="max_size", type=parse_size, metavar="SIZE",
                    help="target output file size, e.g. 25MB, 700KB, 1.5GB (two-pass encode)")
    p.add_argument("--for", dest="for_", choices=sorted(PLATFORM_PRESETS), metavar="PLATFORM",
                    help=f"apply a known platform's upload limit: {', '.join(sorted(PLATFORM_PRESETS))}")
    p.add_argument("--recursive", action="store_true",
                    help="with a directory input, recurse into subfolders")
    p.add_argument("--overwrite", action="store_true",
                    help="allow overwriting an existing output file")
    p.add_argument("--dry-run", action="store_true",
                    help="print the ffmpeg command(s) and planned output, run nothing")
    p.add_argument("--quiet", action="store_true", help="suppress the progress bar")
    return p


def main(argv: list[str] | None = None) -> int:
    _configure_cli_logging()
    args = build_arg_parser().parse_args(argv)

    try:
        opts = EncodeOptions(
            codec=args.codec, crf=args.crf, preset=args.preset, audio=args.audio,
            fps=args.fps, overwrite=args.overwrite, dry_run=args.dry_run, quiet=args.quiet,
        )
    except ValueError as e:
        die(str(e))

    try:
        require_tools()
    except ResizeError as e:
        die(str(e), e.code)

    src_root = Path(args.input).expanduser()
    if not src_root.exists():
        die(f"input not found: {src_root}")

    preset_max_size, preset_max_height, preset_note = _preset_lookup(args.for_)
    effective_max_size = args.max_size if args.max_size is not None else preset_max_size

    if args.target is None and effective_max_size is None:
        die("specify a target resolution (e.g. 720p), --max-size, or --for <platform>")

    if src_root.is_dir():
        if args.output:
            die("-o/--output can't be used with a directory input; each file gets an auto-named output")
        videos = find_videos(src_root, args.recursive)
        if not videos:
            die(f"no video files found in {src_root}" + ("" if args.recursive else " (try --recursive)"))

        status(f"  batch  : {len(videos)} file(s) in {src_root}" + ("  (recursive)" if args.recursive else ""))
        if preset_note:
            status(f"  preset : --for {args.for_}  ({preset_note})")

        result = _run_batch(videos, opts, args.target, preset_max_height, effective_max_size)
        status(f"\nbatch done: {result.ok} converted, {result.skipped} skipped, {result.failed} failed")
        return EXIT_OK if result.failed == 0 else EXIT_FFMPEG

    if preset_note:
        status(f"  preset : --for {args.for_}  ({preset_note})")

    try:
        process_one(src_root, opts, args.target, preset_max_height, effective_max_size, args.output, batch=False)
    except ResizeError as e:
        die(str(e), e.code)
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nvresize: interrupted", file=sys.stderr)
        sys.exit(130)
