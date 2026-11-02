import sys
import unittest
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline/02_extract'))
from sensorTimeline import TimedSignal, blockSignal, interpolateClock


class TimingTests(unittest.TestCase):
    def test_irregular_linear_signal_integrates_exactly(self):
        t = np.array([0, .05, .7, 1, 1.1, 2])
        signal = TimedSignal(t, np.column_stack([2*t+1, -t, np.ones(len(t))*7]), 1)
        mean, coverage, counts = signal.averages([.2], [1.8])
        np.testing.assert_allclose(mean, [[3, -1, 7]], atol=1e-12)
        np.testing.assert_allclose(coverage, [1])
        self.assertEqual(counts[0], 3)

    def test_gap_is_missing_not_interpolated_or_zero(self):
        signal = TimedSignal([0, .1, 1, 1.1], [[2],[2],[2],[2]], .2)
        mean, coverage, _ = signal.averages([0], [1.1])
        self.assertTrue(np.isnan(mean).all())
        self.assertAlmostEqual(coverage[0], .2/1.1)

    def test_no_extrapolated_values_outside_observed_range(self):
        signal = TimedSignal([0,1,2], [[1],[1],[1]], 1)
        mean, coverage, _ = signal.averages([-1,0], [1,2])
        self.assertTrue(np.isnan(mean[0]).all())
        self.assertAlmostEqual(coverage[0], .5)
        self.assertAlmostEqual(mean[1,0], 1)

    def test_clock_mapping_and_restricted_extrapolation(self):
        np.testing.assert_allclose(interpolateClock([0,1,2], [0,2], [10,14]), [10,12,14])
        with self.assertRaises(ValueError):
            interpolateClock([5], [0,2], [10,14])

    def test_counter_discontinuity_stops_processing(self):
        blocks=[{'stamp':0,'counter':2,'values':np.ones((2,3))},
                {'stamp':1,'counter':5,'values':np.ones((2,3))}]
        with self.assertRaises(ValueError):blockSignal(blocks)

    def test_unequal_block_counts_use_timestamps(self):
        blocks=[{'stamp':0,'counter':2,'values':np.ones((2,3))},
                {'stamp':.02,'counter':5,'values':np.ones((3,3))*2},
                {'stamp':.05,'counter':7,'values':np.ones((2,3))*3}]
        signal, terminal, summary=blockSignal(blocks)
        np.testing.assert_allclose(signal.times,[0,.01,.02,.03,.04,.05,.06])
        self.assertEqual(terminal,.05)

    def test_duplicate_times_fail(self):
        with self.assertRaises(ValueError):TimedSignal([0,0,1],[[1],[1],[1]],1)


if __name__=='__main__':unittest.main()
