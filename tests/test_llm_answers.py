"""The model's answers outlive a restart.

The answer cache lived in memory only, and the container is recreated on
every deploy: 324 pick_owned_album asks in four days were 132 questions."""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ollama_client as oc  # noqa: E402
from tests.test_llm import Resp, client, verdict  # noqa: E402

Q = ("Blackstar (2016)", ["Blackstar", "Heathen"])


class AnswerFile(unittest.TestCase):

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.path = os.path.join(d.name, "llm_answers.json")

    def test_an_answer_is_not_asked_again_after_a_restart(self):
        c = client(verdict(resident=True), [Resp(200, {"response": "Blackstar"})])
        self.assertEqual(c.use_answer_file(self.path), 0)
        self.assertEqual(c.pick_owned_album(*Q), "Blackstar")
        again = client(verdict(False), [])              # restarted, GPU busy
        self.assertEqual(again.use_answer_file(self.path), 1)
        self.assertEqual(again.pick_owned_album(*Q), "Blackstar")
        self.assertEqual(again.session.sent, [])

    def test_unavailable_is_never_kept(self):
        c = client(verdict(False), [])
        c.use_answer_file(self.path)
        self.assertIs(c.pick_owned_album(*Q), oc.UNAVAILABLE)
        self.assertFalse(os.path.exists(self.path))

    def test_another_model_or_an_old_answer_is_not_loaded(self):
        row = ["owned", "blackstar (2016)", ["blackstar", "heathen"], "Blackstar"]
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump([row + ["some-other-model", time.time()],
                       row + [client(verdict(), []).model, time.time() - 40 * 86400]], fh)
        c = client(verdict(), [])
        self.assertEqual(c.use_answer_file(self.path), 0)


if __name__ == "__main__":
    unittest.main()
