"""Check portable entry point without starting a listener or changing user data."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run


class RunnerTests(unittest.TestCase):
    def test_default_port_and_custom_data_directory(self):
        before = Path.cwd()
        try:
            with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ), patch('uvicorn.run') as server:
                run.main(['--data-dir', folder])
                self.assertEqual(Path(os.environ['PATENT_ATLAS_DATA']), Path(folder).resolve())
                server.assert_called_once_with('app:app', host='127.0.0.1', port=8810, workers=1)
        finally:
            os.chdir(before)

    def test_custom_port_stays_on_loopback_and_invalid_ports_fail_before_launch(self):
        before = Path.cwd()
        try:
            with patch('uvicorn.run') as server:
                run.main(['--port', '8811'])
                server.assert_called_once_with('app:app', host='127.0.0.1', port=8811, workers=1)
            for value in ('0', '-1', '65536', 'abc'):
                with self.subTest(port=value), patch('uvicorn.run') as server, contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        run.main(['--port', value])
                    server.assert_not_called()
        finally:
            os.chdir(before)
