"""Split tracks are tagged by rule; the model is not asked.

Lidarr rewrites every imported file's tags from MusicBrainz (writeAudioTags
sync, scrub on): the model's re-casing never outlived the import, and of 8
runs (9-78 s each on the 3090) 5 answered nothing usable."""
import inspect
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ollama_client  # noqa: E402
import tagger  # noqa: E402
from orchestrator import Orchestrator  # noqa: E402


class TagClean(unittest.TestCase):

    def test_rip_junk_goes_and_nothing_else_changes(self):
        c = tagger.clean_tag
        self.assertEqual(c("hello world [320 kbps]"), "hello world")
        self.assertEqual(c("Tapestry (FLAC)"), "Tapestry")
        self.assertEqual(c("Disc Set (2CD)  Edition"), "Disc Set Edition")
        self.assertEqual(c("Back & Forth (Mr. Lee & R. Kelly's remix)"),
                         "Back & Forth (Mr. Lee & R. Kelly's remix)")
        self.assertEqual(c("[FLAC]"), "[FLAC]")          # never emptied
        self.assertEqual(c(""), "")

    def test_no_model_in_tagging(self):
        self.assertNotIn("ollama", inspect.signature(tagger.tag_splits).parameters)
        self.assertFalse(hasattr(ollama_client.OllamaClient, "normalize_tags"))
        self.assertIn("tag_splits(cue, splits)", inspect.getsource(Orchestrator))


if __name__ == "__main__":
    unittest.main()
