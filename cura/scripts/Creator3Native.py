import json
import math
import re
import struct
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from ..Script import Script


START = ";C3_BODY"
END = ";C3_END"
STAMP = ";CREATOR3_NATIVE_V1"
IDEX_STAMP = ";CREATOR3_IDEX_CALPAD_V2"
DUAL_STAMP = ";CREATOR3_DUAL_EXPERIMENTAL_V1"
ENGINE = Path(r"C:\Program Files\FlashForge\FlashPrint 5\engine\ffslicer.exe")
NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
PARAMETER = re.compile(rf"([A-Z])({NUMBER})(?=\s|$)")


@dataclass(frozen=True)
class PrintConfig:
    mode: str
    bed: float
    nozzle: tuple[float, float]
    width: float = 300
    depth: float = 250
    height: float = 200
    idex_calibrated: bool = False
    calpads: tuple[str, ...] = ()
    dual_calibrated: bool = False


def idex_half_width(config: PrintConfig) -> float:
    return min(config.width / 4, 71.5)


def extract_calpads(text: str, config: PrintConfig) -> tuple[str, ...]:
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(";calPad A start")]
    ends = [i for i, line in enumerate(lines) if line == ";calPad SB end"]
    if len(starts) != 1 or len(ends) != 1 or ends[0] <= starts[0]:
        raise ValueError("Four complete native calPad blocks are required")
    records = lines[starts[0]:ends[0] + 1]
    markers = [line for line in records if line.startswith(";calPad")]
    expected = [
        marker for block in ("A", "B", "SA", "SB")
        for marker in (f";calPad {block} start Z0.20", f";calPad {block} end")
    ]
    if markers != expected:
        raise ValueError("Invalid native calPad order or height")
    count = 0
    first = True
    extrusion = False
    for line in records:
        if line.startswith(";calPad") and " start " in line:
            count, first, extrusion = 0, True, False
        elif line.startswith(";calPad") and line.endswith(" end"):
            if count < 2 or not extrusion:
                raise ValueError("Empty calPad block or missing extrusion")
        elif line.startswith(";:"):
            op, params = command(line[2:])
            if op == "G92" and params == {"E": 0} and not first:
                continue
            if op != "G1" or set(params) - {"X", "Y", "E", "F"}:
                raise ValueError("Unsupported calPad command")
            if first and (not {"X", "Y", "F"} <= params.keys() or "E" in params):
                raise ValueError("Missing initial calPad XY positioning")
            if abs(params.get("X", 0)) + 0.2 > idex_half_width(config) + 0.001:
                raise ValueError("CalPad outside the IDEX X area: recenter or scale down the model")
            if abs(params.get("Y", 0)) + 0.2 > config.depth / 2 + 0.001:
                raise ValueError("CalPad outside the bed Y area: recenter or scale down the model")
            if "F" in params and not 0 < params["F"] <= 12000:
                raise ValueError("Invalid calPad speed")
            extrusion = extrusion or params.get("E", 0) > 0
            count += 1
            first = False
        elif not line.startswith(";") or line.startswith(";:"):
            raise ValueError("Unexpected active command in calPads")
    return tuple(records)


