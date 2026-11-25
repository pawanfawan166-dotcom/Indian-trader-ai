import unittest

import timemachine


class MonteCarloDistributionTests(unittest.TestCase):
    def test_replay_sessions_accept_complete_horizon_with_trailing_provider_gap(self):
        session = {
            slot: {"o": 100, "h": 101, "l": 99, "c": 100}
            for slot in range(73)
        }

        sessions, excluded = timemachine.replay_sessions(
            {"2026-11-24": session}, "2026-11-25", last_slot=72,
        )

        self.assertEqual(len(sessions), 1)
        self.assertEqual(len(sessions[0][1]), 73)
        self.assertEqual(excluded, [])

    def test_replay_sessions_do_not_fill_missing_bars_inside_the_horizon(self):
        session = {
            slot: {"o": 100, "h": 101, "l": 99, "c": 100}
            for slot in range(73)
        }
        del session[12]

        sessions, excluded = timemachine.replay_sessions(
            {"2026-11-24": session}, "2026-11-25", last_slot=72,
        )

        self.assertEqual(sessions, [])
        self.assertEqual(excluded[0]["missing_through_until"], [12])

    def test_realized_r_distribution_accounts_for_every_replay(self):
        bar = (1.0, 1.01, 0.98, 1.005)
        pool = [[bar] * 40 for _ in range(80)]

        result = timemachine.simulate(
            pool,
            entry=100,
            stop=99,
            target=102,
            k=0,
            last=10,
            replays=50,
            seed=19,
            window=0,
        )

        self.assertEqual(sum(result["hist_R"]["bins"]), 50)
        self.assertEqual(result["hist_R"]["lo"], -1.5)
        self.assertEqual(result["hist_R"]["hi"], -0.5)
        self.assertEqual(max(result["hist_R"]["bins"]), 50)


if __name__ == "__main__":
    unittest.main()
