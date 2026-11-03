import importlib.util
import json
import re
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
stub = types.ModuleType("C3PublicTests.Script")
stub.Script = object
sys.modules[stub.__name__] = stub
spec = importlib.util.spec_from_file_location(
    "C3PublicTests.scripts.Creator3Native", ROOT / "cura" / "scripts" / "Creator3Native.py"
)
native = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = native
spec.loader.exec_module(native)


def source(tool=0, extra="", x=5):
    return [
        f";FLAVOR:Marlin\nT{tool}\nM82\nM104 S215.0\nM109 S215.0\n"
        f";C3_START\nM108 T{tool}\n;C3_BODY\n"
        f"G92 E0\n;LAYER:0\nG0 F4800 X{-x} Y-5 Z0.24\n"
        f"G1 F3300 X{x} Y5 E1.23456\n{extra}"
        ";C3_END\nM104 S0\nM140 S0\n"
    ]


def pads():
    # Synthetic parser fixtures only: these are not printable calibration paths.
    return tuple(
        line for block in ("A", "B", "SA", "SB")
        for line in (
            f";calPad {block} start Z0.20", ";:G1 X11.40 Y-10.80 F3600",
            ";:G1 X12.40 Y-10.80 E1 F3600", ";:G92 E0",
            ";:G1 X12.40 Y-9.80 E1 F3600", f";calPad {block} end",
        )
    )


def settings(mode="solo", calibrated=False):
    return native.PrintConfig(
        mode, 58, (215, 217), idex_calibrated=calibrated,
        calpads=pads() if mode != "solo" and calibrated else (),
    )


def dual_source(first=0, extra=""):
    second = 1 - first
    return source(first, (
        f"G1 F1800 E0\nG92 E0\nT{second}\nG92 E0\nM109 S217\n"
        "G0 F3600 Z0.24\nG0 X-5 Y5\nG1 F1800 E1.3\n"
        "G1 F1200 X5 Y-5 E2.53456\n"
        f"{extra}G1 F1800 E1.23456\nG92 E0\nT{first}\nG92 E0\n"
        "G0 F3600 Z0.44\nG0 X-5 Y-5\nG1 F1800 E1.3\n"
        "G1 F1200 X5 Y5 E2.53456\n"
    ))


def dual_settings():
    return native.PrintConfig("dual", 58, (215, 217), dual_calibrated=True)


