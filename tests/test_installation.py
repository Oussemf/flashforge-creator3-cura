import ctypes
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "win32", "PowerShell installer requires Windows")
class InstallationTests(unittest.TestCase):
    def invoke(self, script, *arguments):
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(ROOT / script), *map(str, arguments)],
            capture_output=True, text=True, timeout=60,
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

    def test_install_restore_without_hash_function_autoload(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config, cache, backups = base / "config", base / "cache", base / "backups"
            config.mkdir()
            (config / "original.cfg").write_bytes(b"original\n")
            module = base / "modules" / "Microsoft.PowerShell.Utility"
            module.mkdir(parents=True)
            (module / "Microsoft.PowerShell.Utility.psd1").write_text(
                "@{ModuleVersion='99.0.0';RootModule='Utility.psm1';FunctionsToExport=@()}\n",
                encoding="utf-8",
            )
            (module / "Utility.psm1").write_text("# Foreign utility module without hash functions.\n")
            for script in ("install.ps1", "restore.ps1"):
                if script == "install.ps1":
                    arguments = f"-ConfigPath '{config}' -CachePath '{cache}' -BackupRoot '{backups}'"
                else:
                    arguments = f"-BackupPath '{next(backups.iterdir())}'"
                wrapper = base / "without-autoload.ps1"
                script_path = str(ROOT / script).replace("'", "''")
                wrapper.write_text(
                    "$ErrorActionPreference = 'Stop'\n"
                    "Import-Module (Join-Path $PSHOME 'Modules\\Microsoft.PowerShell.Utility\\Microsoft.PowerShell.Utility.psd1')\n"
                    "Remove-Item Function:\\Get-FileHash\n"
                    "Remove-Module Microsoft.PowerShell.Utility\n"
                    f"$env:PSModulePath = '{module.parent}'\n"
                    "$PSModuleAutoloadingPreference = 'None'\n"
                    f"& '{script_path}' {arguments}\n",
                    encoding="utf-8",
                )
                result = self.invoke(wrapper)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((config / "original.cfg").read_bytes(), b"original\n")
            self.assertFalse((config / "scripts" / "Creator3Native.py").exists())

    def test_restore_accepts_short_backup_paths_but_rejects_escape(self):
        with tempfile.TemporaryDirectory(prefix="creator3-long-backup-directory-") as directory:
            base = Path(directory)
            config, cache, backups = base / "config", base / "cache", base / "backups"
            config.mkdir()
            (config / "original.cfg").write_bytes(b"original\n")
            result = self.invoke("install.ps1", "-ConfigPath", config, "-CachePath", cache, "-BackupRoot", backups)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            backup = next(backups.iterdir())
            buffer = ctypes.create_unicode_buffer(32768)
            length = ctypes.windll.kernel32.GetShortPathNameW(str(backup), buffer, len(buffer))
            self.assertTrue(0 < length < len(buffer), "Cannot resolve the backup's short path")
            if buffer.value == str(backup):
                self.skipTest("8.3 short names are disabled on this volume")
            manifest_path = backup / "sha256.json"
            original_manifest = manifest_path.read_bytes()
            manifest = json.loads(original_manifest.decode("utf-8-sig"))
            manifest[0]["path"] = "..\\..\\outside.cfg"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            result = self.invoke("restore.ps1", "-BackupPath", buffer.value)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Unsafe path in backup manifest", result.stderr)
            self.assertTrue((config / "scripts" / "Creator3Native.py").exists())
            manifest_path.write_bytes(original_manifest)
            result = self.invoke("restore.ps1", "-BackupPath", buffer.value)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((config / "original.cfg").read_bytes(), b"original\n")
            self.assertFalse((config / "scripts" / "Creator3Native.py").exists())
