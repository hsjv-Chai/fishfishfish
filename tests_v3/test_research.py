import unittest

from fishbot_v3.config import V3Config
from fishbot_v3.research import COSTS, FOLD_STARTS, GRID


class ResearchPlanTests(unittest.TestCase):
    def test_grid_is_pre_registered_and_unique(self):
        self.assertEqual(len(GRID), 16)
        self.assertEqual(len({cfg.key for cfg in GRID}), 16)
        self.assertEqual({cfg.breakout for cfg in GRID}, {20, 55})
        self.assertEqual({cfg.risk for cfg in GRID}, {.0025, .0035})
        self.assertEqual({cfg.time_stop_hours for cfg in GRID}, {0, 120})

    def test_fold_schedule_covers_four_quarters(self):
        self.assertEqual(FOLD_STARTS, ("2024-09", "2024-12", "2025-03", "2025-06"))
        self.assertEqual(COSTS, (0, .0005, .001, .002))

    def test_selected_policy_is_one_of_the_grid(self):
        chosen = V3Config(breakout=55, trail_atr=3, risk=.0025, time_stop_hours=120)
        self.assertIn(chosen.key, {cfg.key for cfg in GRID})


if __name__ == "__main__":
    unittest.main()
