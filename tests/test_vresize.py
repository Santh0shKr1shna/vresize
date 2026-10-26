"""Unit tests for vresize.

Most tests need no ffmpeg - they exercise pure parsing/naming/command-building
logic. A few integration tests under PublicApiIntegrationTests do a real
encode and are skipped automatically when ffmpeg isn't on PATH.

Run with: python -m unittest discover -s tests
"""
from __future__ import annotations

import argparse
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import vresize as vr  # noqa: E402

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


class ParseTargetTests(unittest.TestCase):
    def test_bare_height(self):
        self.assertEqual(vr.parse_target("720"), 720)

    def test_p_suffix(self):
        self.assertEqual(vr.parse_target("720p"), 720)
        self.assertEqual(vr.parse_target("1080P"), 1080)

    def test_aliases(self):
        self.assertEqual(vr.parse_target("4k"), 2160)
        self.assertEqual(vr.parse_target("hd"), 720)
        self.assertEqual(vr.parse_target("SD"), 480)

    def test_invalid(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            vr.parse_target("banana")


class ParseSizeTests(unittest.TestCase):
    def test_mb_default_unit(self):
        self.assertEqual(vr.parse_size("25"), 25 * 1024 ** 2)

    def test_units(self):
        self.assertEqual(vr.parse_size("700KB"), 700 * 1024)
        self.assertEqual(vr.parse_size("1.5GB"), int(1.5 * 1024 ** 3))
        self.assertEqual(vr.parse_size("100B"), 100)

    def test_invalid(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            vr.parse_size("big")

    def test_non_positive(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            vr.parse_size("0MB")


class HumanSizeTagTests(unittest.TestCase):
    def test_mb(self):
        self.assertEqual(vr.human_size_tag(25 * 1024 ** 2), "25MB")

    def test_fractional(self):
        self.assertEqual(vr.human_size_tag(int(1.5 * 1024 ** 3)), "1.5GB")

    def test_bytes_fallback(self):
        self.assertEqual(vr.human_size_tag(500), "500B")


class ResolveHeightTests(unittest.TestCase):
    def test_explicit_wins(self):
        self.assertEqual(vr.resolve_height(480, 720, 1080), 480)

    def test_preset_applies_when_source_bigger(self):
        self.assertEqual(vr.resolve_height(None, 720, 1080), 720)

    def test_preset_skipped_when_source_smaller(self):
        self.assertIsNone(vr.resolve_height(None, 1080, 720))

    def test_no_target_at_all(self):
        self.assertIsNone(vr.resolve_height(None, None, 1080))


class DeriveOutputPathTests(unittest.TestCase):
    def test_explicit_overrides_everything(self):
        src = Path("Movie_1080p.mkv")
        self.assertEqual(
            vr.derive_output_path(src, "480p", "/exports/out.mkv", True),
            Path("/exports/out.mkv"),
        )

    def test_replaces_existing_resolution_token(self):
        src = Path("Movie.1080p.mkv")
        self.assertEqual(
            vr.derive_output_path(src, "480p", None, True),
            Path("Movie.480p.mkv"),
        )

    def test_appends_when_no_token(self):
        src = Path("Movie.mkv")
        self.assertEqual(
            vr.derive_output_path(src, "480p", None, True),
            Path("Movie_480p.mkv"),
        )

    def test_size_tag_does_not_replace_resolution_token(self):
        src = Path("Movie.1080p.mkv")
        self.assertEqual(
            vr.derive_output_path(src, "25MB", None, False),
            Path("Movie.1080p_25MB.mkv"),
        )


class MakeTagTests(unittest.TestCase):
    def test_height_wins(self):
        self.assertEqual(vr.make_tag(720, 25 * 1024 ** 2), "720p")

    def test_size_when_no_height(self):
        self.assertEqual(vr.make_tag(None, 25 * 1024 ** 2), "25MB")

    def test_fallback(self):
        self.assertEqual(vr.make_tag(None, None), "out")


class BuildCmdTests(unittest.TestCase):
    def _args(self, **overrides):
        defaults = dict(codec="h264", crf=23, preset="medium", audio="copy", fps=None)
        defaults.update(overrides)
        return vr.EncodeOptions(**defaults)

    def test_basic_shape(self):
        cmd = vr.build_cmd(Path("in.mp4"), Path("out.mp4"), 720, self._args(), has_audio=True)
        self.assertEqual(cmd[0], "ffmpeg")
        self.assertIn("-vf", cmd)
        self.assertEqual(cmd[cmd.index("-vf") + 1], "scale=-2:720")
        self.assertIn("libx264", cmd)
        self.assertIn("-crf", cmd)
        self.assertEqual(cmd[cmd.index("-crf") + 1], "23")
        self.assertIn("-c:a", cmd)
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "copy")
        self.assertEqual(cmd[-1], "out.mp4")

    def test_no_height_omits_scale_filter(self):
        cmd = vr.build_cmd(Path("in.mp4"), Path("out.mp4"), None, self._args(), has_audio=True)
        self.assertNotIn("-vf", cmd)

    def test_h265_codec(self):
        cmd = vr.build_cmd(Path("in.mp4"), Path("out.mp4"), 720, self._args(codec="h265"), has_audio=True)
        self.assertIn("libx265", cmd)

    def test_no_audio_source_forces_an(self):
        cmd = vr.build_cmd(Path("in.mp4"), Path("out.mp4"), 720, self._args(), has_audio=False)
        self.assertIn("-an", cmd)

    def test_faststart_for_mp4(self):
        cmd = vr.build_cmd(Path("in.mp4"), Path("out.mp4"), 720, self._args(), has_audio=True)
        self.assertIn("+faststart", cmd)

    def test_no_faststart_for_mkv(self):
        cmd = vr.build_cmd(Path("in.mkv"), Path("out.mkv"), 720, self._args(), has_audio=True)
        self.assertNotIn("+faststart", cmd)

    def test_threads_passed_to_cpu_encode(self):
        cmd = vr.build_cmd(Path("in.mp4"), Path("out.mp4"), 720, self._args(threads=2), has_audio=True)
        self.assertEqual(cmd[cmd.index("-threads") + 1], "2")

    def test_no_threads_flag_by_default(self):
        cmd = vr.build_cmd(Path("in.mp4"), Path("out.mp4"), 720, self._args(), has_audio=True)
        self.assertNotIn("-threads", cmd)

    def test_threads_ignored_on_gpu(self):
        cmd = vr.build_cmd(Path("in.mp4"), Path("out.mp4"), 720, self._args(threads=2), True, "nvidia")
        self.assertNotIn("-threads", cmd)


class GpuEncoderArgsTests(unittest.TestCase):
    def _flag(self, args, name):
        return args[args.index(name) + 1]

    def test_cpu_unchanged(self):
        args = vr.video_encoder_args(vr.EncodeOptions(), None)
        self.assertEqual(args, ["-c:v", "libx264", "-crf", "23", "-preset", "medium", "-pix_fmt", "yuv420p"])

    def test_nvidia(self):
        args = vr.video_encoder_args(vr.EncodeOptions(codec="h265", preset="slow"), "nvidia")
        self.assertEqual(self._flag(args, "-c:v"), "hevc_nvenc")
        self.assertEqual(self._flag(args, "-preset"), "p5")
        self.assertEqual(self._flag(args, "-cq"), "28")
        self.assertEqual(self._flag(args, "-b:v"), "0")

    def test_intel(self):
        args = vr.video_encoder_args(vr.EncodeOptions(preset="ultrafast"), "intel")
        self.assertEqual(self._flag(args, "-c:v"), "h264_qsv")
        self.assertEqual(self._flag(args, "-preset"), "veryfast")
        self.assertEqual(self._flag(args, "-global_quality"), "23")
        self.assertEqual(self._flag(args, "-pix_fmt"), "nv12")

    def test_amd(self):
        args = vr.video_encoder_args(vr.EncodeOptions(), "amd")
        self.assertEqual(self._flag(args, "-c:v"), "h264_amf")
        self.assertEqual(self._flag(args, "-quality"), "balanced")
        self.assertEqual(self._flag(args, "-qp_p"), "23")

    def test_apple_quality_scale_inverted(self):
        args = vr.video_encoder_args(vr.EncodeOptions(), "apple")
        self.assertEqual(self._flag(args, "-c:v"), "h264_videotoolbox")
        self.assertEqual(self._flag(args, "-q:v"), "66")

    def test_crf_zero_clamped_for_gpu(self):
        args = vr.video_encoder_args(vr.EncodeOptions(crf=0), "nvidia")
        self.assertEqual(self._flag(args, "-cq"), "1")

    def test_every_preset_maps(self):
        for p in vr.X264_PRESETS:
            for vendor in vr.GPU_ENCODERS:
                vr.video_encoder_args(vr.EncodeOptions(preset=p), vendor)

    def test_build_cmd_uses_gpu_encoder(self):
        cmd = vr.build_cmd(Path("in.mp4"), Path("out.mp4"), 720, vr.EncodeOptions(), True, "nvidia")
        self.assertIn("h264_nvenc", cmd)
        self.assertNotIn("-crf", cmd)

    def test_bad_gpu_value_raises(self):
        with self.assertRaises(ValueError):
            vr.EncodeOptions(gpu="radeon")

    def test_negative_threads_raises(self):
        with self.assertRaises(ValueError):
            vr.EncodeOptions(threads=-1)

    def test_zero_threads_allowed(self):
        self.assertEqual(vr.EncodeOptions(threads=0).threads, 0)


class ResolveGpuTests(unittest.TestCase):
    """probe_gpu is swapped for a fake so these run without any GPU."""

    def setUp(self):
        self._real = vr.probe_gpu
        self.working = set()
        vr.probe_gpu = lambda args: None if args[1] in self.working else "no device"

    def tearDown(self):
        vr.probe_gpu = self._real

    def test_off_is_cpu(self):
        self.assertIsNone(vr.resolve_gpu(vr.EncodeOptions(gpu="off"), size_mode=False))

    def test_explicit_vendor_works(self):
        self.working = {"h264_qsv"}
        self.assertEqual(vr.resolve_gpu(vr.EncodeOptions(gpu="intel"), size_mode=False), "intel")

    @unittest.skipIf(sys.platform == "darwin", "auto only tries videotoolbox on macOS")
    def test_auto_skips_broken_vendor(self):
        self.working = {"h264_qsv"}  # nvidia probed first and fails
        self.assertEqual(vr.resolve_gpu(vr.EncodeOptions(gpu="auto"), size_mode=False), "intel")

    def test_size_mode_forces_cpu(self):
        self.working = {"h264_nvenc"}
        self.assertIsNone(vr.resolve_gpu(vr.EncodeOptions(gpu="nvidia"), size_mode=True))

    def test_failure_without_fallback_raises(self):
        with self.assertRaises(vr.ResizeError) as cm:
            vr.resolve_gpu(vr.EncodeOptions(gpu="nvidia"), size_mode=False)
        self.assertIn("no device", str(cm.exception))

    def test_failure_with_fallback_yes_is_cpu(self):
        asked = []
        opts = vr.EncodeOptions(gpu="nvidia", cpu_fallback=lambda r: asked.append(r) or True)
        self.assertIsNone(vr.resolve_gpu(opts, size_mode=False))
        self.assertEqual(len(asked), 1)

    def test_failure_with_fallback_no_raises(self):
        opts = vr.EncodeOptions(gpu="nvidia", cpu_fallback=lambda r: False)
        with self.assertRaises(vr.ResizeError):
            vr.resolve_gpu(opts, size_mode=False)


class BuildPassCmdTests(unittest.TestCase):
    def test_threads_passed_through_on_pass2(self):
        cmd = vr.build_pass_cmd(Path("in.mp4"), Path("out.mp4"), 720, "libx264", "medium",
                                 2000.0, 2, "passlog", None, "aac", True, True, threads=2)
        self.assertEqual(cmd[cmd.index("-threads") + 1], "2")

    def test_no_threads_flag_by_default(self):
        cmd = vr.build_pass_cmd(Path("in.mp4"), Path("out.mp4"), 720, "libx264", "medium",
                                 2000.0, 1, "passlog", None, "aac", True, True)
        self.assertNotIn("-threads", cmd)


class ComputeBitrateTests(unittest.TestCase):
    def test_basic(self):
        # 25MB over 60s, no audio: ~ (25*1024*1024*8*0.98) / 60 / 1000 kbps
        kbps = vr.compute_bitrate_kbps(25 * 1024 ** 2, 60.0, audio_bps=0)
        self.assertGreater(kbps, 3000)
        self.assertLess(kbps, 3500)

    def test_too_small_raises(self):
        with self.assertRaises(vr.ResizeError):
            vr.compute_bitrate_kbps(10_000, 3600.0, audio_bps=128_000)

    def test_unknown_duration_raises(self):
        with self.assertRaises(vr.ResizeError):
            vr.compute_bitrate_kbps(25 * 1024 ** 2, None, audio_bps=0)


class FindVideosTests(unittest.TestCase):
    def test_filters_by_extension(self, tmp_path=None):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a.mp4").touch()
            (root / "b.txt").touch()
            (root / "sub").mkdir()
            (root / "sub" / "c.mkv").touch()

            flat = vr.find_videos(root, recursive=False)
            self.assertEqual([p.name for p in flat], ["a.mp4"])

            deep = vr.find_videos(root, recursive=True)
            self.assertEqual(sorted(p.name for p in deep), ["a.mp4", "c.mkv"])


class EncodeOptionsTests(unittest.TestCase):
    def test_default_crf_h264(self):
        self.assertEqual(vr.EncodeOptions(codec="h264").crf, 23)

    def test_default_crf_h265(self):
        self.assertEqual(vr.EncodeOptions(codec="h265").crf, 28)

    def test_explicit_crf_kept(self):
        self.assertEqual(vr.EncodeOptions(crf=30).crf, 30)

    def test_bad_crf_raises(self):
        with self.assertRaises(ValueError):
            vr.EncodeOptions(crf=100)

    def test_bad_preset_raises(self):
        with self.assertRaises(ValueError):
            vr.EncodeOptions(preset="turbo")


class CoerceHelpersTests(unittest.TestCase):
    def test_coerce_target_passthrough_int(self):
        self.assertEqual(vr._coerce_target(720), 720)

    def test_coerce_target_none(self):
        self.assertIsNone(vr._coerce_target(None))

    def test_coerce_target_parses_string(self):
        self.assertEqual(vr._coerce_target("4k"), 2160)

    def test_coerce_target_bad_string_raises_value_error(self):
        with self.assertRaises(ValueError):
            vr._coerce_target("banana")

    def test_coerce_size_passthrough_int(self):
        self.assertEqual(vr._coerce_size(1000), 1000)

    def test_coerce_size_parses_string(self):
        self.assertEqual(vr._coerce_size("25MB"), 25 * 1024 ** 2)

    def test_coerce_size_bad_string_raises_value_error(self):
        with self.assertRaises(ValueError):
            vr._coerce_size("not a size")


class PresetLookupTests(unittest.TestCase):
    def test_none_platform(self):
        self.assertEqual(vr._preset_lookup(None), (None, None, None))

    def test_known_platform(self):
        size, height, note = vr._preset_lookup("discord")
        self.assertEqual(size, 25 * 1024 ** 2)
        self.assertIsNone(height)
        self.assertTrue(note)

    def test_unknown_platform_raises(self):
        with self.assertRaises(ValueError):
            vr._preset_lookup("myspace")


class ResizeArgValidationTests(unittest.TestCase):
    """resize()/resize_directory() validate arguments before touching ffmpeg
    or the filesystem, so these need neither ffmpeg nor a real input."""

    def test_resize_requires_a_target(self):
        with self.assertRaises(ValueError):
            vr.resize("nonexistent.mp4")

    def test_resize_directory_requires_a_target(self):
        with self.assertRaises(ValueError):
            vr.resize_directory("nonexistent_dir")

    def test_resize_rejects_unknown_platform(self):
        with self.assertRaises(ValueError):
            vr.resize("nonexistent.mp4", for_platform="myspace")

    def test_resize_rejects_bad_crf(self):
        with self.assertRaises(ValueError):
            vr.resize("nonexistent.mp4", target="720p", crf=999)


class LoggingDefaultsTests(unittest.TestCase):
    def test_status_produces_no_stderr_output_by_default(self):
        # No test in this file calls main()/_configure_cli_logging(), so the
        # module logger should still carry only its default NullHandler.
        buf = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = buf
        try:
            vr.status("this should not appear anywhere")
        finally:
            sys.stderr = old_stderr
        self.assertEqual(buf.getvalue(), "")


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg/ffprobe not on PATH")
class PublicApiIntegrationTests(unittest.TestCase):
    """Real ffmpeg encodes against a tiny synthetic clip."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="vresize-test-")
        cls.clip = Path(cls.tmpdir) / "sample_360p.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=duration=1:size=640x360:rate=15",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
                str(cls.clip),
            ],
            check=True,
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_dry_run_returns_result_without_writing(self):
        result = vr.resize(self.clip, "180p", dry_run=True)
        self.assertTrue(result.dry_run)
        self.assertIsNone(result.output_size)
        self.assertEqual(result.output_height, 180)
        self.assertFalse(result.output.exists())

    def test_real_downscale(self):
        out = Path(self.tmpdir) / "out_180p.mp4"
        result = vr.resize(self.clip, "180p", output=str(out))
        self.assertTrue(result.output.exists())
        self.assertEqual(result.output_height, 180)
        self.assertGreater(result.output_size, 0)
        out.unlink()

    def test_real_gpu_downscale(self):
        try:
            vendor = vr.resolve_gpu(vr.EncodeOptions(gpu="auto"), size_mode=False)
        except vr.ResizeError as e:
            self.skipTest(f"no working GPU encoder here: {e}")
        out = Path(self.tmpdir) / "out_gpu_180p.mp4"
        result = vr.resize(self.clip, "180p", output=str(out), gpu=vendor)
        self.assertEqual(result.output_height, 180)
        self.assertGreater(result.output_size, 0)
        out.unlink()

    def test_multi_audio_tracks_preserved(self):
        # regression: ffmpeg drops every stream but its default pick unless
        # told otherwise - build_cmd/build_pass_cmd must -map all audio.
        src = Path(self.tmpdir) / "multi_audio.mkv"
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=duration=1:size=640x360:rate=15",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
                "-map", "0:v", "-map", "1:a", "-map", "2:a",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-c:a", "aac",
                str(src),
            ],
            check=True,
        )

        def audio_stream_count(path):
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a",
                 "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
                capture_output=True, text=True, check=True,
            ).stdout
            return len([l for l in out.splitlines() if l.strip()])

        self.assertEqual(audio_stream_count(src), 2)

        out = Path(self.tmpdir) / "multi_audio_180p.mkv"
        for audio_mode in ("copy", "aac"):
            vr.resize(src, "180p", output=str(out), audio=audio_mode, overwrite=True)
            self.assertEqual(audio_stream_count(out), 2, f"audio={audio_mode}")

        vr.resize(src, output=str(out), max_size="2MB", overwrite=True)
        self.assertEqual(audio_stream_count(out), 2, "max_size two-pass")

    def test_subtitle_track_preserved(self):
        srt = Path(self.tmpdir) / "subs.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n")
        src = Path(self.tmpdir) / "with_subs.mkv"
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=duration=1:size=640x360:rate=15",
                "-i", str(srt),
                "-map", "0:v", "-map", "1:s",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-c:s", "srt",
                str(src),
            ],
            check=True,
        )

        def subtitle_stream_count(path):
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "s",
                 "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
                capture_output=True, text=True, check=True,
            ).stdout
            return len([l for l in out.splitlines() if l.strip()])

        out = Path(self.tmpdir) / "with_subs_180p.mkv"
        vr.resize(src, "180p", output=str(out))
        self.assertEqual(subtitle_stream_count(out), 1)

    def test_resize_directory_batch(self):
        batch_dir = Path(self.tmpdir) / "batch"
        batch_dir.mkdir(exist_ok=True)
        shutil.copy(self.clip, batch_dir / "a.mp4")
        shutil.copy(self.clip, batch_dir / "b.mp4")

        result = vr.resize_directory(batch_dir, "180p")
        self.assertEqual(result.ok, 2)
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.skipped, 0)

    def test_resize_directory_resume_skips_existing_output(self):
        resume_dir = Path(self.tmpdir) / "resume"
        resume_dir.mkdir(exist_ok=True)
        shutil.copy(self.clip, resume_dir / "clip.mp4")

        first = vr.resize_directory(resume_dir, "180p")
        self.assertEqual(first.ok, 1)

        second = vr.resize_directory(resume_dir, "180p")
        outcome = next(f for f in second.files if f.input.name == "clip.mp4")
        self.assertEqual(outcome.status, "skipped")


if __name__ == "__main__":
    unittest.main()
