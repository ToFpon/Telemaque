"""Mise en veille empêchée seulement pendant une vraie activité (choix de Tof, novembre 2026)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from telemaque.app import INHIBIT_GRACE, keeps_awake  # noqa: E402
from telemaque.backend.engine import Transfer  # noqa: E402


def t(state, rate=0):
    return Transfer(tid="x", name="x", state=state, down_rate=rate)


class KeepsAwakeTest(unittest.TestCase):
    def test_dead_magnet_never_blocks(self):
        """Avant : un lien magnet sans source (ou un téléchargement sans pair) bloquait la veille pour toujours."""
        self.assertEqual(keeps_awake([t("metadata")], 0.0, 1000.0), (False, 0.0))
        self.assertFalse(keeps_awake([t("downloading")], 0.0, 1000.0)[0])

    def test_data_arriving_then_grace(self):
        busy, last = keeps_awake([t("downloading", 50_000)], 0.0, 1000.0)
        self.assertTrue(busy)
        self.assertEqual(last, 1000.0)
        self.assertTrue(keeps_awake([t("downloading")], last, 1000.0 + INHIBIT_GRACE - 1)[0])   # débit retombé
        self.assertFalse(keeps_awake([t("downloading")], last, 1000.0 + INHIBIT_GRACE + 1)[0])

    def test_checking_and_moving_count(self):
        self.assertTrue(keeps_awake([t("checking")], 0.0, 5.0)[0])
        self.assertTrue(keeps_awake([t("moving")], 0.0, 5.0)[0])

    def test_seeding_or_finished_never_blocks(self):
        busy, _ = keeps_awake([t("downloading", 50_000)], 0.0, 1000.0)
        self.assertFalse(keeps_awake([t("seeding"), t("done"), t("paused")], 1000.0, 1001.0)[0])


if __name__ == "__main__":
    unittest.main()
