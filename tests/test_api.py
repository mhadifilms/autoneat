from autoneat.api import ProfileOptions, run_profile


def test_run_profile_uses_explicit_handles(monkeypatch):
    seen = {}

    def fake_run_batch(resolve, project, timeline, options, *, sink=None, cancel_event=None):
        seen["resolve"] = resolve
        seen["project"] = project
        seen["timeline"] = timeline
        seen["options"] = options
        seen["sink"] = sink
        seen["cancel_event"] = cancel_event
        return {"ok": True}

    monkeypatch.setattr("autoneat.api.run_batch", fake_run_batch)
    result = run_profile(
        ProfileOptions(project_name="Ignored"),
        resolve="resolve",
        project="project",
        timeline="timeline",
        sink=print,
        cancel_event="cancel",
    )

    assert result == {"ok": True}
    assert seen == {
        "resolve": "resolve",
        "project": "project",
        "timeline": "timeline",
        "options": ProfileOptions(project_name="Ignored"),
        "sink": print,
        "cancel_event": "cancel",
    }
