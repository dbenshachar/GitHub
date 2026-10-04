import unittest

from remote_desktop.cli import _parse_session_id
from remote_desktop.tcp_router import _parse_new_args


class CliTests(unittest.TestCase):
    def test_parse_session_id(self):
        response = b"SESSION abc123\nrd> "

        self.assertEqual(_parse_session_id(response), "abc123")

    def test_parse_session_id_missing(self):
        self.assertIsNone(_parse_session_id(b"ERR nope\nrd> "))

    def test_parse_new_args_with_profile(self):
        self.assertEqual(_parse_new_args("dev profile=large"), ("dev", "large"))


if __name__ == "__main__":
    unittest.main()
