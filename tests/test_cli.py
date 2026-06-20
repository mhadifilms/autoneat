from pathlib import Path

from autoneat.cli import _parse_shot_ids, build_parser


def test_parse_shot_ids_accepts_spaces_and_commas():
    assert _parse_shot_ids("001, 002 003") == ["001", "002", "003"]


def test_profile_parser_builds_namespace():
    parser = build_parser()
    args = parser.parse_args(
        [
            "profile",
            "--project",
            "Show",
            "--timeline",
            "Show_Neat",
            "--shot-ids",
            "001,002",
            "--all-tracks",
            "--continue",
            "--retry-failed",
            "--state",
            "artifacts/neat/state.json",
        ]
    )

    assert args.command == "profile"
    assert args.project == "Show"
    assert args.timeline == "Show_Neat"
    assert args.all_tracks is True
    assert args.continue_run is True
    assert args.retry_failed is True
    assert Path(args.state) == Path("artifacts/neat/state.json")
