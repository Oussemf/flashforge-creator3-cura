import ast
import unittest
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from test_protocol import protocol


ROOT = Path(__file__).resolve().parents[1]
tree = ast.parse((ROOT / "cura/plugins/Creator3SendToPrinter/Plugin.py").read_text())
device = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SendDevice")
method = next(node for node in device.body if isinstance(node, ast.FunctionDef) and node.name == "requestWrite")


class DeviceBusyError(Exception):
    pass


scope = {"OutputDeviceError": SimpleNamespace(DeviceBusyError=DeviceBusyError)}
exec(compile(ast.Module(body=[method], type_ignores=[]), "SendDevice", "exec"), scope)
owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SendToPrinter")
can_start = next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == "canStart")
start_scope = {"pyqtProperty": lambda *args, **kwargs: lambda function: function,
               "changed": None, "time": time, "pending_upload_idle": protocol.pending_upload_idle}
exec(compile(ast.Module(body=[can_start], type_ignores=[]), "canStart", "exec"), start_scope)


class OutputDeviceTests(unittest.TestCase):
    def setUp(self):
        self.owner = SimpleNamespace(busy=False, open_dialog=Mock())
        self.device = SimpleNamespace(owner=self.owner)

    def test_cura_button_positional_arguments_open_dialog(self):
        scope["requestWrite"](
            self.device, [], "sliced-project", False, object(),
            preferred_mimetypes=["text/x-gcode"],
        )
        self.owner.open_dialog.assert_called_once_with("sliced-project")

    def test_extension_short_call_and_base_api_arguments(self):
        scope["requestWrite"](self.device, [])
        self.owner.open_dialog.assert_called_once_with(None)
        self.owner.open_dialog.reset_mock()
        scope["requestWrite"](
            self.device, [], file_name="test", limit_mimetypes=True,
            file_handler=object(), filter_by_machine=True,
        )
        self.owner.open_dialog.assert_called_once_with("test")

    def test_busy_button_call_does_not_open_dialog(self):
        self.owner.busy = True
        with self.assertRaises(DeviceBusyError):
            scope["requestWrite"](self.device, [], "test", False, object())
        self.owner.open_dialog.assert_not_called()

    def test_start_requires_owned_upload_and_fresh_idle_status(self):
        printer = protocol.Printer("Test", "127.0.0.1")
        status = protocol.parse_status(
            "MachineStatus: BUILDING\nMoveMode: READY\nStatus: S:1 L:0 J:0 F:0",
            "", "SD printing byte 0/1000",
        )
        owner = SimpleNamespace(_uploaded=(printer, "test.gcode"), _busy=False,
                                monitoring=False, _statuses={printer.address: status})
        self.assertTrue(start_scope["canStart"](owner))
        for field, value in (("_uploaded", None), ("_busy", True), ("monitoring", True)):
            before = getattr(owner, field)
            setattr(owner, field, value)
            self.assertFalse(start_scope["canStart"](owner))
            setattr(owner, field, before)
        status["checked"] -= 26
        self.assertFalse(start_scope["canStart"](owner))


if __name__ == "__main__":
    unittest.main()
