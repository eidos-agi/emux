"""New-mission ('n' / `emux new`) plan parsing + prompt wiring."""

from emux.cli import _apply_permission_mode, _mission_path, _parse_plan, _plan_prompt


def test_permission_mode_appended_to_claude():
    cmd = _apply_permission_mode({"command": "claude 'check dally'",
                                  "permission_mode": "bypassPermissions"})
    assert cmd == "claude 'check dally' --permission-mode bypassPermissions"


def test_permission_mode_default_and_nonclaude_untouched():
    assert _apply_permission_mode({"command": "claude 'x'", "permission_mode": "default"}) == "claude 'x'"
    assert _apply_permission_mode({"command": "htop", "permission_mode": "bypassPermissions"}) == "htop"
    assert _apply_permission_mode({"command": "claude 'x'"}) == "claude 'x'"


def test_permission_mode_never_doubled_or_invented():
    already = "claude 'x' --permission-mode acceptEdits"
    assert _apply_permission_mode({"command": already, "permission_mode": "bypassPermissions"}) == already
    # unknown mode from the model → pass through untouched, no invented flag
    assert _apply_permission_mode({"command": "claude 'x'", "permission_mode": "yolo"}) == "claude 'x'"


def test_mission_path_local():
    path = _mission_path({"name": "finances", "command": "claude 'dig in'"})
    assert path == "this terminal → tmux new-session 'finances' → claude"


def test_mission_path_remote_with_cwd():
    path = _mission_path({
        "name": "check-dally", "host": "Daniels-Mac-mini.local",
        "cwd": "/Users/ds/work", "command": "claude 'check on dally'",
    })
    assert path == ("this terminal → ssh Daniels-Mac-mini.local → "
                    "tmux new-session 'check-dally' (cwd /Users/ds/work) → claude")


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