class NativeTests(unittest.TestCase):
    def test_dual_native_startup_and_swapped_assignments(self):
        for first in (0, 1):
            with self.subTest(first=first):
                output = native.convert(dual_source(first), dual_settings())[0]
                self.assertIn("M118 X5.00 Y5.00 Z0.44 T0 T1\n", output)
                self.assertIn("M104 S215 T0\nM104 S217 T1", output)
                self.assertIn("M6 T0\nM6 T1", output)
                self.assertIn(f"M108 T{1-first}\nM107\nG92 E0\nM104 S217 T{1-first}\nM6 T{1-first}", output)
                self.assertIn(native.DUAL_STAMP, output)
                self.assertNotRegex(output, r"(?m)^(?:T[01]|M109|;calPad)\b")

    def test_dual_requires_opt_in_and_both_tools(self):
        with self.assertRaisesRegex(ValueError, "Experimental"):
            native.convert(dual_source(), native.PrintConfig("dual", 58, (215, 217)))
        with self.assertRaisesRegex(ValueError, "both nozzles"):
            native.convert(source(), dual_settings())
        with self.assertRaisesRegex(ValueError, "nonzero"):
            native.convert(dual_source(), native.PrintConfig("dual", 58, (215, 0), dual_calibrated=True))

    def test_dual_independent_e_resets_and_precise_extrusion(self):
        output = native.convert(dual_source(), dual_settings())[0]
        expected = [
            params["E"] for line in "".join(dual_source()).splitlines()
            for op, params in [native.command(line)]
            if op in ("G0", "G1") and "E" in params
        ]
        actual = [
            params["E"] for line in output.splitlines()
            if line.startswith("G1 ")
            for op, params in [native.command(line)]
            if op == "G1" and "E" in params
        ]
        self.assertEqual(actual, expected)
        self.assertIn("E2.53456", output)
        self.assertIn("G92 E0\nM108 T0", output)
        precise = dual_source()[0].replace("E2.53456", "E2.53456001234567")
        self.assertIn("E2.53456001234567", native.convert([precise], dual_settings())[0])

    def test_dual_z_is_standalone_capped_and_xy_feed_is_restored(self):
        output = native.convert(dual_source(), dual_settings())[0]
        self.assertIn("G1 Z0.24 F420\nG1 F3600 X-5 Y5", output)
        self.assertIn("G1 Z0.44 F420\nG1 F3600 X-5 Y-5", output)
        for line in output.splitlines():
            if not line.startswith("G1 "):
                continue
            op, params = native.command(line)
            if op == "G1" and "Z" in params:
                self.assertLessEqual(params["F"], 420)
                self.assertEqual(set(params), {"Z", "F"})
        with self.assertRaisesRegex(ValueError, "coordinated"):
            native.convert(dual_source(extra="G1 X5 Z0.3 E3\n"), dual_settings())

    def test_dual_switch_requires_reset_and_new_head_xy_position(self):
        for old, new, error in (
            ("T1\nG92 E0", "T1", "reset absolute E"),
            ("G0 X-5 Y5", "G0 Y5", "before XY positioning"),
            ("T1\n", "T1 F600\n", "additional parameters"),
        ):
            with self.subTest(error=error):
                raw = [dual_source()[0].replace(old, new)]
                with self.assertRaisesRegex(ValueError, error):
                    native.convert(raw, dual_settings())

    def test_dual_temperature_and_fan_addressing(self):
        output = native.convert(dual_source(extra="M104 T0 S190\nM104 S218\nM106 P0 S128\nM106 P1 S200\n"), dual_settings())[0]
        self.assertIn("M104 S190 T0\nM104 S218 T1", output)
        self.assertIn("M106 S200", output)
        self.assertIn("M108 T0\nM106 S128", output)

    def test_dual_uses_full_bed_and_does_not_relax_old_modes(self):
        output = native.convert([dual_source()[0].replace("X5 Y5", "X90 Y5")], dual_settings())[0]
        self.assertIn("X90.00", output)
        with self.assertRaisesRegex(ValueError, "outside"):
            native.convert([dual_source()[0].replace("X5 Y5", "X151 Y5")], dual_settings())
        for mode in ("solo", "mirror", "duplicate"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                native.convert(dual_source(), settings(mode, mode != "solo"))

    def test_dual_idempotence_and_stamp_confirmation(self):
        config = dual_settings()
        output = native.convert(dual_source(), config)
        self.assertEqual(native.convert(output, config), output)
        with self.assertRaisesRegex(ValueError, "confirmation"):
            native.convert(output, native.PrintConfig("dual", 58, (215, 217)))
        with self.assertRaisesRegex(ValueError, "confirmation"):
            native.convert([output[0].replace(native.DUAL_STAMP, "")], config)

    def test_right_centered_and_native(self):
        output = native.convert(source(), settings())[0]
        self.assertIn("M108 T0", output)
        self.assertIn("M104 S215 T0\nM104 S0 T1", output)
        self.assertIn("M118 X5.00 Y5.00 Z0.24 T0", output)
        self.assertIn("G1 F3300 X5 Y5 E1.23456", output)
        self.assertNotRegex(output, r"(?m)^M109 .*S|^T[01]$")
        self.assertTrue(output.endswith("M652\nG91\nM18\n"))

    def test_left_temperature_and_selection(self):
        output = native.convert(source(1), settings())[0]
        self.assertIn("M108 T1", output)
        self.assertIn("M104 S0 T0\nM104 S217 T1", output)
        self.assertIn("M6 T1", output)
        self.assertNotIn("M6 T0", output)

    def test_modes_have_native_calibration_and_preserve_model(self):
        raw = source(extra="G92 E0\nG1 X1 Y2 E2\n")
        for mode, number in (("mirror", 1), ("duplicate", 2)):
            config = settings(mode, True)
            output = native.convert(raw, config)[0]
            self.assertIn(f"T0 T1 D{number}", output)
            self.assertEqual(output.count(f"\nM109 T{number}\n"), 1)
            self.assertIn(f";calPad SB end\nM109 T{number}", output)
            self.assertIn(native.IDEX_STAMP, output)
            self.assertNotIn("\nG0 ", output)
            self.assertEqual(native.extract_calpads(output, config), config.calpads)
            original = raw[0].split(";LAYER:0\n")[1].split(";C3_END")[0]
            converted = output.split(";LAYER:0\n")[1].split(";C3_END")[0]
            self.assertEqual(re.sub(r"(?m)^G0\b", "G1", original), converted)

    def test_idex_requires_confirmation_calpads_and_right_pilot(self):
        for mode in ("mirror", "duplicate"):
            for data, config in (
                (source(), settings(mode)),
                (source(), native.PrintConfig(mode, 58, (215, 215), idex_calibrated=True)),
                (source(1), settings(mode, True)),
            ):
                with self.subTest(mode=mode), self.assertRaises(ValueError):
                    native.convert(data, config)

    def test_conversion_is_idempotent_but_mode_changes_require_reslicing(self):
        for mode in ("solo", "mirror", "duplicate"):
            config = settings(mode, mode != "solo")
            result = native.convert(source(), config)
            self.assertEqual(native.convert(result, config), result)
        with self.assertRaisesRegex(ValueError, "slice again"):
            native.convert(native.convert(source(), settings()), settings("mirror", True))

    def test_old_idex_stamp_cannot_bypass_calibration(self):
        for marker in (";mode:duplicate; source:Marlin\n", "M118 X20 Y20 Z10 T0 T1 D2\n", "M109 T1\n"):
            with self.assertRaises(ValueError):
                native.convert([native.STAMP + "\n" + marker], settings())

    def test_invalid_inputs_are_rejected(self):
        invalid = (
            "G91\nG1 X1\n", "M83\n", "G92 X0\n", "T1\n", "T1\nG1 X1 E2\n",
            "M109 T1\n", "G28\n", "G2 X1 Y2 I3 J4 E1\n", "M104 S999 T0\n",
            "M106 S256\n", "G1 XNaN\n", "G1 X1 X2\n", "G0 Y126\n",
            "G0 Z201\n", "G0 Z-1\n", "G0 X151\n",
        )
        for extra in invalid:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                native.convert(source(extra=extra), settings())
        with self.assertRaises(ValueError):
            native.convert([source()[0].replace(";C3_BODY\n", "")], settings())
        with self.assertRaises(ValueError):
            native.convert(source(), settings("unknown"))

    def test_idex_width_boundary(self):
        native.convert(source(x=71.5), settings("mirror", True))
        with self.assertRaises(ValueError):
            native.convert(source(x=71.51), settings("mirror", True))

    def test_native_temperature_waits_and_fans(self):
        output = native.convert(source(1, "M105\nM109 S218.0 T1\nM190 S60.0\nM106 S127.5 P1\nM107 P1\n"), settings())[0]
        self.assertIn("M104 S218 T1\nM6 T1", output)
        self.assertIn("M140 S60 T0\nM7 T0", output)
        self.assertIn("M106 S128\nM107", output)
        self.assertNotIn("\nM105\n", output)
        self.assertNotRegex(output, r"(?m)^M(?:104|140) S\d+\.")

    def test_calpad_validation_rejects_unsafe_records(self):
        text = "\n".join(pads())
        variants = (
            text.replace(";calPad B end", ";calPad B missing"),
            text.replace("Z0.20", "Z0.30", 1),
            text.replace(";:G1 ", "G1 ", 1), text.replace(";:G1 ", ";:G0 ", 1),
            text.replace("X11.40", "X100.40", 1),
            text.replace("Y-10.80", "Y-200", 1), text.replace("F3600", "F0", 1),
            text.replace("X11.40", "XNaN", 1),
            text.replace(";:G92 E0", ";:G92 X0", 1),
            text.replace(";:G92 E0", ";:G92 E1", 1),
        )
        for variant in variants:
            with self.subTest(variant=variant[:50]), self.assertRaises(ValueError):
                native.extract_calpads(variant, settings("duplicate", True))

    def test_raft_support_envelope_includes_bead_width_and_travels(self):
        mesh = native.calibration_envelope(
            source(extra=";LAYER:-3\nG0 X-25 Y-20\nG1 X25 Y20 E2\n;TYPE:SUPPORT\nG0 X30 Y-15\nG1 Y22 E3\n"),
            0.6, settings("duplicate", True),
        )
        self.assertEqual(struct.unpack_from("<I", mesh, 80)[0], 12)
        vertices = [struct.unpack_from("<fff", mesh, offset + vertex)
                    for offset in range(84, len(mesh), 50) for vertex in (12, 24, 36)]
        for axis, expected in ((0, (-25.6, 30.6)), (1, (-20.6, 22.6)), (2, (0, 1))):
            self.assertAlmostEqual(min(v[axis] for v in vertices), expected[0], places=4)
            self.assertAlmostEqual(max(v[axis] for v in vertices), expected[1], places=4)

    def test_envelope_rejects_missing_invalid_and_out_of_bounds_paths(self):
        for data, padding in (
            (["G1 X0 Y0"], 0.2), (source(), float("nan")), (source(), 0),
            (source(extra="G1 XNaN\n"), 0.2), (source(extra="G1 Y124.8 E2\n"), 0.4),
            (source(extra="G1 X71.4 E2\n"), 0.4),
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                native.calibration_envelope(data, padding, settings("duplicate", True))

    def test_raft_support_paths_preserved_and_dual_material_rejected(self):
        data = source(extra=";LAYER:-3\n;TYPE:SUPPORT\nG0 Z0.3 X-12 Y-10\nG1 X12 Y10 E2\n")
        for mode in ("duplicate", "mirror"):
            output = native.convert(data, settings(mode, True))[0]
            self.assertIn(";LAYER:-3\n;TYPE:SUPPORT\nG1 Z0.3 X-12 Y-10\nG1 X12 Y10 E2", output)
            with self.assertRaises(ValueError):
                native.convert(source(extra="T1\nG1 X12 E2\n"), settings(mode, True))

    def test_missing_engine_and_engine_errors_are_explicit(self):
        config = settings("duplicate", True)
        with self.assertRaisesRegex(ValueError, "engine required"):
            native.native_calpads(b"", config, ROOT / "absent.exe")
        with tempfile.TemporaryDirectory() as directory:
            engine = Path(directory) / "engine"
            engine.touch()
            with patch.object(native.subprocess, "run", return_value=types.SimpleNamespace(returncode=1, stdout="failure detail")):
                with self.assertRaisesRegex(ValueError, "failure detail"):
                    native.native_calpads(b"", config, engine)
            with patch.object(native.subprocess, "run", side_effect=native.subprocess.TimeoutExpired("engine", 120)):
                with self.assertRaises(native.subprocess.TimeoutExpired):
                    native.native_calpads(b"", config, engine)

    def test_execute_fail_closed_replaces_unconverted_paths(self):
        values = {
            "machine_gcode_flavor": "RepRap (Marlin/Sprinter)", "machine_center_is_zero": True,
            "relative_extrusion": False, "machine_width": 300, "machine_depth": 250,
            "machine_height": 200, "material_bed_temperature_layer_0": 58,
        }
        trains = []
        for position in (0, 1):
            train = Mock()
            train.getMetaDataEntry.return_value = str(position)
            train.getProperty.side_effect = lambda key, prop: {
                "machine_nozzle_offset_x": 0, "machine_nozzle_offset_y": 0,
                "material_print_temperature_layer_0": 215,
            }[key]
            trains.append(train)
        stack = Mock(extruderList=trains)
        stack.getBottom.return_value.getId.return_value = "creator3_personal"
        stack.getProperty.side_effect = lambda key, prop: values[key]
        app = Mock()
        app.getGlobalContainerStack.return_value = stack
        application = Mock()
        application.getInstance.return_value = app
        modules = {}
        logger, message = Mock(), Mock()
        for name, symbol, value in (
            ("UM.Application", "Application", application), ("UM.Logger", "Logger", logger),
            ("UM.Message", "Message", message),
        ):
            module = types.ModuleType(name)
            setattr(module, symbol, value)
            modules[name] = module
        script = native.Creator3Native()
        script.getSettingValueByKey = lambda key: {"mode": "duplicate", "idex_calibrated": False, "dual_calibrated": False}[key]
        data = source()
        with patch.dict(sys.modules, modules), patch.object(native, "scene_calpads", side_effect=ValueError("Calibration unconfirmed")):
            with self.assertRaisesRegex(ValueError, "Calibration unconfirmed"):
                script.execute(data)
        self.assertIn("CREATOR3_EXPORT_BLOQUE", data[0])
        self.assertIn("DO NOT PRINT THIS FILE", data[0])
        self.assertNotRegex(data[0], r"(?m)^G")
        logger.logException.assert_called_once()
        message.return_value.show.assert_called_once()

    def test_distribution_definitions_and_default_mode(self):
        schema = json.loads(native.Creator3Native().getSettingDataString())
        self.assertEqual(schema["settings"]["mode"]["default_value"], "solo")
        self.assertFalse(schema["settings"]["idex_calibrated"]["default_value"])
        self.assertFalse(schema["settings"]["dual_calibrated"]["default_value"])
        machine = json.loads((ROOT / "cura" / "definitions" / "creator3_personal.def.json").read_text())
        self.assertEqual(machine["overrides"]["machine_extruder_count"]["default_value"], 2)
        self.assertTrue(machine["overrides"]["machine_center_is_zero"]["default_value"])
        self.assertFalse(machine["overrides"]["machine_use_extruder_offset_to_offset_coords"]["default_value"])
        for name, position in (("right", 0), ("left", 1)):
            train = json.loads((ROOT / "cura" / "extruders" / f"creator3_personal_{name}.def.json").read_text())
            self.assertEqual(train["overrides"]["extruder_nr"]["default_value"], position)

    @unittest.skipUnless(native.ENGINE.is_file(), "Optional local FlashPrint engine is not installed")
    def test_real_engine_calpads_and_bed_clipping(self):
        config = settings("duplicate", True)
        mesh = native.calibration_envelope(source(extra="G1 X20 Y123 E2\nG1 X-20 Y-123 E3\n"), 0.4, config)
        for mode in ("duplicate", "mirror"):
            generated = native.native_calpads(mesh, native.replace(config, mode=mode))
            ys = [params["Y"] for line in generated if line.startswith(";:")
                  for _, params in [native.command(line[2:])] if "Y" in params]
            self.assertAlmostEqual(min(ys), -124.8)
            self.assertAlmostEqual(max(ys), 124.8)
            self.assertIn(";:G92 E0", generated)


if __name__ == "__main__":
    unittest.main(verbosity=2)
