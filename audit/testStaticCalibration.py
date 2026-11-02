import sys
import unittest
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'pipeline/05_analysis'))
from calibrateStatic import fitCalibration


class CalibrationTests(unittest.TestCase):
    def test_known_scale_rotation_and_bias_recovered(self):
        expected=np.vstack([np.eye(3),-np.eye(3)])
        transform=np.array([[0,1.2,0],[-.9,0,0],[0,0,1.1]])
        bias=np.array([.1,-.2,.05])
        raw=expected@transform+bias
        fit=fitCalibration(raw,expected)
        # Evaluate a new orientation not used in fitting.
        new=np.array([1,1,1])/np.sqrt(3)
        corrected=(new@transform+bias)@np.array(fit['matrixRawToG'])+fit['offsetG']
        np.testing.assert_allclose(corrected,new,atol=1e-12)

    def test_one_stationary_orientation_cannot_calibrate_three_axes(self):
        with self.assertRaises(ValueError):fitCalibration(np.ones((6,3)),np.tile([0,0,1],(6,1)))

    def test_unresponsive_sensor_is_rejected(self):
        with self.assertRaises(ValueError):fitCalibration(np.ones((6,3)),np.vstack([np.eye(3),-np.eye(3)]))


if __name__=='__main__':unittest.main()
