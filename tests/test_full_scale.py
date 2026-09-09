import unittest
from unittest.mock import Mock

from apps.PACE5000.pace5000_backend import Pace5000Backend


class ControlFullScaleTests(unittest.TestCase):
    def setUp(self):
        self.backend = Pace5000Backend()
        self.backend.connected = True
        self.backend.write = Mock()

    def test_uses_positive_full_query_value(self):
        self.backend.query = Mock(return_value="7.0")
        self.backend._query_line = Mock()

        self.assertEqual(self.backend.get_control_full_scale_mpa(), 7.0)
        self.backend._query_line.assert_not_called()

    def test_falls_back_to_upper_limit_when_full_query_is_negative(self):
        self.backend.query = Mock(return_value="-0.1")
        self.backend._query_line = Mock(
            return_value=':INST:LIM "7.00 barg", 7.0, -0.1'
        )

        self.assertEqual(self.backend.get_control_full_scale_mpa(), 7.0)
        self.backend._query_line.assert_called_once_with(":INST:LIM1?")

    def test_rejects_non_positive_fallback(self):
        self.backend.query = Mock(return_value="-0.1")
        self.backend._query_line = Mock(
            return_value=':INST:LIM "vacuum", -0.1, -1.0'
        )

        self.assertIsNone(self.backend.get_control_full_scale_mpa())


if __name__ == "__main__":
    unittest.main()
