"""Unit tests for vresize. No ffmpeg required - subprocess calls aren't exercised.

Run with: python -m unittest discover -s tests
"""
from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import vresize as vr  # noqa: E402


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
        ns = argparse.Namespace(
            codec="h264", crf=23, preset="medium", audio="copy", fps=None,
        )
        for k, v in overrides.items():
            setattr(ns, k, v)
        return ns

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


class ComputeBitrateTests(unittest.TestCase):
    def test_basic(self):
        # 25MB over 60s, no audio: ~ (25*1024*1024*8*0.98) / 60 / 1000 kbps
        kbps = vr.compute_bitrate_kbps(25 * 1024 ** 2, 60.0, audio_bps=0)
        self.assertGreater(kbps, 3000)
        self.assertLess(kbps, 3500)

    def test_too_small_raises(self):
        with self.assertRaises(vr.ProcessError):
            vr.compute_bitrate_kbps(10_000, 3600.0, audio_bps=128_000)

    def test_unknown_duration_raises(self):
        with self.assertRaises(vr.ProcessError):
            vr.compute_bitrate_kbps(25 * 1024 ** 2, None, audio_bps=0)


class FindVideosTests(unittest.TestCase):
    def test_filters_by_extension(self, tmp_path=None):
        import tempfile
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


if __name__ == "__main__":
    unittest.main()
