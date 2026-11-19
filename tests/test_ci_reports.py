"""Publishing must not turn an empty run into PASS or disclose captured credentials."""
import importlib.util
import json
from pathlib import Path


path = Path(__file__).resolve().parents[1] / "scripts" / "ci_test_runner.py"
spec = importlib.util.spec_from_file_location("ci_report", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_report_requires_an_executed_passing_test(tmp_path):
    missing = module.summarize(tmp_path / "missing.xml", 0, 1)
    assert missing["tests"] == 0 and not missing["success"]
    skipped = tmp_path / "skipped.xml"
    skipped.write_text('<testsuite><testcase name="offline"><skipped/></testcase></testsuite>')
    assert not module.summarize(skipped, 0, 1)["success"]


def test_report_counts_outcomes_and_omits_sensitive_test_output(tmp_path):
    report = tmp_path / "results.xml"
    report.write_text('''<testsuite>
        <properties><property name="password" value="secret-password"/></properties>
        <testcase name="ok" classname="api" time="0.1"/>
        <testcase name="broken" classname="api" time="0.2">
          <failure message="secret-password">Authorization: Bearer secret-token</failure>
          <system-out>private database path</system-out>
        </testcase>
        <testcase name="skip"><skipped/></testcase>
        <testcase name="error"><error/></testcase>
    </testsuite>''')
    result = module.summarize(report, 1, 1.5)
    assert result["tests"] == 4
    assert (result["passed"], result["failed"], result["error"], result["skipped"]) == (1, 1, 1, 1)
    assert not result["success"]
    payload = json.dumps(result)
    assert "secret-password" not in payload and "secret-token" not in payload
    assert "private database path" not in payload


def test_nonzero_pytest_exit_cannot_be_published_as_success(tmp_path):
    report = tmp_path / "results.xml"
    report.write_text('<testsuite><testcase name="ok"/></testsuite>')
    assert module.summarize(report, 0, 1)["success"]
    assert not module.summarize(report, 2, 1)["success"]
