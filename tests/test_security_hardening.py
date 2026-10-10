# -*- coding: utf-8 -*-
"""Narrow regressions for the 1.0 security-convergence fixes."""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from pangpang import vault


class SecurityHardeningTests(unittest.TestCase):
    def test_linklike_detects_windows_reparse_attribute_without_resolve(self):
        fake = type("Stat", (), {
            "st_mode": stat.S_IFDIR,
            "st_file_attributes": getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
        })()
        with patch.object(vault.os, "lstat", return_value=fake):
            self.assertTrue(vault._linklike_directory(Path("ignored")))

    @unittest.skipIf(os.name == "nt", "POSIX permission contract")
    def test_posix_secure_directory_is_0700_owned_and_rejects_final_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = root / "data"
            secured = vault.secure_directory(data)
            self.assertEqual(stat.S_IMODE(secured.stat().st_mode), 0o700)
            owner = os.geteuid() if hasattr(os, "geteuid") else os.getuid()
            self.assertEqual(secured.stat().st_uid, owner)

            alias = root / "data-link"
            alias.symlink_to(data, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "unsafe_data_directory"):
                vault.secure_directory(alias)

    @unittest.skipIf(os.name == "nt", "POSIX permission contract")
    def test_posix_vault_master_key_is_0600(self):
        with tempfile.TemporaryDirectory() as td:
            v = vault.Vault(Path(td) / "vault")
            try:
                self.assertEqual(
                    stat.S_IMODE((v.path / "master.key").stat().st_mode), 0o600)
            finally:
                v.close()
    def test_windows_acl_never_uses_path_lookup_and_grants_numeric_sid(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)
            tools = {
                "whoami.exe": r"C:\Windows\System32\whoami.exe",
                "icacls.exe": r"C:\Windows\System32\icacls.exe",
            }
            with patch.object(vault, "_windows_system_binary",
                              side_effect=lambda name: tools[name]) as resolver, \
                 patch.object(vault.subprocess, "check_output",
                              return_value='"DOMAIN\\User","S-1-5-21-123-456-789-1001"\r\n') as check, \
                 patch.object(vault.subprocess, "run") as run:
                vault._secure_windows_directory(path)

            self.assertEqual(
                resolver.call_args_list,
                [call("whoami.exe"), call("icacls.exe")])
            check.assert_called_once_with(
                [tools["whoami.exe"], "/user", "/fo", "csv", "/nh"], text=True)
            argv = run.call_args.args[0]
            self.assertEqual(argv[0], tools["icacls.exe"])
            self.assertEqual(argv[1], str(path))
            self.assertIn("*S-1-5-21-123-456-789-1001:(OI)(CI)F", argv)
            self.assertNotIn("whoami", argv[0].lower())
            self.assertTrue(Path(argv[0]).is_absolute() or argv[0].startswith("C:\\"))

    def test_windows_acl_rejects_malformed_identity_before_icacls(self):
        with tempfile.TemporaryDirectory() as td:
            tools = {
                "whoami.exe": r"C:\Windows\System32\whoami.exe",
                "icacls.exe": r"C:\Windows\System32\icacls.exe",
            }
            with patch.object(vault, "_windows_system_binary",
                              side_effect=lambda name: tools[name]), \
                 patch.object(vault.subprocess, "check_output",
                              return_value='"DOMAIN\\User","NOT-A-SID"\r\n'), \
                 patch.object(vault.subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "windows_account_sid_unavailable"):
                    vault._secure_windows_directory(Path(td))
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
