import ast
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from test_protocol import FakePrinter, protocol


ROOT = Path(__file__).resolve().parents[1]
tree = ast.parse((ROOT / "cura/plugins/Creator3SendToPrinter/Plugin.py").read_text())
owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SendToPrinter")
methods = [
    node for node in owner.body
    if isinstance(node, ast.FunctionDef) and node.name in ("preheatBed", "preheat_done")
]
scope = {
    "pyqtSlot": lambda *args: lambda method: method,
    "validate_bed_temperature": protocol.validate_bed_temperature,
    "Client": protocol.Client,
}
exec(compile(ast.Module(body=methods, type_ignores=[]), "preheat", "exec"), scope)


class Owner:
    preheatBed = scope["preheatBed"]
    preheat_done = scope["preheat_done"]

    def __init__(self, port=8899):
        self._busy = False
        self.monitoring = False
        self._printers = [protocol.Printer("Selected printer", "127.0.0.1", port)]
        self.printers = [{"ready": True}]
        self._statuses = {"127.0.0.1": {"checked": time.monotonic()}}
        self._status = ""
        self._uploaded = object()
        self.changed = SimpleNamespace(emit=lambda: None)
        self.errors = []
        self.operation = None

    def error(self, text):
        self.errors.append(text)

    def launch(self, operation, done):
        self.operation = operation
        self.done = done
        self._busy = True


class PreheatTests(unittest.TestCase):
    def test_ui_guards_do_not_launch_a_request(self):
        for case in ("busy", "monitoring", "missing", "stale", "invalid"):
            with self.subTest(case=case):
                owner = Owner()
                if case == "busy":
                    owner._busy = True
                elif case == "monitoring":
                    owner.monitoring = True
                elif case == "missing":
                    owner._printers = []
                elif case == "stale":
                    owner.printers[0]["ready"] = False
                owner.preheatBed(0, 121 if case == "invalid" else 60)
                self.assertIsNone(owner.operation)
                self.assertTrue(owner.errors or case == "busy")

    def test_selected_target_is_captured_without_changing_uploaded_job(self):
        with FakePrinter() as server:
            owner = Owner(server.port)
            uploaded = owner._uploaded
            owner.preheatBed(0, 60)
            owner._printers = [protocol.Printer("Different printer", "127.0.0.2")]
            value = owner.operation(threading.Event(), lambda value: None)
            owner.done(True, "", value)
            self.assertIn("~M140 S60", server.commands)
            self.assertIn("Selected printer", owner._status)
            self.assertIn("acknowledged", owner._status)
            self.assertIs(owner._uploaded, uploaded)
            self.assertEqual(owner._statuses, {})
            self.assertEqual(owner.errors, [])

    def test_zero_disables_heating_and_failure_reports_uncertain_heater_state(self):
        owner = Owner()
        owner.preheat_done(True, "", (owner._printers[0], 0))
        self.assertIn("Heating is off", owner._status)
        owner.preheat_done(False, "Connection closed", None)
        self.assertIn("heating may still be active", owner.errors[-1])
        self.assertIn("Connection closed", owner.errors[-1])


if __name__ == "__main__":
    unittest.main()
