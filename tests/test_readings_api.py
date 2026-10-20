import json
import unittest
from unittest.mock import Mock
from urllib.request import urlopen

from apps.PACE5000.pace5000_api import Pace5000ApiServer
from apps.PACE5000.pace5000_backend import Pace5000Backend
from apps.PACE5000.pace5000_readings_api import Pace5000ReadingsServer


class CachedReadingsTests(unittest.TestCase):
    def test_both_http_endpoints_use_poll_cache_without_instrument_query(self):
        backend = Pace5000Backend()
        backend.connected = True
        backend._active_pressure_unit = "Bar"
        backend.drain_system_errors = Mock()
        backend.get_pressure = Mock(return_value=12.0)
        backend.get_positive_source_pressure = Mock(return_value=None)
        backend.get_negative_source_pressure = Mock(return_value=None)
        backend.get_effort = Mock(return_value=None)
        backend.get_target_pressure = Mock(return_value="15.0")
        backend.get_slew_rate = Mock(return_value="0.2")
        backend.poll_pressure()

        backend.get_pressure = Mock(side_effect=AssertionError("device query from HTTP"))
        backend.get_target_pressure = Mock(side_effect=AssertionError("device query from HTTP"))
        for server_type in (Pace5000ReadingsServer, Pace5000ApiServer):
            with self.subTest(server=server_type.__name__):
                server = server_type(backend, port=0)
                server.start()
                try:
                    url = f"http://127.0.0.1:{server._server.server_port}/api/v1/readings"
                    with urlopen(url, timeout=2) as response:
                        data = json.load(response)
                    self.assertTrue(data["connected"])
                    self.assertAlmostEqual(data["pressure_mpa"], 1.2)
                    self.assertAlmostEqual(data["target_pressure_mpa"], 1.5)
                    self.assertIsInstance(data["timestamp"], float)
                finally:
                    server.stop()

        backend.disconnect_device()
        self.assertIsNone(backend.get_cached_readings()["pressure_mpa"])


if __name__ == "__main__":
    unittest.main()
