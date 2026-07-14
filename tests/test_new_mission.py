"""New-mission ('n' / `emux new`) plan parsing + prompt wiring."""

from emux.cli import _parse_plan, _plan_prompt


def test_parse_plan_clean_json():
    plan = _parse_plan(
        '{"question": "", "summary": "dig into finances", "name": "finances",'
        ' "host": "mac-mini", "cwd": null,'
        ' "command": "claude \'look into my finances\'"}'
    )
    assert plan is not None
    assert plan["name"] == "finances"
    assert plan["host"] == "mac-mini"


def test_parse_plan_tolerates_prose():
    plan = _parse_plan('Sure! Here is the plan:\n{"question": "", "name": "x", "command": "ls"}\nDone.')
    assert plan is not None and plan["name"] == "x"


def test_parse_plan_garbage_is_none():
    assert _parse_plan("no json here") is None
    assert _parse_plan('["a", "list"]') is None


def test_plan_prompt_carries_transcript(monkeypatch, tmp_path):
    monkeypatch.setenv("EMUX_REGISTRY", str(tmp_path / "registry.json"))
    prompt = _plan_prompt(["user: look into my finances on mac-mini"])
    assert "look into my finances on mac-mini" in prompt
    assert "ONE JSON object" in prompt


def test_tui_actions_include_new_mission():
    from emux.tui import _build_groups, _build_preview_for
    groups = _build_groups()
    kinds = [it["kind"] for it in groups["actions"]]
    assert "new_mission" in kinds
    item = next(it for it in groups["actions"] if it["kind"] == "new_mission")
    assert "New mission" in _build_preview_for(item)
