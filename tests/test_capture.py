import json
from pathlib import Path

from autoneat import capture
from autoneat import cli


def test_capture_screen_uses_explicit_output(monkeypatch, tmp_path: Path):
    output = tmp_path / "nested" / "screen.png"
    seen = {}

    def fake_capture(path: Path) -> None:
        seen["path"] = path
        path.write_bytes(b"png")

    monkeypatch.setattr("autoneat._neat_ui._capture_screen", fake_capture)

    assert capture.capture_screen(output) == output
    assert seen["path"] == output


def test_capture_screen_uses_autoneat_cache(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("autoneat._neat_ui._cache_base", lambda: tmp_path)
    monkeypatch.setattr(
        "autoneat._neat_ui._capture_screen", lambda path: path.write_bytes(b"png")
    )

    output = capture.capture_screen()

    assert output.parent == tmp_path / "captures"
    assert output.name.startswith("screen-")


def test_capture_cli_json_and_base64(monkeypatch, tmp_path: Path, capsys):
    output = tmp_path / "screen.png"
    output.write_bytes(b"png")
    monkeypatch.setattr(capture, "capture_screen", lambda _output: output)

    assert capture.main(["--json", "--b64"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert json.loads(lines[0]) == {"ok": True, "path": str(output), "bytes": 3}
    assert lines[1] == "BASE64:cG5n"


def test_capture_cli_reports_failure_as_json(monkeypatch, capsys):
    def fail(_output):
        raise RuntimeError("no display")

    monkeypatch.setattr(capture, "capture_screen", fail)

    assert capture.main(["--json"]) == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "no display"}


def test_top_level_cli_routes_capture(monkeypatch):
    seen = {}

    def fake_main(argv=None, *, prog="autoneat capture"):
        seen["argv"] = argv
        seen["prog"] = prog
        return 17

    monkeypatch.setattr(capture, "main", fake_main)

    assert cli.main(["capture", "--json"]) == 17
    assert seen == {"argv": ["--json"], "prog": "autoneat capture"}
