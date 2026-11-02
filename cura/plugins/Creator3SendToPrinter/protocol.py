import binascii
import ipaddress
import logging
import re
import socket
import struct
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from threading import Event
from typing import Callable


LOG = logging.getLogger(__name__)
PORT = 8899
BLOCK_SIZE = 4096
REMOTE_ROOT = "0:/user/"
MAX_NAME_BYTES = 25


class PrinterError(ValueError):
    pass


class Cancelled(PrinterError):
    pass


@dataclass(frozen=True)
class Printer:
    name: str
    address: str
    port: int = PORT


def address(value: str) -> str:
    ip = ipaddress.IPv4Address(value.strip())
    if ip.is_multicast or ip.is_unspecified or int(ip) == 0xffffffff:
        raise PrinterError("A unicast IPv4 address is required")
    return str(ip)


def transfer_name(title: str) -> str:
    stem = PureWindowsPath(title.replace("/", "\\")).stem
    ascii_name = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    prefix = re.sub(r"[^A-Za-z0-9_-]+", "_", ascii_name).strip("_-")[:10] or "Creator3"
    return f"{prefix}_{uuid.uuid4().hex[:8]}.gcode"


def remote_path(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.gcode", name) or len(name.encode("ascii")) > MAX_NAME_BYTES:
        raise PrinterError("Invalid or overlong transfer filename")
    return REMOTE_ROOT + name


def packet(sequence: int, data: bytes) -> bytes:
    if not data or len(data) > BLOCK_SIZE:
        raise PrinterError("Invalid block size")
    return struct.pack(">4I", 0x5a5aa5a5, sequence, len(data), binascii.crc32(data)) + data.ljust(BLOCK_SIZE, b"\0")


def validate_gcode(path: Path) -> None:
    native = False
    movement = False
    mode = None
    idex_stamp = False
    markers = []
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if "CREATOR3_EXPORT_BLOQUE" in line or "NE PAS IMPRIMER" in line or "DO NOT PRINT" in line:
                raise PrinterError("Cura export blocked: this file will not be uploaded")
            native = native or line.strip() == ";CREATOR3_NATIVE_V1"
            idex_stamp = idex_stamp or line.strip() == ";CREATOR3_IDEX_CALPAD_V2"
            if line.startswith(";mode:"):
                mode = line.split(";", 2)[1].partition(":")[2]
            if line.startswith(";calPad "):
                markers.append(line.strip())
            if re.match(r"^G1\b.*\bE[-+]?[0-9.]+", line):
                movement = True
    if not native or mode not in ("solo", "mirror", "duplicate") or not movement:
        raise PrinterError("Incomplete native Creator 3 G-code: check post-processing and slice again")
    if mode != "solo":
        expected = [
            marker for block in ("A", "B", "SA", "SB")
            for marker in (f";calPad {block} start Z0.20", f";calPad {block} end")
        ]
        if not idex_stamp or markers != expected:
            raise PrinterError("Native IDEX calibration is missing or incomplete")


def discovery_reply(data: bytes, source: str) -> Printer:
    raw_name = data.split(b"\0", 1)[0]
    if not raw_name or len(raw_name) > 128:
        raise PrinterError("Invalid discovery response")
    name = raw_name.decode("utf-8")
    if any(ord(char) < 32 for char in name):
        raise PrinterError("Invalid discovery name")
    port = struct.unpack_from(">H", data, 132)[0] if len(data) >= 140 else PORT
    if not 0 < port <= 65535:
        raise PrinterError("Invalid discovery port")
    return Printer(name, address(source), port)


def discover(interfaces: list[str], cancel: Event, duration: float = 4) -> tuple[list[Printer], list[str]]:
    sockets = []
    warnings = []
    found = {}
    try:
        for interface in interfaces:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.bind((address(interface), 8002))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
                sock.sendto(b"Hello World!", ("225.0.0.9", 19000))
                sock.setblocking(False)
                sockets.append(sock)
            except (OSError, ValueError) as error:
                sock.close()
                LOG.warning("Discovery interface %s failed: %s", interface, error)
                warnings.append(f"{interface} : {error}")
        if not sockets:
            raise PrinterError("Discovery failed: " + (" ; ".join(warnings) or "no IPv4 interfaces"))
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            if cancel.is_set():
                raise Cancelled("Discovery cancelled")
            for sock in sockets:
                try:
                    data, sender = sock.recvfrom(2048)
                except BlockingIOError:
                    continue
                try:
                    printer = discovery_reply(data, sender[0])
                except (ValueError, UnicodeError) as error:
                    LOG.warning("Ignored discovery response from %s: %s", sender[0], error)
                    continue
                found[printer.address] = printer
            time.sleep(0.04)
        return sorted(found.values(), key=lambda printer: (printer.name, printer.address)), warnings
    finally:
        for sock in sockets:
            sock.close()


class Client:
    def __init__(self, host: str, cancel: Event, port: int = PORT):
        self.host = address(host)
        self.cancel = cancel
        self.port = port
        self.sock = None
        self.buffer = bytearray()
        self.control = False
        self.raw_transfer = False
        self.incomplete_upload = False

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise Cancelled("Upload cancelled; do not print the partial file")

    def __enter__(self):
        self._check_cancel()
        self.sock = socket.create_connection((self.host, self.port), timeout=10)
        self.sock.settimeout(10)
        try:
            info = self.command("M115")
            require_creator3(info)
            self.ready()
            response = self.command("M601 S1")
            version = re.search(r"^Control Success V(\d+\.\d+)\s*$", response, re.M)
            if "Control Success." not in response and version is None:
                raise PrinterError("Control denied: disconnect FlashPrint and try again")
            self.control = True
            self.raw_transfer = version is not None and float(version.group(1)) >= 2
            self.ready()
            return self
        except (OSError, ValueError) as error:
            try:
                if self.control and not self.cancel.is_set():
                    try:
                        self.command("M602")
                    except (OSError, ValueError) as release_error:
                        LOG.warning("Control release after initialization failure: %s", release_error)
                        raise PrinterError(f"{error}; control release not confirmed: {release_error}") from error
            finally:
                self.sock.close()
            raise

    def __exit__(self, exc_type, exc, traceback):
        try:
            if self.control and not self.incomplete_upload and not self.cancel.is_set():
                try:
                    self.command("M602")
                except (OSError, ValueError) as error:
                    LOG.warning("Control release failed: %s", error)
                    if exc_type is None:
                        raise PrinterError(f"Operation acknowledged, but control release not confirmed: {error}") from error
        finally:
            self.sock.close()

    def _line(self) -> str:
        deadline = time.monotonic() + 15
        while b"\n" not in self.buffer:
            self._check_cancel()
            if time.monotonic() >= deadline:
                raise PrinterError("Printer response timed out")
            chunk = self.sock.recv(4096)
            if not chunk:
                raise PrinterError("Connection closed by the printer")
            self.buffer.extend(chunk)
            if len(self.buffer) > 65536:
                raise PrinterError("Printer response exceeds the size limit")
        raw, _, remainder = self.buffer.partition(b"\n")
        self.buffer = bytearray(remainder)
        return raw.rstrip(b"\r").decode("utf-8")

    def command(self, command: str) -> str:
        self._check_cancel()
        self.sock.sendall(("~" + command + "\r\n").encode("ascii"))
        lines = []
        for _ in range(128):
            line = self._line()
            if line.strip() == "ok":
                response = "\n".join(lines)
                opcode = command.split()[0]
                if f"CMD {opcode} Received." not in response:
                    raise PrinterError(f"Missing {opcode} acknowledgement: {response}")
                if re.search(r"error|failed|out of space|open fail", response, re.I):
                    raise PrinterError(f"Printer rejected {opcode}: {response}")
                return response
            lines.append(line)
        raise PrinterError("Unterminated command response")

    def ready(self) -> None:
        status = self.command("M119")
        if not re.search(r"MachineStatus\s*:\s*READY\b", status, re.I):
            raise PrinterError("Printer not ready (printing, paused or in error): " + status)

    def upload(self, path: Path, name: str, progress: Callable[[int], None]) -> None:
        validate_gcode(path)
        target = remote_path(name)
        size = path.stat().st_size
        self.incomplete_upload = True
        response = self.command(f"M28 {size} {target}")
        if "Writing to file:" not in response:
            raise PrinterError("Remote file creation not confirmed")
        sent = 0
        sequence = 0
        last_progress = -1
        with path.open("rb") as stream:
            while chunk := stream.read(BLOCK_SIZE):
                self._check_cancel()
                if self.raw_transfer:
                    self.sock.sendall(chunk)
                else:
                    self.sock.sendall(packet(sequence, chunk))
                    ack = self._line().strip()
                    if ack != f"N{sequence} ok":
                        raise PrinterError(f"Block {sequence} not acknowledged: {ack}")
                sent += len(chunk)
                current_progress = min(99, int(sent * 100 / size))
                if current_progress != last_progress:
                    progress(current_progress)
                    last_progress = current_progress
                sequence += 1
        if sent != size:
            raise PrinterError("File size changed during upload")
        time.sleep(0.5)
        response = self.command("M29")
        if "Done saving file." not in response:
            raise PrinterError("Remote file save not confirmed")
        self.incomplete_upload = False
        progress(100)

    def start(self, name: str) -> None:
        self.ready()
        self.command("M23 " + remote_path(name))


def require_creator3(info: str) -> None:
    if not re.search(r"^Machine\s*Type\s*:\s*(?:Flashforge\s+)?Creator\s*3\s*$", info, re.I | re.M):
        raise PrinterError("This address does not identify a Flashforge Creator 3")


def parse_status(status: str, temperatures: str, printing: str) -> dict:
    state = re.search(r"MachineStatus\s*:\s*([A-Z_]+)", status, re.I)
    if state is None:
        raise PrinterError("M119 did not report a printer state")
    values = {}
    for match in re.finditer(r"\b(T[01]?|B)\s*:\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", temperatures):
        tool, current, target = match.groups()
        values[tool] = f"{float(current):g}/{float(target):g}"
    progress = None
    match = re.search(r"SD printing byte\s+(\d+)\s*/\s*(\d+)", printing, re.I)
    if match is not None:
        current, total = map(int, match.groups())
        if total > 0:
            progress = min(100, current * 100 / total)
    return {
        "state": state.group(1).upper(),
        "right": values.get("T0", values.get("T", "--")),
        "left": values.get("T1", "--"),
        "bed": values.get("B", "--"),
        "print_progress": progress,
        "checked": time.monotonic(),
        "error": "",
    }


def read_status(printer: Printer, cancel: Event) -> dict:
    client = Client(printer.address, cancel, printer.port)
    client._check_cancel()
    client.sock = socket.create_connection((printer.address, printer.port), timeout=3)
    client.sock.settimeout(3)
    try:
        require_creator3(client.command("M115"))
        return parse_status(client.command("M119"), client.command("M105"), client.command("M27"))
    finally:
        client.sock.close()
