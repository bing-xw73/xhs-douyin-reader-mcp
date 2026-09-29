import importlib
import os
import unittest
from unittest import mock


class ServerConfigTests(unittest.TestCase):
    def test_secret_required(self):
        with mock.patch.dict(os.environ, {"MCP_SECRET": "short"}, clear=False):
            with self.assertRaises(RuntimeError):
                importlib.import_module("server")


if __name__ == "__main__":
    unittest.main()
