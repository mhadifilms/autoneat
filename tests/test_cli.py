from autoneat.cli import build_parser
from autoneat import runner


def test_top_level_parser_keeps_doctor_command():
    parser = build_parser()
    args = parser.parse_args(["doctor"])

    assert args.command == "doctor"


def test_profile_runner_accepts_toolkit_neat_flags():
    parser = runner._build_parser()
    args = parser.parse_args(
        [
            "--shot-ids",
            "001",
            "002",
            "--all-video-tracks",
            "--continue",
            "--retry-failed",
            "--reset",
            "--color-wrap",
            "--color-wrap-scale",
            "0.25",
            "--no-templates",
        ]
    )

    assert args.shot_ids == ["001", "002"]
    assert args.all_video_tracks is True
    assert args.continue_run is True
    assert args.retry_failed is True
    assert args.reset_neat is True
    assert args.no_color_wrap is False
    assert args.color_wrap_scale == 0.25
    assert args.no_templates is True
