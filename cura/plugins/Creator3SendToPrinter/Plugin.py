import json
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from threading import Event

from PyQt6.QtCore import QCoreApplication, QObject, QThread, QTimer, pyqtProperty, pyqtSignal, pyqtSlot
from PyQt6.QtNetwork import QNetworkInterface
from UM.Backend.Backend import BackendState
from UM.Extension import Extension
from UM.Logger import Logger
from UM.Message import Message
from UM.Mesh.MeshWriter import MeshWriter
from UM.OutputDevice.OutputDevice import OutputDevice
from UM.OutputDevice import OutputDeviceError

from .protocol import Client, Printer, address, discover, read_status, transfer_name, validate_gcode


class Worker(QThread):
    result = pyqtSignal(bool, str, object)
    progress = pyqtSignal(int)

    def __init__(self, operation, parent):
        super().__init__(parent)
        self.operation = operation
        self.cancel = Event()

    def run(self):
        try:
            value = self.operation(self.cancel, self.progress.emit)
        except (OSError, ValueError, UnicodeError) as error:
            Logger.logException("e", "Creator 3 network operation failed")
            self.result.emit(False, str(error), None)
        else:
            self.result.emit(True, "", value)


class SendDevice(OutputDevice):
    def __init__(self, owner):
        super().__init__("creator3-send-to-printer")
        self.owner = owner
        self.setName("Flashforge Creator 3")
        self.setShortDescription("Send to printer")
        self.setDescription("Send to a Flashforge Creator 3...")
        self.setIconName("print")

    def requestWrite(self, nodes, file_name=None, **kwargs):
        if self.owner.busy:
            raise OutputDeviceError.DeviceBusyError()
        self.owner.open_dialog(file_name)


