import hashlib
import importlib.util
import socket
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("c3send_protocol", Path(__file__).resolve().parents[1] / "cura" / "plugins" / "Creator3SendToPrinter" / "protocol.py")
protocol = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = protocol
spec.loader.exec_module(protocol)


NATIVE = b";CREATOR3_NATIVE_V1\n;mode:solo; source:Marlin\nG1 X0 Y0 Z0.2 E1\n;C3_END\nM18\n"
DUAL = b";CREATOR3_NATIVE_V1\n;mode:dual; source:Marlin\n;CREATOR3_DUAL_EXPERIMENTAL_V1\nM118 X5 Y5 Z0.2 T0 T1\nM108 T0\nG1 X0 Y0 E1\nM108 T1\nG92 E0\nG1 X1 Y1 E1\n;C3_END\nM18\n"


class FakePrinter:
    def __init__(self, fault=None, fragment=False, ack_format="N{sequence} ok"):
        self.fault = fault
        self.fragment = fragment
        self.ack_format = ack_format
        self.commands = []
        self.payload = bytearray()
        self.error = None
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.sock.settimeout(10)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.thread.join(12)
        self.sock.close()
        if self.thread.is_alive():
            raise AssertionError("Simulator did not terminate")
        if self.error is not None and exc_type is None:
            raise self.error

    def run(self):
        try:
            conn, _ = self.sock.accept()
            with conn:
                conn.settimeout(8)
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                stream = conn.makefile("rb")
                uploading = 0
                sequence = 0
                while True:
                    if uploading:
                        if self.fault == "v2":
                            block = stream.read(min(uploading, 4096))
                            if not block:
                                break
                            self.payload.extend(block)
                            uploading -= len(block)
                            continue
                        header = stream.read(16)
                        if not header:
                            break
                        magic, number, count, crc = struct.unpack(">4I", header)
                        block = stream.read(4096)
                        assert magic == 0x5a5aa5a5 and number == sequence
                        assert len(block) == 4096 and block[count:] == bytes(4096 - count)
                        assert protocol.binascii.crc32(block[:count]) == crc
                        self.payload.extend(block[:count])
                        uploading -= count
                        ack = (self.ack_format.format(sequence=sequence) + "\r\n").encode()
                        if self.fault == "ack":
                            ack = f"N{sequence + 1} ok\r\n".encode()
                        elif self.fault == "crc":
                            ack = f"N{sequence} error\r\n".encode()
                        self.reply(conn, ack)
                        sequence += 1
                        if self.fault in ("ack", "crc"):
                            break
                        continue
                    line = stream.readline()
                    if not line:
                        break
                    command = line.decode().strip()
                    self.commands.append(command)
                    op = command[1:].split()[0]
                    response = f"CMD {op} Received.\r\n"
                    if op == "M601":
                        control = {"control": "Control failed.", "v1": "Control Success V1.0", "v2": "Control Success V2.0"}
                        response += control.get(self.fault, "Control Success.") + "\r\n"
                    elif op == "M115":
                        response += "Machine Type: " + ("Adventurer 3" if self.fault == "model" else "Creator 3") + "\r\n"
                    elif op == "M119":
                        busy = (
                            self.fault == "busy"
                            or (self.fault == "busy-after-control" and "~M601 S1" in self.commands)
                            or (self.fault == "busy-before-preheat" and self.commands.count("~M119") >= 3)
                        )
                        if self.fault in ("upload-idle", "upload-moving", "upload-progress", "upload-busy-after-control"):
                            active = self.fault == "upload-busy-after-control" and "~M601 S1" in self.commands
                            response += "MachineStatus: BUILDING\r\nMoveMode: " + ("MOVING" if active or self.fault == "upload-moving" else "READY")
                            response += "\r\nStatus: S:1 L:0 J:0 F:0\r\n"
                        else:
                            response += "MachineStatus: " + ("PRINTING" if busy else "READY") + "\r\n"
                    elif op == "M140" and self.fault == "heater":
                        response += "heater command failed\r\n"
                    elif op == "M105":
                        response += "T0:215/215 T1:214/215 B:58/58\r\n"
                    elif op == "M27":
                        response += "SD printing byte 0/1000\r\n" if self.fault in ("upload-idle", "upload-moving", "upload-busy-after-control") else "SD printing byte 20/100\r\n"
                    elif op == "M28":
                        if self.fault == "space":
                            response += "out of space\r\n"
                        else:
                            _, size, name = command[1:].split()
                            uploading = int(size)
                            assert name.startswith("0:/user/") and len(name.encode()) < 36
                            response += f"Writing to file: {name}\r\n"
                    elif op == "M29":
                        response += "save failed\r\n" if self.fault == "save" else "Done saving file.\r\n"
                    elif op == "M602":
                        response += "Control Release.\r\n"
                    self.reply(conn, (response + "ok\r\n").encode())
                stream.close()
        except (OSError, AssertionError, ValueError) as error:
            self.error = error

    def reply(self, conn, response):
        if self.fragment:
            for byte in response:
                conn.sendall(bytes([byte]))
        else:
            conn.sendall(response)


