"""Run real pytest checks and emit a report that contains no response bodies or credentials."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
import subprocess
import sys
import time
from pathlib import Path
import xml.etree.ElementTree as ET


def summarize(junit: Path, exit_code: int, seconds: float) -> dict:
    cases = []
    if junit.is_file():
        root = ET.parse(junit).getroot()
        for case in root.iter("testcase"):
            status = "passed"
            for name in ("failure", "error", "skipped"):
                if case.find(name) is not None:
                    status = {"failure": "failed", "error": "error", "skipped": "skipped"}[name]
                    break
            # Deliberately omit failure text, captured stdout and system properties:
            # they may contain fixture values or request headers.
            cases.append({
                "name": case.get("name", ""),
                "class": case.get("classname", ""),
                "seconds": float(case.get("time", "0")),
                "status": status,
            })
    totals = {key: sum(c["status"] == key for c in cases)
              for key in ("passed", "failed", "error", "skipped")}
    return {
        "schema_version": 1,
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "exit_code": exit_code,
        "seconds": round(seconds, 3),
        "tests": len(cases),
        **totals,
        "success": exit_code == 0 and totals["passed"] > 0 and not totals["failed"] and not totals["error"],
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="/tmp/parkslot-tests")
    parser.add_argument("--emit-report", action="store_true")
    args, pytest_args = parser.parse_known_args()
    destination = Path(args.output)
    destination.mkdir(parents=True, exist_ok=True)
    junit = destination / "junit.xml"
    started = time.monotonic()
    code = subprocess.call([
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "--junitxml=" + str(junit), *pytest_args,
    ])
    report = summarize(junit, code, time.monotonic() - started)
    payload = json.dumps(report, ensure_ascii=True, indent=2)
    (destination / "summary.json").write_text(payload + "\n", encoding="utf-8")
    # Replace verbose JUnit with a minimal, shareable form.
    clean = ET.Element("testsuites")
    suite = ET.SubElement(clean, "testsuite", name="ParkSlot",
        tests=str(report["tests"]), failures=str(report["failed"]),
        errors=str(report["error"]), skipped=str(report["skipped"]))
    for case in report["cases"]:
        node = ET.SubElement(suite, "testcase", name=case["name"],
                             classname=case["class"], time=str(case["seconds"]))
        if case["status"] != "passed":
            kind = {"failed": "failure", "error": "error", "skipped": "skipped"}[case["status"]]
            ET.SubElement(node, kind, message="See the private local test log.")
    ET.ElementTree(clean).write(junit, encoding="utf-8", xml_declaration=True)
    if args.emit_report:
        encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        print("PARKSLOT_TEST_REPORT=" + encoded, flush=True)
    print(f"ParkSlot tests: {report['tests']} cases; success={report['success']}", flush=True)
    return code if code else (0 if report["success"] else 2)


if __name__ == "__main__":
    raise SystemExit(main())