class SendToPrinter(QObject, Extension):
    changed = pyqtSignal()

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.device = SendDevice(self)
        self.dialog = None
        self.worker = None
        self.status_worker = None
        self._statuses = {}
        self._busy = False
        self._progress = 0
        self._status = ""
        self._name = ""
        self._uploaded = None
        self._printers = []
        self._job_title = "Creator3"
        self._writing = False
        preferences = app.getPreferences()
        preferences.addPreference("creator3_send/printers", "[]")
        preferences.addPreference("creator3_send/last_address", "")
        try:
            saved = json.loads(preferences.getValue("creator3_send/printers"))
            if not isinstance(saved, list) or any(not isinstance(entry, dict) for entry in saved):
                raise ValueError("The printer list must contain name/address objects")
            for entry in saved:
                port = int(entry.get("port", 8899))
                if not 0 < port <= 65535:
                    raise ValueError("Invalid saved port")
                printer = Printer(str(entry["name"]), address(entry["address"]), port)
                if printer.address not in [p.address for p in self._printers]:
                    self._printers.append(printer)
        except (ValueError, TypeError, KeyError) as error:
            Logger.logException("e", "Invalid saved Creator 3 printer list")
            self._status = "Invalid saved printer list: " + str(error)
        self.setMenuName("Creator 3 - Send to printer")
        self.addMenuItem("Send sliced build plate...", self.open_dialog)
        app.globalContainerStackChanged.connect(self.refresh_device)
        self.monitor_timer = QTimer(self)
        self.monitor_timer.setInterval(10000)
        self.monitor_timer.timeout.connect(self.refreshStatus)
        QCoreApplication.instance().aboutToQuit.connect(self.shutdown)
        QTimer.singleShot(0, self.refresh_device)

    def shutdown(self):
        self.monitor_timer.stop()
        for worker in (self.worker, self.status_worker):
            if worker is not None and worker.isRunning():
                worker.cancel.set()
                worker.wait(12000)

    @pyqtProperty(bool, notify=changed)
    def busy(self):
        return self._busy

    @pyqtProperty(int, notify=changed)
    def progress(self):
        return self._progress

    @pyqtProperty(str, notify=changed)
    def status(self):
        return self._status

    @pyqtProperty(str, notify=changed)
    def filename(self):
        return self._name

    @pyqtProperty(bool, notify=changed)
    def canStart(self):
        if self._uploaded is None or self._busy or self.monitoring:
            return False
        printer, _ = self._uploaded
        return any(p["address"] == printer.address and p["ready"] for p in self.printers)

    @pyqtProperty(bool, notify=changed)
    def monitoring(self):
        return self.status_worker is not None

    @pyqtProperty("QVariantList", notify=changed)
    def printers(self):
        labels = {"READY": "Ready", "PRINTING": "Printing", "BUILDING_FROM_SD": "Printing from storage",
                  "PAUSED": "Paused", "PAUSE": "Paused"}
        result = []
        for p in self._printers:
            status = self._statuses.get(p.address, {})
            state = status.get("state", "UNKNOWN")
            fresh = time.monotonic() - status.get("checked", 0) <= 25
            result.append({
                "name": p.name, "address": p.address, "label": f"{p.name} - {p.address}",
                "state": labels.get(state, state if state != "UNKNOWN" else "Status unavailable"),
                "right": status.get("right", "--"), "left": status.get("left", "--"),
                "bed": status.get("bed", "--"), "printProgress": status.get("print_progress"),
                "error": status.get("error", ""), "fresh": fresh,
                "ready": state == "READY" and fresh,
            })
        return result

    @pyqtProperty(str, notify=changed)
    def uploadedDescription(self):
        if self._uploaded is None:
            return ""
        printer, name = self._uploaded
        return f"{name} on {printer.name} ({printer.address})"

    @pyqtProperty(int, notify=changed)
    def selectedIndex(self):
        last = self.app.getPreferences().getValue("creator3_send/last_address")
        return next((i for i, p in enumerate(self._printers) if p.address == last), 0)

    @pyqtSlot(int)
    def selectPrinter(self, index):
        if not self._busy and 0 <= index < len(self._printers):
            self.app.getPreferences().setValue("creator3_send/last_address", self._printers[index].address)

    def refresh_device(self):
        stack = self.app.getGlobalContainerStack()
        manager = self.app.getOutputDeviceManager()
        personal = stack is not None and stack.getBottom().getId() == "creator3_personal"
        if personal and manager.getOutputDevice(self.device.getId()) is None:
            manager.addOutputDevice(self.device)
        elif not personal and manager.getOutputDevice(self.device.getId()) is not None:
            manager.removeOutputDevice(self.device.getId())

    def open_dialog(self, file_name=None):
        if self._busy:
            return
        self._job_title = file_name or self.app.getPrintInformation().jobName or "Creator3"
        self._name = transfer_name(self._job_title)
        self._uploaded = None
        self.changed.emit()
        if self.dialog is None:
            self.dialog = self.app.createQmlComponent(str(Path(__file__).with_name("SendDialog.qml")), {"manager": self})
        if self.dialog is None:
            self.error("Could not open the Send to printer window")
            return
        self.dialog.show()
        self.monitor_timer.start()
        if not self._printers:
            QTimer.singleShot(0, self.search)
        else:
            QTimer.singleShot(0, self.refreshStatus)

    @pyqtSlot(bool)
    def visibility(self, visible):
        if not visible:
            self.monitor_timer.stop()
            if self.status_worker is not None:
                self.status_worker.cancel.set()

    def error(self, text):
        Logger.log("e", "Creator 3 Send: %s", text)
        self._status = text
        self.changed.emit()
        Message(text, title="Creator 3 - operation failed", lifetime=0).show()

    def save_printers(self):
        self.app.getPreferences().setValue("creator3_send/printers", json.dumps([asdict(p) for p in self._printers]))

    @pyqtSlot(str, str)
    def addPrinter(self, name, host):
        if self._busy:
            return
        try:
            printer = Printer(name.strip() or "Creator 3", address(host))
        except ValueError as error:
            self.error(str(error))
            return
        self._printers = [p for p in self._printers if p.address != printer.address] + [printer]
        self.save_printers()
        self.app.getPreferences().setValue("creator3_send/last_address", printer.address)
        self.changed.emit()
        self.refreshStatus()

    @pyqtSlot(int)
    def removePrinter(self, index):
        if not self._busy and 0 <= index < len(self._printers):
            self._printers.pop(index)
            self.save_printers()
            self.changed.emit()
            self.refreshStatus()

    @pyqtSlot()
    def refreshStatus(self):
        if self._busy or self.status_worker is not None or not self._printers:
            return
        printers = tuple(self._printers)

        def operation(cancel, progress):
            values = {}
            for printer in printers:
                if cancel.is_set():
                    break
                try:
                    values[printer.address] = read_status(printer, cancel)
                except (OSError, ValueError) as error:
                    Logger.log("w", "Creator 3 status %s: %s", printer.address, error)
                    values[printer.address] = {
                        "state": "UNKNOWN", "checked": time.monotonic(), "error": str(error),
                    }
            return values

        self.status_worker = Worker(operation, self)
        self.status_worker.result.connect(self.status_done)
        self.status_worker.finished.connect(self.status_finished)
        self.changed.emit()
        self.status_worker.start()

    def status_done(self, success, error, value):
        if not success:
            self.error(error)
            return
        self._statuses.update(value)
        self.changed.emit()

    def status_finished(self):
        self.status_worker.deleteLater()
        self.status_worker = None
        self.changed.emit()

    def launch(self, operation, done):
        self._busy = True
        self._progress = 0
        self.worker = Worker(operation, self)
        self.worker.progress.connect(self.update_progress)
        self.worker.result.connect(done)
        self.worker.finished.connect(self.thread_finished)
        self.changed.emit()
        self.worker.start()

    def thread_finished(self):
        self.worker.deleteLater()
        self.worker = None
        self._busy = False
        self.changed.emit()
        if self.dialog is not None and self.dialog.isVisible():
            QTimer.singleShot(0, self.refreshStatus)

    def update_progress(self, value):
        if value != self._progress:
            self._progress = value
            self.changed.emit()
            if self._writing:
                self.device.writeProgress.emit(self.device, value)

    @pyqtSlot()
    def search(self):
        if self._busy:
            return
        interfaces = sorted({
            str(ip.toString()) for ip in QNetworkInterface.allAddresses()
            if "." in ip.toString() and not ip.isLoopback()
        })
        self._status = "Searching for printers on the local network..."
        self.launch(lambda cancel, progress: discover(interfaces, cancel), self.search_done)

    def search_done(self, success, error, value):
        if not success:
            self.error(error)
            return
        found, warnings = value
        by_ip = {p.address: p for p in self._printers}
        by_ip.update({p.address: p for p in found})
        self._printers = list(by_ip.values())
        self.save_printers()
        self._status = f"{len(found)} printer(s) discovered."
        if not found:
            self._status += " Check your network/firewall or add the IP address manually."
        if warnings:
            self._status += " Unavailable network interfaces: " + " ; ".join(warnings)
        self.changed.emit()

    @pyqtSlot()
    def cancel(self):
        if self.worker is not None:
            self.worker.cancel.set()
            self._status = "Cancellation requested..."
            self.changed.emit()

    @pyqtSlot(int)
    def send(self, index):
        if self._busy:
            return
        if self.status_worker is not None:
            self.error("Status refresh in progress: wait before sending")
            return
        if not 0 <= index < len(self._printers):
            self.error("Select a printer or add its IP address")
            return
        if not self.printers[index]["ready"]:
            self.error("Printer not ready or status unconfirmed: refresh its status before sending")
            return
        self._uploaded = None
        self._name = transfer_name(self._job_title)
        self._busy = True
        self._status = "Exporting and post-processing the active build plate..."
        self.changed.emit()
        QTimer.singleShot(0, lambda: self.prepare(index))

    def prepare(self, index):
        temporary = None
        try:
            stack = self.app.getGlobalContainerStack()
            if stack is None or stack.getBottom().getId() != "creator3_personal":
                raise ValueError("Use the Creator 3 integration profile")
            if self.app.getBackend()._backend_state != BackendState.Done:
                raise ValueError("Slicing must be complete: slice again before sending")
            scene = self.app.getController().getScene()
            plate = self.app.getMultiBuildPlateModel().activeBuildPlate
            if not getattr(scene, "gcode_dict", {}).get(plate):
                raise ValueError("No G-code on the active build plate")
            self._writing = True
            self.device.writeStarted.emit(self.device)
            writer = self.app.getPluginRegistry().getPluginObject("GCodeWriter")
            if writer is None:
                raise ValueError("Cura GCodeWriter is unavailable")
            temporary = tempfile.TemporaryDirectory(prefix="creator3-send-")
            path = Path(temporary.name) / self._name
            with path.open("w", encoding="utf-8", newline="\n") as stream:
                if not writer.write(stream, [], MeshWriter.OutputMode.TextMode):
                    raise ValueError("Cura export failed: " + str(writer.getInformation()))
            validate_gcode(path)
            printer = self._printers[index]
            name = self._name
            self.app.getPreferences().setValue("creator3_send/last_address", printer.address)
            self._status = f"Uploading to {printer.name}: {name} ({path.stat().st_size / 1048576:.1f} MiB)"

            def operation(cancel, progress):
                try:
                    with Client(printer.address, cancel, printer.port) as client:
                        client.upload(path, name, progress)
                    return printer, name
                finally:
                    temporary.cleanup()

            self.launch(operation, self.send_done)
        except (OSError, ValueError, TypeError, AttributeError) as error:
            if temporary is not None:
                temporary.cleanup()
            self._busy = False
            self.finish_write(False)
            self.error(str(error))

    def finish_write(self, success):
        if self._writing:
            self.device.writeFinished.emit(self.device)
            signal = self.device.writeSuccess if success else self.device.writeError
            signal.emit(self.device)
            self._writing = False

    def send_done(self, success, error, value):
        self.finish_write(success)
        if not success:
            self.error(error + "\nNo start command was sent. A partial file may remain on the printer.")
            return
        self._uploaded = value
        printer, name = value
        self._status = f"Upload confirmed: {name} on {printer.name}. Printing has NOT started."
        self.changed.emit()
        Message(self._status, title="Creator 3 - file uploaded", lifetime=0).show()

    @pyqtSlot()
    def startPrint(self):
        if not self.canStart:
            self.error("No completed upload is ready to start")
            return
        printer, name = self._uploaded
        self._uploaded = None
        self._status = f"Start requested: {name} on {printer.name}"

        def operation(cancel, progress):
            with Client(printer.address, cancel, printer.port) as client:
                client.start(name)
            return name

        self.launch(operation, self.start_done)

    def start_done(self, success, error, value):
        if not success:
            self.error("Start not confirmed (do not blindly retry): " + error)
            return
        self._status = "Start command acknowledged: " + value
        self.changed.emit()
