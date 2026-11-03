import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "win32", "PowerShell installer requires Windows")
class InstallationTests(unittest.TestCase):
    def invoke(self, script, *arguments):
        # A pwsh runner's module paths can shadow Windows PowerShell's built-in modules.
        environment = os.environ.copy()
        environment.pop("PSModulePath", None)
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(ROOT / script), *map(str, arguments)],
            capture_output=True, text=True, timeout=60,
            env=environment,
        )

    def test_install_backup_restore_and_changed_file_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config, cache, backups = base / "config", base / "cache", base / "backups"
            (config / "scripts").mkdir(parents=True)
            (cache / "nested").mkdir(parents=True)
            original = b"Existing script must be restored.\n"
            (config / "scripts" / "Creator3Native.py").write_bytes(original)
            (config / "existing.cfg").write_bytes(b"Existing quality and printer settings.\n")
            (cache / "nested" / "cache.data").write_bytes(b"Cache snapshot.\n")
            result = self.invoke("install.ps1", "-ConfigPath", config, "-CachePath", cache, "-BackupRoot", backups)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            backup = next(backups.iterdir())
            journal = json.loads((backup / "installation.json").read_text(encoding="utf-8-sig"))
            self.assertEqual(len(journal["files"]), 9)
            self.assertEqual((backup / "config" / "scripts" / "Creator3Native.py").read_bytes(), original)
            self.assertEqual((backup / "cache" / "nested" / "cache.data").read_bytes(), b"Cache snapshot.\n")
            changed = config / "plugins" / "Creator3SendToPrinter" / "SendDialog.qml"
            installed = changed.read_bytes()
            changed.write_bytes(installed + b"\n// Local edit\n")
            result = self.invoke("restore.ps1", "-BackupPath", backup)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("changed after installation", result.stderr)
            changed.write_bytes(installed)
            result = self.invoke("restore.ps1", "-BackupPath", backup)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((config / "scripts" / "Creator3Native.py").read_bytes(), original)
            self.assertEqual((config / "existing.cfg").read_bytes(), b"Existing quality and printer settings.\n")
            self.assertFalse(changed.exists())

    def test_corrupted_backup_is_rejected_before_restoration(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config, cache, backups = base / "config", base / "cache", base / "backups"
            config.mkdir()
            (config / "existing.cfg").write_bytes(b"original\n")
            result = self.invoke("install.ps1", "-ConfigPath", config, "-CachePath", cache, "-BackupRoot", backups)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            backup = next(backups.iterdir())
            (backup / "config" / "existing.cfg").write_bytes(b"corrupted\n")
            result = self.invoke("restore.ps1", "-BackupPath", backup)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Corrupted backup", result.stderr)
            self.assertTrue((config / "scripts" / "Creator3Native.py").exists())

    def test_empty_configuration_backup_has_valid_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config, cache, backups = base / "config", base / "cache", base / "backups"
            config.mkdir()
            result = self.invoke("install.ps1", "-ConfigPath", config, "-CachePath", cache, "-BackupRoot", backups)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            backup = next(backups.iterdir())
            self.assertEqual(json.loads((backup / "sha256.json").read_text(encoding="utf-8-sig")), [])
            result = self.invoke("restore.ps1", "-BackupPath", backup)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_backup_inside_configuration_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = base / "config"
            config.mkdir()
            for backup in (config, config / "backups"):
                result = self.invoke("install.ps1", "-ConfigPath", config, "-CachePath", base / "cache", "-BackupRoot", backup)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Backups must be outside", result.stderr)


class PowerShellEnvironmentTests(unittest.TestCase):
    def test_installer_and_restore_rebuild_native_module_paths(self):
        with patch.dict(os.environ, {"PSModulePath": "foreign-pwsh-modules", "C3_TEST_ENV": "preserved"}):
            with patch.object(subprocess, "run") as run:
                for script in ("install.ps1", "restore.ps1"):
                    InstallationTests().invoke(script)
                    environment = run.call_args.kwargs["env"]
                    self.assertNotIn("PSModulePath", environment)
                    self.assertEqual(environment["C3_TEST_ENV"], "preserved")
                    self.assertEqual(os.environ["PSModulePath"], "foreign-pwsh-modules")