def native_calpads(mesh: bytes, config: PrintConfig, engine: Path = ENGINE) -> tuple[str, ...]:
    if config.mode not in ("mirror", "duplicate"):
        raise ValueError("calPad generation is only available in IDEX modes")
    if any(not math.isfinite(value) or value <= 0 for value in (config.width, config.depth, config.height)):
        raise ValueError("Invalid build volume dimensions")
    if not engine.is_file():
        raise ValueError(f"FlashPrint engine required for IDEX calibration: {engine}")
    # The native engine clips pad centrelines to the platform: reserve the 0.2 mm bead radius.
    settings = f"""gcodeFlavor = flashforge
startEndFlavor = creator3
layerHeight = 0.2
firstLayerHeight = 0.2
pathWidth = 0.4
filamentDiameter0 = 1.75
filamentDiameter1 = 1.75
extrusionRatio = 1.0
firstExtrusionRatio = 1.0
travelSpeed = 60
[dimension]
enable = false
[support]
enable = false
[preExtrude]
enable = false
[shell]
startClosestX0 = 0
startClosestY0 = 0
[platform]
sizeX = {config.width}
sizeY = {config.depth - 0.4}
sizeZ = {config.height}
[calPad]
enable = true
layerHeight = 0.2
margin = 5
density = 0.3
speed = 80
[dittoPrint]
enable = true
mirror = {"true" if config.mode == "mirror" else "false"}
extruderGap = 34
"""
    with tempfile.TemporaryDirectory(prefix="creator3-calpad-") as directory:
        root = Path(directory)
        model = root / "scene.stl"
        model.write_bytes(mesh)
        cfg = root / "calpad.cfg"
        cfg.write_text(settings, encoding="ascii")
        output = root / "native.gcode"
        result = subprocess.run(
            [str(engine), "--config", str(cfg), "--model", "file:" + str(model),
             "--gcode", str(output)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120,
            encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0 or not output.is_file():
            raise ValueError("FlashPrint calPad generation failed: " + result.stdout[-1500:])
        return extract_calpads(output.read_text(encoding="utf-8"), config)


def calibration_envelope(data: list[str], padding: float, config: PrintConfig) -> bytes:
    text = "".join(data)
    if text.splitlines().count(START) != 1 or text.splitlines().count(END) != 1:
        raise ValueError("IDEX envelope requires this integration's start/end G-code")
    if not math.isfinite(padding) or padding <= 0:
        raise ValueError("Invalid line width for IDEX calibration")
    body = text.split(START + "\n", 1)[1].split(END + "\n", 1)[0]
    limits = {"X": [math.inf, -math.inf], "Y": [math.inf, -math.inf]}
    for line in body.splitlines():
        op, params = command(line)
        if op in ("G0", "G1"):
            for axis in limits:
                if axis in params:
                    limits[axis][0] = min(limits[axis][0], params[axis])
                    limits[axis][1] = max(limits[axis][1], params[axis])
    if any(not math.isfinite(value) for bounds in limits.values() for value in bounds):
        raise ValueError("IDEX envelope: missing XY paths")
    x0, x1 = limits["X"][0] - padding, limits["X"][1] + padding
    y0, y1 = limits["Y"][0] - padding, limits["Y"][1] + padding
    if max(abs(x0), abs(x1)) > idex_half_width(config) or max(abs(y0), abs(y1)) > config.depth / 2:
        raise ValueError("Raft/supports outside the IDEX area, including bead width: recenter or scale down the model")
    # A thin solid covers the projected Cura paths, including generated trees and raft.
    vertices = [
        (x0, y0, 0), (x1, y0, 0), (x1, y1, 0), (x0, y1, 0),
        (x0, y0, 1), (x1, y0, 1), (x1, y1, 1), (x0, y1, 1),
    ]
    faces = [
        (0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
        (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
        (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7),
    ]
    mesh = bytearray(b"Creator3 Cura path envelope".ljust(80, b"\0"))
    mesh.extend(struct.pack("<I", len(faces)))
    for face in faces:
        points = [coordinate for index in face for coordinate in vertices[index]]
        mesh.extend(struct.pack("<12fH", 0, 0, 0, *points, 0))
    return bytes(mesh)


def scene_calpads(app, stack, trains, config: PrintConfig, data: list[str]) -> tuple[str, ...]:
    import io
    from UM.Mesh.MeshWriter import MeshWriter
    from UM.Scene.Iterator.DepthFirstIterator import DepthFirstIterator

    if not config.idex_calibrated:
        raise ValueError("First verify single-extruder centering and calibrate both nozzles in FlashPrint.")
    if stack.getProperty("print_sequence", "value") != "all_at_once":
        raise ValueError("IDEX requires printing all models at once, not one at a time.")
    envelope_required = stack.getProperty("adhesion_type", "value") != "none"
    for train in trains:
        if train.getProperty("support_enable", "value"):
            envelope_required = True
        if (float(train.getProperty("machine_nozzle_size", "value")) != 0.4
                or float(train.getProperty("material_diameter", "value")) != 1.75):
            raise ValueError("IDEX calibration is validated only for 0.4 mm nozzles and 1.75 mm filament.")
    scene = app.getController().getScene()
    plate = app.getMultiBuildPlateModel().activeBuildPlate
    nodes = []
    for node in DepthFirstIterator(scene.getRoot()):
        if node.callDecoration("getBuildPlateNumber") != plate:
            continue
        if node.callDecoration("isNonPrintingMesh"):
            raise ValueError("IDEX: remove modifier meshes and custom support meshes before export.")
        if node.getMeshData() is not None and node.callDecoration("isSliceable"):
            if node.callDecoration("isOutsideBuildArea"):
                raise ValueError("Model outside the build area: IDEX calibration cannot proceed")
            nodes.append(node)
    if not nodes:
        raise ValueError("No models on the active build plate for calPad generation")
    if envelope_required:
        pilot = trains[0]
        widths = [
            float(pilot.getProperty(key, "value"))
            for key in pilot.getAllKeys()
            if key == "line_width" or "_line_width" in key and not key.endswith("_factor")
        ]
        factor = float(pilot.getProperty("initial_layer_line_width_factor", "value")) / 100
        if not widths or any(not math.isfinite(width) or width <= 0 for width in widths):
            raise ValueError("Invalid line widths for IDEX calibration")
        if not math.isfinite(factor) or factor <= 0:
            raise ValueError("Invalid initial-layer line-width factor for IDEX calibration")
        mesh = calibration_envelope(data, max(widths) * max(1, factor) / 2, config)
        return native_calpads(mesh, config)
    writer = app.getPluginRegistry().getPluginObject("STLWriter")
    if writer is None:
        raise ValueError("Cura STL writer is unavailable")
    mesh = io.BytesIO()
    if not writer.write(mesh, nodes, MeshWriter.OutputMode.BinaryMode):
        raise ValueError("Scene STL export failed: " + str(writer.getInformation()))
    return native_calpads(mesh.getvalue(), config)


def temperature(value: float, maximum: int) -> int:
    if not math.isfinite(value) or not 0 <= value <= maximum:
        raise ValueError(f"Temperature out of range: {value}")
    return round(value)


def command(line: str) -> tuple[str, dict[str, float]]:
    code = line.partition(";")[0].strip()
    if not code:
        return "", {}
    parts = code.split(maxsplit=1)
    if not re.fullmatch(r"[GMT]\d+", parts[0]):
        raise ValueError(f"Unsupported command: {code}")
    parameters = {}
    if len(parts) == 2:
        tail = parts[1]
        for match in PARAMETER.finditer(tail):
            key, number = match.groups()
            if key in parameters:
                raise ValueError(f"Repeated parameter: {code}")
            parameters[key] = float(number)
            if not math.isfinite(parameters[key]):
                raise ValueError(f"Non-finite parameter: {code}")
        if PARAMETER.sub("", tail).strip():
            raise ValueError(f"Unsupported parameter: {code}")
    return parts[0], parameters


def tool_number(value: float) -> int:
    if value not in (0, 1):
        raise ValueError(f"Unsupported extruder: {value}")
    return int(value)


def convert(data: list[str], config: PrintConfig) -> list[str]:
    text = "".join(data)
    existing_idex = re.search(
        r"(?m)^(?:;mode:(?:mirror|duplicate)\b|M118\b[^\r\n]*\bD[12]\b|M109 T[12]\s*$)", text
    )
    if existing_idex and IDEX_STAMP not in text:
        raise ValueError(
            "Old IDEX export without automatic calibration: slice again in Cura."
        )
    if STAMP in text:
        if f";mode:{config.mode};" not in text:
            raise ValueError("Mode changed: slice again before exporting.")
        if existing_idex:
            extract_calpads(text, config)
        if config.mode == "dual" and (DUAL_STAMP not in text or not config.dual_calibrated):
            raise ValueError("Experimental alternating-head printing requires calibration confirmation.")
        return data
    if config.mode not in ("solo", "mirror", "duplicate", "dual"):
        raise ValueError("Unknown print mode")
    dual = config.mode == "dual"
    if dual and not config.dual_calibrated:
        raise ValueError("Experimental alternating-head printing: verify both nozzles and the printer's XYZ calibration first.")
    if text.splitlines().count(START) != 1 or text.splitlines().count(END) != 1:
        raise ValueError("Use the Creator 3 integration profile and its start/end G-code.")
    prefix, rest = text.split(START + "\n", 1)
    body, _ = rest.split(END + "\n", 1)
    if config.mode in ("mirror", "duplicate") and not config.idex_calibrated:
        raise ValueError("First verify single-extruder centering and calibrate both nozzles in FlashPrint.")
    if config.mode in ("mirror", "duplicate"):
        if not config.calpads:
            raise ValueError("Native calPads missing: automatic generation is required for IDEX.")
        extract_calpads("\n".join(config.calpads), config)
    if any(not math.isfinite(value) or value <= 0 for value in (config.width, config.depth, config.height)):
        raise ValueError("Invalid build volume dimensions")

    active = None
    for line in prefix.splitlines():
        code = line.partition(";")[0].strip()
        if re.fullmatch(r"T[01]", code):
            active = int(code[1:])
        elif re.fullmatch(r"M108 T[01]", code):
            active = int(code[-1])
    if active is None:
        raise ValueError("Initial extruder missing from G-code")
    initial = active
    used = set()
    extrusion = 0.0
    tool_extrusion = [0.0, 0.0]
    reset_required = False
    known_xy = set()
    extent = {"X": 0.0, "Y": 0.0, "Z": 0.0}
    allowed = {
        "G0", "G1", "G4", "G90", "G92", "M82",
        "M104", "M109", "M140", "M190", "M106", "M107", "M105",
        "M201", "M203", "M204", "M205", "M220", "M221", "M400",
        "T0", "T1", "M108",
    }
    parsed = []
    for line in body.splitlines():
        op, params = command(line)
        if op and op not in allowed:
            raise ValueError(f"Command not validated for this profile: {op}")
        if op in ("T0", "T1"):
            if params:
                raise ValueError("Tool selection must not include additional parameters")
            if dual and active != int(op[1:]):
                known_xy.clear()
                reset_required = True
            active = int(op[1:])
        elif op == "M108":
            if set(params) != {"T"}:
                raise ValueError("M108 must only select T0 or T1")
            target = tool_number(params["T"])
            if dual and active != target:
                known_xy.clear()
                reset_required = True
            active = target
        elif op == "G92":
            if set(params) != {"E"}:
                raise ValueError("XYZ origin changes are not allowed")
            extrusion = params["E"]
            tool_extrusion[active] = extrusion
            reset_required = False
        elif op in ("G0", "G1"):
            if dual and "Z" in params and "E" in params:
                raise ValueError("Alternating-head mode does not support coordinated Z/extrusion moves.")
            for axis in ("X", "Y"):
                if axis in params:
                    known_xy.add(axis)
                    extent[axis] = max(extent[axis], abs(params[axis]))
            if "Z" in params:
                if params["Z"] < 0:
                    raise ValueError("Movement below the build plate")
                extent["Z"] = max(extent["Z"], params["Z"])
            if "E" in params:
                if dual and reset_required:
                    raise ValueError("Cura must reset absolute E with G92 after every tool change.")
                if dual:
                    extrusion = tool_extrusion[active]
                if params["E"] > extrusion + 0.00001:
                    if known_xy != {"X", "Y"}:
                        raise ValueError("Extrusion before XY positioning")
                    used.add(active)
                extrusion = params["E"]
                tool_extrusion[active] = extrusion
        parsed.append((line, op, params))
    if dual:
        if used != {0, 1}:
            raise ValueError("Alternating-head mode requires extrusion from both nozzles. Use Single extruder otherwise.")
        selected = initial
    elif len(used) != 1 or initial not in used:
        raise ValueError("Use one nozzle for the entire plate in Single/Mirror/Duplicate mode. Select experimental alternating heads for dual-material jobs.")
    else:
        selected = used.pop()
    if config.mode in ("mirror", "duplicate") and selected != 0:
        raise ValueError("Mirror/duplicate: assign all models to Extruder 1 (right/T0).")
    if not dual and any(op in ("T0", "T1", "M108") and (
        int(op[1:]) if op.startswith("T") else tool_number(params["T"])
    ) != selected for _, op, params in parsed):
        raise ValueError("Tool changes during printing are not supported.")
    half_width = config.width / 2 if config.mode in ("solo", "dual") else idex_half_width(config)
    if extent["X"] > half_width + 0.001 or extent["Y"] > config.depth / 2 + 0.001:
        raise ValueError(
            f"Path outside the print area: X +/-{extent['X']:.2f}, Y +/-{extent['Y']:.2f} mm. "
            f"Limits: X +/-{half_width:.2f}, Y +/-{config.depth / 2:.2f} mm, including raft/supports."
        )
    if extent["Z"] <= 0 or extent["Z"] > config.height + 0.001:
        raise ValueError("Path height out of range")
    bed = temperature(config.bed, 120)
    heat = temperature(config.nozzle[selected], 300)
    dual_heat = [temperature(value, 300) for value in config.nozzle] if dual else []
    if dual and any(value == 0 for value in dual_heat):
        raise ValueError("Both nozzle temperatures must be nonzero in alternating-head mode.")
    if heat == 0:
        raise ValueError("Nozzle temperature must not be zero")
    mode_number = {"mirror": 1, "duplicate": 2}.get(config.mode)
    tools = [0, 1] if dual or mode_number is not None else [selected]
    bounds = " ".join(f"{axis}{math.ceil(value * 100) / 100:.2f}" for axis, value in extent.items())
    header = []
    for line in prefix.splitlines():
        if line.startswith(";C3_"):
            break
        if line.startswith(";"):
            header.append(line)
    startup = [STAMP, f";mode:{config.mode}; source:Marlin; coordinates and extrusion unchanged"]
    if dual:
        startup.append(DUAL_STAMP)
    startup.append(f"M118 {bounds} " + " ".join(f"T{tool}" for tool in tools) + (
        f" D{mode_number}" if mode_number is not None else ""
    ))
    startup.append(f"M140 S{bed} T0")
    startup.extend(f"M104 S{dual_heat[tool] if dual else heat if tool in tools else 0} T{tool}" for tool in (0, 1))
    startup.extend(["M107", "G90", "G1 Z50.000 F420", "M7 T0"])
    startup.extend(f"M6 T{tool}" for tool in tools)
    startup.append("M651 S255")
    if mode_number:
        startup.extend([IDEX_STAMP, *config.calpads])
    startup.extend([f"M109 T{mode_number}" if mode_number else f"M108 T{selected}", "G92 E0"])
    output = []
    active = selected
    fan_speeds = [0, 0]
    source_feed = None
    source_feed_text = None
    native_feed = None
    for line, op, params in parsed:
        if op in ("T0", "T1", "M108"):
            target = int(op[1:]) if op.startswith("T") else tool_number(params["T"])
            if dual and target != active:
                active = target
                output.append(f"M108 T{active}")
                output.append(f"M106 S{fan_speeds[active]}" if fan_speeds[active] else "M107")
            continue
        if op == "M82":
            continue
        if op == "M105":
            if params:
                raise ValueError("Only temperature query M105 without parameters is supported")
            continue
        if op in ("M104", "M109"):
            if "S" not in params or set(params) - {"S", "T"}:
                raise ValueError("Only M104/M109 S<temperature> T<tool> are supported")
            target = tool_number(params.get("T", active))
            if not dual and target != selected:
                continue
            heat = temperature(params["S"], 300)
            targets = [target] if dual else tools
            output.extend(f"M104 S{heat} T{tool}" for tool in targets)
            if op == "M109":
                output.extend(f"M6 T{tool}" for tool in targets)
        elif op in ("M140", "M190"):
            if "S" not in params or set(params) - {"S", "T"}:
                raise ValueError("Unsupported bed temperature command")
            output.append(f"M140 S{temperature(params['S'], 120)} T0")
            if op == "M190":
                output.append("M7 T0")
        elif op in ("M106", "M107"):
            if set(params) - {"S", "P"}:
                raise ValueError("Unsupported fan command")
            target = tool_number(params.get("P", active))
            if not dual and target != selected:
                continue
            speed = params.get("S", 255 if op == "M106" else 0)
            if not 0 <= speed <= 255:
                raise ValueError("Fan speed out of range")
            if dual:
                fan_speeds[target] = round(speed)
                if target != active:
                    continue
            output.append(f"M106 S{round(speed)}" if op == "M106" else "M107")
        elif dual and op in ("G0", "G1"):
            words = dict(PARAMETER.findall(line.partition(";")[0]))
            if set(params) - {"X", "Y", "Z", "E", "F"}:
                raise ValueError("Unsupported alternating-head motion parameters.")
            if "F" in params:
                if params["F"] <= 0:
                    raise ValueError("Movement feedrate must be positive.")
                source_feed = params["F"]
                source_feed_text = words["F"]
            if source_feed is None:
                raise ValueError("Alternating-head motion requires an explicit initial feedrate.")
            remaining = {key: value for key, value in params.items() if key not in ("Z", "F")}
            if "Z" in params:
                # The native reader applies nozzle-height compensation to standalone Z commands.
                native_feed = min(source_feed, 420)
                output.append(f"G1 Z{words['Z']} F{source_feed_text if source_feed <= 420 else '420'}")
            if remaining or "Z" not in params:
                code = "G1"
                if "F" in params or native_feed != source_feed:
                    code += f" F{source_feed_text}"
                code += "".join(f" {key}{words[key]}" for key in remaining)
                comment = line.partition(";")[2]
                if comment:
                    code += " ;" + comment
                output.append(code)
                native_feed = source_feed
        else:
            if mode_number and op == "G0":
                line = re.sub(r"^G0(?=\s)", "G1", line)
            output.append(line)
    shutdown = [
        END, "M107", "M104 S0 T0", "M104 S0 T1", "M140 S0 T0",
        "G162 Z F1800", "M652", "G91", "M18",
    ]
    return ["\n".join(header + startup + output + shutdown) + "\n"]


class Creator3Native(Script):
    def getSettingDataString(self):
        return json.dumps({
            "name": "Creator 3 - Native FlashForge Export",
            "key": "Creator3Native",
            "metadata": {},
            "version": 2,
            "settings": {
                "mode": {
                    "label": "Print mode",
                    "description": "Single right/left. Mirror/Duplicate: one right/T0 pilot with native calPads. Experimental alternating heads: separate Cura paths for both nozzles, XYZ calibration, prime tower and nozzle-switch retraction required.",
                    "type": "enum",
                    "options": {"solo": "Single extruder (right or left)", "mirror": "IDEX Mirror + native calibration", "duplicate": "IDEX Duplicate + native calibration", "dual": "Dual material / alternating heads (experimental)"},
                    "default_value": "solo",
                },
                "idex_calibrated": {
                    "label": "Single-extruder centering and IDEX calibration verified",
                    "description": "Required for IDEX. Confirm single-extruder centering and calibration of both nozzles on the printer. Native calPads are always retained.",
                    "type": "bool",
                    "default_value": False,
                },
                "dual_calibrated": {
                    "label": "Experimental dual-head XYZ calibration verified",
                    "description": "Required for alternating heads. Verify solo centering, both nozzle heights and the printer's dual-nozzle XYZ calibration. Enable a prime tower and nozzle-switch retraction. Firmware manages parking and height compensation. First prints must be supervised.",
                    "type": "bool",
                    "default_value": False,
                },
            },
        })

    def execute(self, data):
        from UM.Application import Application
        from UM.Logger import Logger
        from UM.Message import Message

        try:
            app = Application.getInstance()
            stack = app.getGlobalContainerStack()
            if stack is None or stack.getBottom().getId() != "creator3_personal":
                raise ValueError("This script requires the Creator 3 integration profile.")
            if stack.getProperty("machine_gcode_flavor", "value") != "RepRap (Marlin/Sprinter)":
                raise ValueError("Keep the Marlin G-code flavor for this profile.")
            if not stack.getProperty("machine_center_is_zero", "value"):
                raise ValueError("The origin must be at the center of the build plate.")
            if stack.getProperty("relative_extrusion", "value"):
                raise ValueError("Absolute extrusion is required.")
            trains = sorted(stack.extruderList, key=lambda train: int(train.getMetaDataEntry("position")))
            if len(trains) != 2:
                raise ValueError("Two extruders must be configured.")
            for train in trains:
                if any(float(train.getProperty(key, "value")) != 0 for key in ("machine_nozzle_offset_x", "machine_nozzle_offset_y")):
                    raise ValueError("Nozzle offsets must remain zero: the firmware handles IDEX.")
            config = PrintConfig(
                mode=str(self.getSettingValueByKey("mode")),
                bed=float(stack.getProperty("material_bed_temperature_layer_0", "value")),
                nozzle=tuple(float(train.getProperty("material_print_temperature_layer_0", "value")) for train in trains),
                width=float(stack.getProperty("machine_width", "value")),
                depth=float(stack.getProperty("machine_depth", "value")),
                height=float(stack.getProperty("machine_height", "value")),
                idex_calibrated=self.getSettingValueByKey("idex_calibrated") in (True, "True", "true"),
                dual_calibrated=self.getSettingValueByKey("dual_calibrated") in (True, "True", "true"),
            )
            if config.mode == "dual":
                if stack.getProperty("print_sequence", "value") != "all_at_once":
                    raise ValueError("Alternating-head mode requires All at Once, not One at a Time.")
                if not stack.getProperty("prime_tower_enable", "value"):
                    raise ValueError("Enable a prime tower for experimental alternating-head printing.")
                for train in trains:
                    if float(train.getProperty("switch_extruder_retraction_amount", "value")) <= 0:
                        raise ValueError("Enable nozzle-switch retraction for both extruders.")
                    if train.getProperty("magic_spiralize", "value"):
                        raise ValueError("Spiralize is not supported in alternating-head mode.")
            if config.mode in ("mirror", "duplicate") and STAMP not in "".join(data):
                config = replace(config, calpads=scene_calpads(app, stack, trains, config, data))
            result = convert(data, config)
        except (ValueError, TypeError, AttributeError, OSError, subprocess.SubprocessError) as error:
            # Cura catches script exceptions and otherwise writes the original, unconverted G-code.
            data[:] = [
                ";CREATOR3_EXPORT_BLOQUE\n;ERROR: " + str(error).replace("\n", " ") +
                "\n;DO NOT PRINT THIS FILE\nM104 S0 T0\nM104 S0 T1\nM140 S0 T0\nM107\nM18\n"
            ]
            Logger.logException("e", "Creator 3: export blocked")
            Message(str(error) + "\nExport blocked: no printing paths were exported.",
                    title="Creator 3 - export blocked", lifetime=0).show()
            raise
        return result
