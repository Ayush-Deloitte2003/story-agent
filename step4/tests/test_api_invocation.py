import io
import unittest
import urllib.error
from unittest.mock import patch

from app.api import _invoke


class ApiInvocationTests(unittest.TestCase):
    def test_retries_transient_dapr_service_discovery_failure(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"raised":true}'

        unavailable = urllib.error.HTTPError(
            "http://127.0.0.1:3500", 503, "Unavailable", {}, io.BytesIO(b"")
        )
        with (
            patch("app.api.urllib.request.urlopen", side_effect=[unavailable, Response()]) as urlopen,
            patch("app.api.time.sleep") as sleep,
        ):
            self.assertEqual(_invoke("booking-worker", "internal/approval/raise"), {"raised": True})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(0.5)


if __name__ == "__main__":
    unittest.main()
