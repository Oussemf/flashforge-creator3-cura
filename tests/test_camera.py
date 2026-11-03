import ast
import importlib.util
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HAS_QT = importlib.util.find_spec("PyQt6") is not None

if HAS_QT:
    from PyQt6.QtCore import (
        QByteArray, QBuffer, QCoreApplication, QIODevice, QObject, QTimer, QUrl,
        pyqtProperty, pyqtSignal, pyqtSlot,
    )
    from PyQt6.QtGui import QImage
    from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

    class Logger:
        messages = []

        @classmethod
        def log(cls, *args):
            cls.messages.append(args)

    from ipaddress import IPv4Address

    def address(host):
        return str(IPv4Address(host))

    tree = ast.parse((ROOT / "cura/plugins/Creator3SendToPrinter/Plugin.py").read_text())
    camera = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "CameraPreview")
    exec(compile(ast.Module(body=[camera], type_ignores=[]), "CameraPreview", "exec"), globals())


@unittest.skipUnless(HAS_QT, "Camera tests require Cura's PyQt6 runtime")
class CameraTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])
        image = QImage(16, 12, QImage.Format.Format_RGB32)
        image.fill(0x336699)
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buffer, "JPEG"):
            raise RuntimeError("Qt JPEG support is missing")
        cls.jpeg = bytes(data)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                cls.requests.append(self.path)
                if cls.delay:
                    time.sleep(cls.delay)
                self.send_response(cls.code)
                self.send_header("Content-Type", "image/jpeg")
                self.end_headers()
                try:
                    self.wfile.write(cls.payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 8080), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(3)

    def setUp(self):
        type(self).requests = []
        type(self).payload = self.jpeg
        type(self).code = 200
        type(self).delay = 0
        self.preview = CameraPreview(None)

    def tearDown(self):
        self.preview.setHost("")
        self.preview.deleteLater()
        self.app.processEvents()

    def wait_for(self, condition, seconds=7):
        deadline = time.monotonic() + seconds
        while not condition():
            self.app.processEvents()
            if time.monotonic() >= deadline:
                self.fail("Camera condition timed out")
            time.sleep(0.01)

    def test_snapshot_and_no_overlapping_requests(self):
        self.preview.setHost("127.0.0.1")
        reply = self.preview.reply
        self.preview.refresh()
        self.assertIs(self.preview.reply, reply)
        self.wait_for(lambda: bool(self.preview.source))
        self.assertEqual(self.requests, ["/?action=snapshot"])
        self.assertTrue(self.preview.source.startswith("data:image/jpeg;base64,"))
        self.assertEqual(self.preview.error, "")
        self.assertTrue(self.preview.timer.isActive())
        self.wait_for(lambda: len(self.requests) >= 2)
        self.preview.setHost("")
        self.assertFalse(self.preview.timer.isActive())
        self.assertEqual(self.preview.source, "")

    def test_invalid_image_and_http_error_are_explicit_and_recover(self):
        type(self).payload = b"not a JPEG"
        self.preview.setHost("127.0.0.1")
        self.wait_for(lambda: bool(self.preview.error))
        self.assertEqual(self.preview.source, "")
        self.assertIn("JPEG", self.preview.error)
        type(self).payload = self.jpeg
        self.preview.refresh()
        self.wait_for(lambda: bool(self.preview.source))
        self.assertEqual(self.preview.error, "")
        type(self).code = 503
        self.preview.refresh()
        self.wait_for(lambda: bool(self.preview.error))
        self.assertEqual(self.preview.source, "")

    def test_cancelled_reply_cannot_restore_old_printer_image(self):
        type(self).delay = 0.2
        self.preview.setHost("127.0.0.1")
        self.preview.setHost("")
        self.assertIsNone(self.preview.reply)
        self.assertEqual(self.preview.source, "")
        self.assertEqual(self.preview.error, "")
        self.preview.setHost("127.0.0.2")
        self.wait_for(lambda: bool(self.preview.error))
        self.assertEqual(self.preview.source, "")

    def test_request_timeout(self):
        type(self).delay = 6
        self.preview.setHost("127.0.0.1")
        self.preview.timer.stop()
        self.wait_for(lambda: bool(self.preview.error))
        self.assertEqual(self.preview.source, "")
        self.assertIsNone(self.preview.reply)


if __name__ == "__main__":
    unittest.main()
