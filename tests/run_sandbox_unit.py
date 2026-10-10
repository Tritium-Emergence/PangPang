"""Run application unit tests in a Windows restricted-token sandbox.

Only temporary-directory ACL setup is substituted: Windows' owner-only DACL
excludes the sandbox token. This harness does NOT test production ACL isolation.
Never use it for the OS-isolation acceptance check.
"""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
scratch = ROOT / '.test-tmp'
scratch.mkdir(exist_ok=True)
tempfile.tempdir = str(scratch)
original_mkdir = os.mkdir


def test_mkdir(path, mode=0o777, *, dir_fd=None):
    return original_mkdir(path, 0o777, dir_fd=dir_fd)


def test_directory(directory):
    path = Path(directory).resolve()
    if not path.is_relative_to(scratch):
        raise RuntimeError('test ACL substitute only permits isolated scratch data')
    path.mkdir(parents=True, exist_ok=True)
    return path


if __name__ == '__main__':
    import pangpang.service
    import pangpang.vault
    with patch('os.mkdir', test_mkdir), patch('pangpang.service.secure_directory', test_directory), patch('pangpang.vault.secure_directory', test_directory):
        unittest.main(module=None)
