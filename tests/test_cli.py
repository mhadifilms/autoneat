from autoneat import runner
from autoneat.cli import build_parser


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


def test_profile_options_filters_embedding_values_and_preserves_owned_flags(monkeypatch):
    captured = {}

    def fake_main(argv=None, *, _parsed_args=None):
        captured["argv"] = argv
        captured["args"] = _parsed_args
        return 23

    monkeypatch.setattr(runner, "main", fake_main)

    result = runner.run_profile_options(
        {
            "project": "client/show-101",
            "shot_ids": ("001", "002"),
            "fresh_neat": True,
            "reuse_existing_neat": False,
            "no_color_wrap": False,
            "color_wrap_scale": 0.25,
            "toolkit_only_value": "ignored",
        },
        timeline_name="Show_Degrain",
    )

    assert result == 23
    assert captured["argv"] is None
    assert captured["args"].project == "client/show-101"
    assert captured["args"].shot_ids == ("001", "002")
    assert captured["args"].reuse_existing_neat is False
    assert captured["args"].no_color_wrap is False
    assert captured["args"].timeline == "Show_Degrain"
    assert not hasattr(captured["args"], "toolkit_only_value")


def test_profile_options_gui_relaunch_argv_round_trips_changed_values():
    args = runner._profile_options_namespace(
        {
            "project": "client/show-101",
            "shot_ids": ("001", "002"),
            "reuse_existing_neat": False,
            "no_color_wrap": False,
            "color_wrap_scale": 0.25,
            "no_templates": True,
        },
        timeline_name="Show_Degrain",
    )

    reparsed = runner._build_parser().parse_args(runner._profile_options_argv(args))

    assert reparsed.project == args.project
    assert reparsed.shot_ids == list(args.shot_ids)
    assert reparsed.reuse_existing_neat is False
    assert reparsed.no_color_wrap is False
    assert reparsed.color_wrap_scale == 0.25
    assert reparsed.no_templates is True
    assert reparsed.timeline == "Show_Degrain"
