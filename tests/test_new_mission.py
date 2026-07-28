"""New-mission ('n' / `emux new` / `amux new`) plan + brief/jsonl wiring (EID-1164)."""

import json
from pathlib import Path

from emux.cli import _apply_permission_mode, _mission_path, _parse_plan, _plan_prompt
from emux import missions as mission_store


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
        ' "command": "claude"}'
    )
    assert plan is not None
    assert plan["name"] == "finances"
    assert plan["host"] == "mac-mini"
    assert plan["command"] == "claude"


def test_parse_plan_tolerates_prose():
    plan = _parse_plan('Sure! Here is the plan:\n{"question": "", "name": "x", "command": "ls"}\nDone.')
    assert plan is not None and plan["name"] == "x"


def test_parse_plan_garbage_is_none():
    assert _parse_plan("no json here") is None
    assert _parse_plan('["a", "list"]') is None


def test_plan_prompt_forbids_embedded_mission_text(monkeypatch, tmp_path):
    monkeypatch.setenv("EMUX_REGISTRY", str(tmp_path / "registry.json"))
    prompt = _plan_prompt(["user: look into my finances on mac-mini"])
    assert "look into my finances on mac-mini" in prompt
    assert "ONE JSON object" in prompt
    assert "never put the mission text" in prompt.lower() or "NEVER the mission prompt" in prompt
    assert "load this file" in prompt


def test_register_mission_writes_brief_and_jsonl(monkeypatch, tmp_path):
    cfg = tmp_path / "amux"
    cfg.mkdir()
    monkeypatch.setenv("EMUX_PRODUCT", "amux")
    monkeypatch.setattr(mission_store, "config_dir", lambda product=None: cfg)

    intent = "Review AIC north star and file one Linear note with quotes in it: \"hello\"."
    out = mission_store.register_mission(
        name="northstar-check",
        summary="Review north star",
        intent=intent,
        host=None,
        cwd="/Users/dshanklinbv/repos-aic/aic-software-engineer-cockpit",
        permission_mode="bypassPermissions",
        command="claude",
        product="amux",
        transcript=["user: " + intent],
    )
    assert out["ok"] is True
    brief = Path(out["brief_path"])
    log = Path(out["log_path"])
    assert brief.is_file()
    assert log.is_file()
    body = brief.read_text(encoding="utf-8")
    assert "Mission: northstar-check" in body
    assert intent in body
    assert out["id"] in body
    # Launch embeds path only — not the full intent body as free-form soup
    launch = out["launch_command"]
    assert launch.startswith("claude ")
    assert str(brief) in launch
    assert intent not in launch
    assert "--permission-mode bypassPermissions" in launch
    # jsonl has one record pointing at the brief
    rows = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(rows) == 1
    assert rows[0]["id"] == out["id"]
    assert rows[0]["brief_path"] == str(brief)
    assert rows[0]["intent"] == intent


def test_register_mission_shell_command_untouched(monkeypatch, tmp_path):
    cfg = tmp_path / "emux"
    cfg.mkdir()
    monkeypatch.setattr(mission_store, "config_dir", lambda product=None: cfg)
    out = mission_store.register_mission(
        name="top",
        summary="watch load",
        intent="watch load",
        command="htop",
        product="emux",
    )
    assert out["launch_command"] == "htop"
    assert out["agentish"] is False
    assert Path(out["brief_path"]).is_file()


def test_intent_from_transcript():
    assert mission_store.intent_from_transcript(["user: dig into dally", "planner asked: where?"]) == "dig into dally"
    assert mission_store.intent_from_transcript([]) == ""


def test_tui_actions_include_new_mission():
    from emux.tui import _build_groups, _build_preview_for
    groups = _build_groups()
    kinds = [it["kind"] for it in groups["actions"]]
    assert "new_mission" in kinds
    item = next(it for it in groups["actions"] if it["kind"] == "new_mission")
    assert "New mission" in _build_preview_for(item)
