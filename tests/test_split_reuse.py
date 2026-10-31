"""A CUE is split once: a retry keeps the tracks the last attempt split.

A CUE whose import failed is retried, and every retry split the whole image
again although its tracks sat in the folder from the last attempt (31 Oct:
Chambao's 'Caminando' CD1 split at 13:19 and 13:51; 1 Oct: a Christina
Aguilera disc 10 times). Needs ffmpeg and mutagen: run in the image."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import splitter  # noqa: E402
from cue_parser import Cue, Track  # noqa: E402


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg (run in the image)")
class SplitReuse(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.image = self.dir / "image.flac"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                        "-i", "sine=frequency=440:sample_rate=44100:duration=3",
                        "-c:a", "flac", str(self.image)], check=True)
        self.cue = Cue(performer="A", title="B", audio_files=["image.flac"], tracks=[
            Track(number=1, title="One", start_seconds=0.0, end_seconds=1.5),
            Track(number=2, title="Two", start_seconds=1.5, end_seconds=None),
        ])
        self.out = self.dir / "B"

    def tearDown(self):
        self.tmp.cleanup()

    def split(self, reuse=True):
        real = subprocess.run
        encodes = []

        def run(cmd, *a, **kw):
            if "-c:a" in cmd:
                encodes.append(Path(cmd[-1]).name)
            return real(cmd, *a, **kw)
        with mock.patch.object(splitter.subprocess, "run", side_effect=run):
            res = splitter.split_cue(self.cue, self.image, self.out,
                                     reuse_existing=reuse)
        return res, encodes

    def test_a_retry_encodes_nothing_and_returns_the_same_tracks(self):
        first, enc1 = self.split()
        self.assertEqual(len(enc1), 2)
        again, enc2 = self.split()
        self.assertEqual(enc2, [])
        self.assertEqual([r.output_path for r in again],
                         [r.output_path for r in first])

    def test_a_track_ffmpeg_did_not_finish_is_encoded_again_alone(self):
        # ffmpeg writes the sample count into STREAMINFO when it finishes: a
        # file it was killed in the middle of has 0 there. Written to a pipe,
        # it never goes back to fill it in, which leaves the same header.
        first, _ = self.split()
        cut = first[1].output_path
        with open(cut, "wb") as fh:
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                            "-i", str(self.image), "-ss", "1.5", "-c:a", "flac",
                            "-f", "flac", "pipe:1"], stdout=fh, check=True)
        _, enc = self.split()
        self.assertEqual(enc, [cut.name])

    def test_an_empty_or_broken_track_is_encoded_again(self):
        first, _ = self.split()
        first[0].output_path.write_bytes(b"")
        first[1].output_path.write_bytes(b"fLaC")
        _, enc = self.split()
        self.assertEqual(len(enc), 2)

    def test_a_track_of_another_length_is_encoded_again(self):
        first, _ = self.split()
        self.cue.tracks[0].end_seconds = 1.0
        self.cue.tracks[1].start_seconds = 1.0
        _, enc = self.split()
        self.assertEqual(enc, [first[0].output_path.name, first[1].output_path.name])

    def test_without_reuse_every_track_is_encoded(self):
        self.split()
        _, enc = self.split(reuse=False)
        self.assertEqual(len(enc), 2)


if __name__ == "__main__":
    unittest.main()
