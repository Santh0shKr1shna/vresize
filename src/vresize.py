#!/usr/bin/env python3
"""vresize - shrink a video using ffmpeg: by resolution, by target file size,
or to fit a known platform's upload limit.

Single-file, standard-library-only CLI. Requires ffmpeg and ffprobe on PATH.

Examples:
    vresize Movie_1080p.mkv 720p
    vresize clip.mp4 480p --codec h265 --crf 28 --dry-run
    vresize clip.mp4 --max-size 25MB
    vresize clip.mp4 --for discord
    vresize ./raw_footage 720p --recursive
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# --- exit codes -------------------------------------------------------------
EXIT_OK = 0
EXIT_USAGE = 1      # bad args / missing prereq / output exists / would upscale
EXIT_FFMPEG = 2      # ffmpeg itself failed


class ProcessError(Exception):
    """A single file failed to process. Carries the exit code it should map to."""

    def __init__(self, msg: str, code: int = EXIT_USAGE) -> None:
        super().__init__(msg)
        self.code = code


class SkipExisting(Exception):
    """Raised in batch mode when an output already exists and isn't --overwrite."""


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

X264_PRESETS = (
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
)

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
    """Human-facing progress/status text -> stderr, so stdout stays clean."""
    print(msg, file=sys.stderr, flush=True)


