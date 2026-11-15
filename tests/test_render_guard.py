# -*- coding: utf-8 -*-
"""When Align Edge must wait, and when it may cancel one stuck redraw."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from render_guard import (  # noqa: E402
    MAX_RENDER_RECOVERIES,
    STUCK_RENDER_SECONDS,
    should_cancel_stuck_draw,
    should_defer_layer_work,
)


class RenderGuardTests(unittest.TestCase):
    def test_defer_while_anything_owns_the_layers(self):
        self.assertFalse(should_defer_layer_work(False, False, False))
        self.assertTrue(should_defer_layer_work(True, False, False))
        self.assertTrue(should_defer_layer_work(False, True, False))
        self.assertTrue(should_defer_layer_work(False, False, True))

    def test_cancel_only_a_draw_that_stayed_busy(self):
        limit = STUCK_RENDER_SECONDS
        self.assertFalse(should_cancel_stuck_draw(limit - 0.1, True, 0, False))
        self.assertTrue(should_cancel_stuck_draw(limit, True, 0, False))
        self.assertFalse(should_cancel_stuck_draw(limit + 5, False, 0, False))
        self.assertFalse(should_cancel_stuck_draw(limit + 5, True, 0, True))
        self.assertTrue(should_cancel_stuck_draw(limit + 5, True, 1, False))
        self.assertFalse(
            should_cancel_stuck_draw(limit + 5, True, MAX_RENDER_RECOVERIES, False))


if __name__ == "__main__":
    unittest.main()
