"""Regression checks for invalid IMU input; run from the repository root."""
import sys
import struct
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline/02_extract'))
import decoders


class InertialInputTests(unittest.TestCase):
    def test_valid_zero_negative_and_exponent_are_preserved(self):
        self.assertEqual(decoders.parseTriplets('0 -2.5 1e-3\n4 5 6'),
                         [(0.0, -2.5, 0.001), (4.0, 5.0, 6.0)])

    def test_invalid_component_cannot_become_a_zero_measurement(self):
        for token in ('NaN', 'Inf', '-inf', '1001', '-1001'):
            with self.subTest(token=token), self.assertRaises(ValueError):
                decoders.parseTriplets(f'1 {token} 3')

    def test_partial_triplet_cannot_be_silently_discarded(self):
        with self.assertRaises(ValueError):
            decoders.parseTriplets('1 2 3 4 5')


class RoveSpeedTests(unittest.TestCase):
    def test_speed_field_uses_two_bytes_and_knots(self):
        record = bytearray(32)
        struct.pack_into('<dd', record, 0, 4048.0, -9641.0)
        struct.pack_into('<H', record, 20, 300)
        record[22:28] = bytes([26, 7, 13, 10, 28, 0])
        decoded = decoders.parseRoveGpsRecord(record)
        self.assertEqual(decoded['speed_knots'], 300)
        self.assertAlmostEqual(decoded['speed_mph'], 300 * 1852 / 1609.344)


class RedtigerSpeedTests(unittest.TestCase):
    def test_raw_speed_is_exposed_as_knots(self):
        chunk = bytearray(256)
        chunk[4:11] = b'freeGPS'
        struct.pack_into('<f', chunk, 96, 20)
        decoded = decoders.parseRedtigerChunk(chunk)
        self.assertEqual(decoded['speed_knots'], 20)
        self.assertNotIn('speed_mph', decoded)


if __name__ == '__main__':
    unittest.main()