def die(msg: str, code: int = EXIT_USAGE) -> "NoReturn":  # type: ignore[valid-type]
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
        die(
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
        raise ProcessError(
            f"ffprobe could not read {path.name!r}: {e.stderr.strip() or e}", EXIT_FFMPEG
        )
    data = json.loads(out)

    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise ProcessError(f"no video stream found in {path.name!r}")
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


def build_cmd(src: Path, dst: Path, height: int | None, args: argparse.Namespace,
              has_audio: bool) -> list[str]:
    """Single-pass, constant-quality (CRF) encode - used when no file-size
    budget is in play."""
    vcodec = "libx265" if args.codec == "h265" else "libx264"

    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(src)]
    if height is not None:
        cmd += ["-vf", f"scale=-2:{height}"]
    cmd += ["-c:v", vcodec, "-crf", str(args.crf), "-preset", args.preset]
    cmd += ["-pix_fmt", "yuv420p"]
    if args.fps:
        cmd += ["-r", str(args.fps)]

    if args.audio == "none" or not has_audio:
        cmd += ["-an"]
    elif args.audio == "aac":
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
        raise ProcessError("cannot target a file size: source duration is unknown")
    total_bits = max_size_bytes * 8 * (1 - overhead_frac)
    video_bits = total_bits - audio_bps * duration
    kbps = video_bits / duration / 1000
    if kbps < 50:
        raise ProcessError(
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
    if not quiet:
        print("\r" + " " * 70 + "\r", end="", file=sys.stderr, flush=True)

    if proc.returncode != 0:
        print("ffmpeg failed:\n  " + "\n  ".join(stderr_tail[-20:]), file=sys.stderr)
    return proc.returncode


def run_two_pass(src: Path, dst: Path, height: int | None, args: argparse.Namespace,
                  info: dict, max_size: int, audio_mode: str) -> int:
    audio_bps = 0 if (audio_mode == "none" or not info["has_audio"]) else 128_000
    kbps = compute_bitrate_kbps(max_size, info["duration"], audio_bps)
    vcodec = "libx265" if args.codec == "h265" else "libx264"
    faststart = dst.suffix.lower() in FASTSTART_EXTS

    if args.dry_run:
        placeholder_log = str(Path(tempfile.gettempdir()) / "vresize-2pass" / "pass")
        for n in (1, 2):
            cmd = build_pass_cmd(src, dst, height, vcodec, args.preset, kbps, n,
                                  placeholder_log, args.fps, audio_mode, info["has_audio"], faststart)
            print(" ".join(quote_cmd(cmd)))
        return EXIT_OK

    status(f"  bitrate: ~{kbps:.0f} kbps video (2-pass, targeting {human_size_tag(max_size)})")
    tmpdir = tempfile.mkdtemp(prefix="vresize-2pass-")
    try:
        passlog = str(Path(tmpdir) / "pass")
        for n in (1, 2):
            cmd = build_pass_cmd(src, dst, height, vcodec, args.preset, kbps, n,
                                  passlog, args.fps, audio_mode, info["has_audio"], faststart)
            rc = run_with_progress(cmd, info["duration"], args.quiet, label=f"[pass {n}/2] ")
            if rc != 0:
                return rc
        return EXIT_OK
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def process_one(src: Path, args: argparse.Namespace, preset_max_height: int | None,
                 effective_max_size: int | None, explicit_output: str | None,
                 batch: bool = False) -> Path:
    """Process one file end to end. Returns the output path on success; raises
    ProcessError (or SkipExisting, in batch mode) on failure."""
    if not src.is_file():
        raise ProcessError(f"input file not found: {src}")

    info = probe(src)
    status(
        f"  source : {info['width']}x{info['height']}  {info['vcodec']}  "
        f"{fmt_time(info['duration'])}  {'audio' if info['has_audio'] else 'no audio'}"
    )

    if args.target is not None and args.target >= info["height"]:
        raise ProcessError(
            f"target {args.target}p is not smaller than the source ({info['height']}p). "
            "vresize only downscales."
        )

    height = resolve_height(args.target, preset_max_height, info["height"])

    audio_mode = args.audio
    if effective_max_size is not None and audio_mode == "copy":
        status("  note   : --max-size/--for forces audio to aac (copy would make the size unpredictable)")
        audio_mode = "aac"

    tag = make_tag(height, effective_max_size)
    dst = derive_output_path(src, tag, explicit_output, do_token_replace=(height is not None))

    try:
        same = dst.resolve() == src.resolve()
    except OSError:
        same = str(dst) == str(src)
    if same:
        raise ProcessError("output path would be the same as the input; pass -o to choose another")

    if dst.exists() and not args.overwrite:
        if batch:
            raise SkipExisting(f"{dst.name} already exists (pass --overwrite to replace)")
        raise ProcessError(f"output already exists: {dst}  (pass --overwrite to replace)")

    if not dst.parent.exists():
        raise ProcessError(f"output directory does not exist: {dst.parent}")

    if height is not None and effective_max_size is None:
        status(f"  target : {height}p  ({args.codec}, crf {args.crf}, preset {args.preset})")
    elif height is not None:
        status(f"  target : {height}p, budget {human_size_tag(effective_max_size)}  ({args.codec})")
    else:
        status(f"  target : source resolution, budget {human_size_tag(effective_max_size)}  ({args.codec})")
    status(f"  output : {dst}")

    if effective_max_size is not None:
        rc = run_two_pass(src, dst, height, args, info, effective_max_size, audio_mode)
    else:
        cmd = build_cmd(src, dst, height, args, info["has_audio"])
        if args.dry_run:
            print(" ".join(quote_cmd(cmd)))
            return dst
        rc = run_with_progress(cmd, info["duration"], args.quiet)

    if args.dry_run:
        return dst

    if rc != 0:
        if dst.exists():
            try:
                dst.unlink()
            except OSError:
                pass
        raise ProcessError("ffmpeg failed (see output above)", EXIT_FFMPEG)

    # keep timestamps sane in file listings
    try:
        st = src.stat()
        os.utime(dst, (st.st_atime, st.st_mtime))
    except OSError:
        pass

    size_mb = dst.stat().st_size / (1024 * 1024)
    status(f"  done   : {dst}  ({size_mb:.1f} MB)")
    return dst


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
    args = build_arg_parser().parse_args(argv)

    if args.crf is None:
        args.crf = 28 if args.codec == "h265" else 23
    if not (0 <= args.crf <= 51):
        die(f"--crf must be between 0 and 51 (got {args.crf})")
    if args.preset not in X264_PRESETS:
        die(f"--preset {args.preset!r} is not a valid x264/x265 preset; choose one of: {', '.join(X264_PRESETS)}")

    require_tools()

    src_root = Path(args.input).expanduser()
    if not src_root.exists():
        die(f"input not found: {src_root}")

    preset_max_size, preset_max_height, preset_note = PLATFORM_PRESETS.get(args.for_, (None, None, None))
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

        ok = failed = skipped = 0
        for i, src in enumerate(videos, 1):
            status(f"\n[{i}/{len(videos)}] {src.name}")
            try:
                process_one(src, args, preset_max_height, effective_max_size, None, batch=True)
                ok += 1
            except SkipExisting as e:
                status(f"  skip   : {e}")
                skipped += 1
            except ProcessError as e:
                status(f"  error  : {e}")
                failed += 1

        status(f"\nbatch done: {ok} converted, {skipped} skipped, {failed} failed")
        return EXIT_OK if failed == 0 else EXIT_FFMPEG

    if preset_note:
        status(f"  preset : --for {args.for_}  ({preset_note})")

    try:
        process_one(src_root, args, preset_max_height, effective_max_size, args.output, batch=False)
    except ProcessError as e:
        die(str(e), e.code)
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nvresize: interrupted", file=sys.stderr)
        sys.exit(130)