class SendTests(unittest.TestCase):
    def test_preheat_bed_exact_command_and_no_print_or_nozzle_changes(self):
        for temperature in (0, 60, protocol.MAX_BED_TEMPERATURE):
            with self.subTest(temperature=temperature), FakePrinter(fragment=True) as server:
                with protocol.Client("127.0.0.1", threading.Event(), server.port) as client:
                    client.preheat_bed(temperature)
                self.assertEqual(server.commands, [
                    "~M115", "~M119", "~M601 S1", "~M119", "~M119",
                    f"~M140 S{temperature}", "~M602",
                ])

    def test_preheat_bed_invalid_temperature_sends_nothing(self):
        client = protocol.Client("127.0.0.1", threading.Event())
        for temperature in (-1, 121, True, 60.5, "60", "60\nM23"):
            with self.subTest(temperature=temperature), patch.object(client, "command") as command:
                with self.assertRaises(protocol.PrinterError):
                    client.preheat_bed(temperature)
                command.assert_not_called()

    def test_preheat_bed_rechecks_model_control_and_ready(self):
        for fault in ("model", "control", "busy", "busy-after-control", "busy-before-preheat"):
            with self.subTest(fault=fault), FakePrinter(fault=fault) as server:
                with self.assertRaises(protocol.PrinterError):
                    with protocol.Client("127.0.0.1", threading.Event(), server.port) as client:
                        client.preheat_bed(60)
                self.assertFalse(any(command.startswith("~M140") for command in server.commands))

    def test_preheat_bed_rejection_is_explicit_and_control_is_released(self):
        with FakePrinter(fault="heater") as server:
            with self.assertRaisesRegex(protocol.PrinterError, "rejected M140"):
                with protocol.Client("127.0.0.1", threading.Event(), server.port) as client:
                    client.preheat_bed(60)
            self.assertEqual(server.commands[-1], "~M602")
            self.assertEqual(server.commands.count("~M140 S60"), 1)

    def test_dual_requires_stamp_native_selection_and_no_sync_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dual.gcode"
            path.write_bytes(DUAL)
            protocol.validate_gcode(path)
            for invalid in (
                DUAL.replace(b";CREATOR3_DUAL_EXPERIMENTAL_V1\n", b""),
                DUAL.replace(b"T0 T1\n", b"T0 T1 D1\n"),
                DUAL.replace(b"M108 T1", b"T1"),
                DUAL.replace(b"M108 T1", b"M108 T0"),
                DUAL + b"M109 T1\n",
                DUAL + b";CREATOR3_IDEX_CALPAD_V2\n",
                DUAL + b";calPad A start Z0.20\n",
            ):
                with self.subTest(invalid=invalid):
                    path.write_bytes(invalid)
                    with self.assertRaises(protocol.PrinterError):
                        protocol.validate_gcode(path)

    def test_dual_upload_preserves_bytes_and_never_starts_automatically(self):
        with tempfile.TemporaryDirectory() as directory, FakePrinter(fragment=True) as fake:
            path = Path(directory) / "dual.gcode"
            path.write_bytes(DUAL)
            with protocol.Client("127.0.0.1", threading.Event(), fake.port) as client:
                client.upload(path, protocol.transfer_name("alternating heads"), lambda _: None)
            self.assertEqual(fake.payload, DUAL)
            self.assertFalse(any(command.startswith(("~M23", "~M24")) for command in fake.commands))

    def test_short_ascii_unique_name_including_remote_path(self):
        for title in ("very long café display stand model.stl", "你好", r"C:\test\stand.stl", "../../M23 \r\nM24.gcode"):
            a, b = protocol.transfer_name(title), protocol.transfer_name(title)
            self.assertNotEqual(a, b)
            self.assertLessEqual(len(a.encode("ascii")), 25)
            self.assertLess(len(protocol.remote_path(a).encode()), 36)
            self.assertLess(len(a.encode()), 30)
            self.assertRegex(a, r"^[A-Za-z0-9_-]+\.gcode$")

    def test_invalid_names_and_addresses(self):
        for name in ("../test.gcode", "a b.gcode", "test\nM23.gcode", "é.gcode", "x" * 26 + ".gcode"):
            with self.assertRaises(ValueError):
                protocol.remote_path(name)
        for ip in ("", "localhost", "1.2.3.4:8899", "255.255.255.255", "225.0.0.9", "0.0.0.0"):
            with self.assertRaises(ValueError):
                protocol.address(ip)

    def test_crc_and_padding(self):
        block = protocol.packet(7, b"123456789")
        self.assertEqual(struct.unpack(">4I", block[:16]), (0x5a5aa5a5, 7, 9, 0xcbf43926))
        self.assertEqual(len(block), 4112)
        self.assertEqual(block[25:], bytes(4087))
        for data in (b"", bytes(4097)):
            with self.assertRaises(ValueError):
                protocol.packet(0, data)

    def test_discovery_decode(self):
        p = protocol.discovery_reply(b"Creator 3 workshop\0extra", "192.0.2.15")
        self.assertEqual(p.name, "Creator 3 workshop")
        packet = b"Creator 3\0".ljust(128, b"\0") + socket.inet_aton("225.0.0.9") + struct.pack(">4H", 8899, 11121, 12, 2)
        self.assertEqual(protocol.discovery_reply(packet, "192.0.2.15").port, 8899)
        for response in (b"\0", b"\x01bad\0", b"\xff\0"):
            with self.assertRaises((ValueError, UnicodeError)):
                protocol.discovery_reply(response, "192.0.2.15")

    def test_actual_udp_discovery(self):
        stop = threading.Event()
        received = []
        class Datagram:
            def __init__(self):
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            def bind(self, target):
                self.sock.bind((target[0], 0))
            def setsockopt(self, *args):
                pass
            def sendto(self, data, target):
                received.append((data, target))
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reply:
                    reply.sendto(b"Creator 3\0", self.sock.getsockname())
            def setblocking(self, value):
                self.sock.setblocking(value)
            def recvfrom(self, size):
                return self.sock.recvfrom(size)
            def close(self):
                self.sock.close()
        real_socket = socket.socket
        datagram = Datagram()
        with patch.object(protocol.socket, "socket", return_value=datagram):
            # Restore socket only inside sendto to allow the loopback reply.
            original_send = datagram.sendto
            def send(data, target):
                with patch.object(protocol.socket, "socket", real_socket):
                    original_send(data, target)
            datagram.sendto = send
            found, warnings = protocol.discover(["127.0.0.1"], stop, duration=0.15)
        self.assertEqual(found, [protocol.Printer("Creator 3", "127.0.0.1")])
        self.assertFalse(warnings)
        self.assertEqual(received, [(b"Hello World!", ("225.0.0.9", 19000))])

    def test_reject_blocked_raw_and_old_idex_before_network_upload(self):
        variants = (b";CREATOR3_EXPORT_BLOQUE\nM18\n", b"G1 X0 E1\n", NATIVE.replace(b"solo", b"duplicate"),
                    NATIVE + b";NE PAS IMPRIMER\n", NATIVE + b";DO NOT PRINT THIS FILE\n")
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "job.gcode"
            for content in variants:
                path.write_bytes(content)
                with self.subTest(content=content), self.assertRaises(ValueError):
                    protocol.validate_gcode(path)

    def test_idex_requires_exact_calpads(self):
        markers = "\n".join(
            line for block in ("A", "B", "SA", "SB")
            for line in (f";calPad {block} start Z0.20", f";calPad {block} end")
        ).encode()
        data = NATIVE.replace(b"solo", b"duplicate") + b";CREATOR3_IDEX_CALPAD_V2\n" + markers + b"\n"
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "job.gcode"
            path.write_bytes(data)
            protocol.validate_gcode(path)
            path.write_bytes(data.replace(b";calPad SB end", b";calPad SB absent"))
            with self.assertRaises(ValueError):
                protocol.validate_gcode(path)

    def test_fragmented_tcp_upload_and_no_automatic_start(self):
        with tempfile.TemporaryDirectory() as root, FakePrinter(fragment=True) as server:
            path = Path(root) / "job.gcode"
            content = NATIVE + (b";test\n" * 820)
            path.write_bytes(content)
            progress = []
            with protocol.Client("127.0.0.1", threading.Event(), server.port) as client:
                client.upload(path, protocol.transfer_name("two feet"), progress.append)
            self.assertEqual(server.payload, content)
            self.assertEqual(progress[-1], 100)
            self.assertEqual(server.commands[-2:], ["~M29", "~M602"])
            self.assertFalse(any(c.startswith(("~M23", "~M24")) for c in server.commands))

    def test_upload_accepts_zero_padded_block_numbers(self):
        for ack_format in (
            "N{sequence} ok", "N{sequence:04d} ok", "N{sequence:08d} ok",
            "N{sequence} ok.", "N{sequence:04d} ok.", "N{sequence:08d} ok.",
            "N000{sequence} ok.",
        ):
            with self.subTest(ack_format=ack_format), tempfile.TemporaryDirectory() as root:
                content = NATIVE + b";padding\n" * 6000
                path = Path(root) / "job.gcode"
                path.write_bytes(content)
                progress = []
                with FakePrinter(fragment=True, ack_format=ack_format) as server:
                    with protocol.Client("127.0.0.1", threading.Event(), server.port) as client:
                        client.upload(path, "test.gcode", progress.append)
                    self.assertEqual(server.payload, content)
                    self.assertEqual(server.commands, [
                        "~M115", "~M119", "~M601 S1", "~M119",
                        f"~M28 {len(content)} 0:/user/test.gcode", "~M29", "~M602",
                    ])
                self.assertEqual(progress[-1], 100)

    def test_upload_rejects_wrong_or_malformed_padded_acknowledgements(self):
        for ack_format, block in (
            ("N0001 ok", 0), ("N0010 ok", 0), ("N0000 error", 0), ("N0000 ok extra", 0),
            ("prefix N0000 ok", 0), ("N+0000 ok", 0), ("N0000.0 ok", 0),
            ("N ok", 0), ("ok", 0), ("N0000 ok", 1),
            ("N0001 ok.", 0), ("N0000 ok..", 0), ("N0000 ok. extra", 0),
            ("N0000 error.", 0), ("N0000 ok.", 1),
        ):
            with self.subTest(ack_format=ack_format), tempfile.TemporaryDirectory() as root:
                path = Path(root) / "job.gcode"
                path.write_bytes(NATIVE + b";padding\n" * 1000)
                progress = []
                with FakePrinter(ack_format=ack_format) as server:
                    with self.assertRaisesRegex(protocol.PrinterError, f"Block {block} not acknowledged"):
                        with protocol.Client("127.0.0.1", threading.Event(), server.port) as client:
                            client.upload(path, "test.gcode", progress.append)
                    self.assertNotIn("~M29", server.commands)
                    self.assertFalse(any(c.startswith(("~M23", "~M24")) for c in server.commands))
                self.assertNotIn(100, progress)

    def test_explicit_start_separate_connection(self):
        with FakePrinter() as server:
            name = protocol.transfer_name("pieds")
            with protocol.Client("127.0.0.1", threading.Event(), server.port) as client:
                client.start(name)
            self.assertIn("~M23 " + protocol.remote_path(name), server.commands)
            self.assertNotIn("~M24", server.commands)

    def test_confirmed_upload_start_accepts_only_idle_building(self):
        with FakePrinter(fault="upload-idle") as server:
            with protocol.Client("127.0.0.1", threading.Event(), server.port, pending_start="test.gcode") as client:
                client.start("test.gcode")
            self.assertEqual(sum(c.startswith("~M23 ") for c in server.commands), 1)
            self.assertEqual(server.commands.count("~M27"), 3)
            self.assertNotIn("~M24", server.commands)
        for fault in ("upload-idle", "upload-moving", "upload-progress", "upload-busy-after-control", "busy"):
            with self.subTest(fault=fault), FakePrinter(fault=fault) as server:
                with self.assertRaises(protocol.PrinterError):
                    options = {} if fault == "upload-idle" else {"pending_start": "test.gcode"}
                    with protocol.Client("127.0.0.1", threading.Event(), server.port, **options) as client:
                        client.start("test.gcode")
                self.assertFalse(any(c.startswith("~M23 ") for c in server.commands))

    def test_pending_upload_state_requires_all_controller_evidence(self):
        status = "MachineStatus: BUILDING\nMoveMode: READY\nStatus: S:1 L:0 J:0 F:0"
        parsed = protocol.parse_status(status, "", "SD printing byte 0/1000")
        self.assertTrue(protocol.pending_upload_idle(parsed))
        for change in (
            {"state": "PAUSED"}, {"state": "BUILDING_FROM_SD"},
            {"move_mode": None}, {"move_mode": "MOVING"},
            {"controller": None}, {"controller": {"S": 1, "L": 0, "J": 1, "F": 0}},
            {"print_bytes": None}, {"print_bytes": (1, 1000)}, {"print_bytes": (0, 100000)},
        ):
            with self.subTest(change=change):
                self.assertFalse(protocol.pending_upload_idle({**parsed, **change}))

    def test_pending_start_connection_cannot_send_other_jobs_or_heat(self):
        with FakePrinter(fault="upload-idle") as server:
            with protocol.Client("127.0.0.1", threading.Event(), server.port, pending_start="test.gcode") as client:
                for operation in (
                    lambda: client.start("different.gcode"),
                    lambda: client.preheat_bed(60),
                    lambda: client.upload(Path("unused.gcode"), "test.gcode", lambda value: None),
                ):
                    with self.assertRaises(protocol.PrinterError):
                        operation()
            self.assertFalse(any(c.startswith(("~M23", "~M140", "~M28")) for c in server.commands))

    def test_status_is_read_only_with_temperatures_and_progress(self):
        with FakePrinter() as server:
            status = protocol.read_status(protocol.Printer("Creator 3", "127.0.0.1", server.port), threading.Event())
            self.assertEqual(status["state"], "READY")
            self.assertEqual(status["left"], "214/215")
            self.assertEqual(status["right"], "215/215")
            self.assertEqual(status["bed"], "58/58")
            self.assertEqual(status["print_progress"], 20)
            self.assertEqual(server.commands, ["~M115", "~M119", "~M105", "~M27"])

    def test_native_version_negotiates_legacy_or_raw_transfer(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "large.gcode"
            data = NATIVE + b";tail\n" * 2500
            source.write_bytes(data)
            for version in ("v1", "v2"):
                with self.subTest(version=version), FakePrinter(fault=version) as server:
                    with protocol.Client("127.0.0.1", threading.Event(), server.port) as client:
                        self.assertEqual(client.raw_transfer, version == "v2")
                        client.upload(source, protocol.transfer_name("native-v2"), lambda _: None)
                    self.assertEqual(bytes(server.payload), data)
                    self.assertIn("~M29", server.commands)
                    self.assertNotIn("~M23", server.commands)

    def test_busy_guards_before_and_after_control(self):
        for fault in ("busy", "busy-after-control"):
            with self.subTest(fault=fault), FakePrinter(fault=fault) as server:
                with self.assertRaises(ValueError):
                    with protocol.Client("127.0.0.1", threading.Event(), server.port):
                        self.fail("Busy printer must not be acquired for uploading")
                self.assertFalse(any(c.startswith("~M28") for c in server.commands))
                self.assertEqual("~M601 S1" in server.commands, fault == "busy-after-control")
                self.assertEqual("~M602" in server.commands, fault == "busy-after-control")

    def test_printing_status_does_not_take_control_or_transfer(self):
        with FakePrinter(fault="busy") as server:
            status = protocol.read_status(protocol.Printer("Creator 3", "127.0.0.1", server.port), threading.Event())
            self.assertEqual(status["state"], "PRINTING")
            self.assertNotIn("~M601 S1", server.commands)
            self.assertFalse(any(c.startswith("~M28") for c in server.commands))

    def test_unavailable_telemetry_is_not_reported_as_zero(self):
        status = protocol.parse_status("MachineStatus: PAUSED", "", "SD printing byte 0/0")
        self.assertEqual(status["left"], "--")
        self.assertIsNone(status["print_progress"])
        with self.assertRaises(ValueError):
            protocol.parse_status("unknown", "", "")

    def test_protocol_errors_never_send_start(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "job.gcode"
            path.write_bytes(NATIVE * 90)
            for fault in ("control", "model", "busy", "space", "ack", "crc", "save"):
                with self.subTest(fault=fault), FakePrinter(fault=fault) as server:
                    with self.assertRaises(ValueError):
                        with protocol.Client("127.0.0.1", threading.Event(), server.port) as client:
                            client.upload(path, protocol.transfer_name("pieds"), lambda value: None)
                    self.assertFalse(any(c.startswith(("~M23", "~M24")) for c in server.commands))
                    if fault in ("ack", "crc"):
                        self.assertNotIn("~M29", server.commands)

    def test_cancel_before_connect(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(protocol.socket, "create_connection") as connect:
            with self.assertRaises(protocol.Cancelled):
                with protocol.Client("127.0.0.1", cancel):
                    pass
            connect.assert_not_called()

    def test_cancel_mid_transfer_closes_without_finishing_or_start(self):
        with tempfile.TemporaryDirectory() as root, FakePrinter() as server:
            path = Path(root) / "job.gcode"
            path.write_bytes(NATIVE * 900)
            cancel = threading.Event()
            with self.assertRaises(protocol.Cancelled):
                with protocol.Client("127.0.0.1", cancel, server.port) as client:
                    client.upload(path, protocol.transfer_name("pieds"), lambda value: cancel.set())
            self.assertNotIn("~M29", server.commands)
            self.assertNotIn("~M602", server.commands)
            self.assertLess(len(server.payload), path.stat().st_size)

    def test_large_file_exact_and_progress_bounded(self):
        with tempfile.TemporaryDirectory() as root, FakePrinter() as server:
            path = Path(root) / "job.gcode"
            path.write_bytes(NATIVE + b";large\n" * 1500000)
            progress = []
            with protocol.Client("127.0.0.1", threading.Event(), server.port) as client:
                client.upload(path, protocol.transfer_name("large"), progress.append)
            self.assertEqual(hashlib.sha256(server.payload).digest(), hashlib.sha256(path.read_bytes()).digest())
            self.assertLessEqual(len(progress), 101)


if __name__ == "__main__":
    unittest.main(verbosity=2)
