"""Native product handoff — durable KNOWLEDGE.md + seat, structural verify.

Thin by design. See docs/handoff-procedure.md.

  install  — park KNOWLEDGE + this-chat-handoff + tmux + register
  verify   — structural only (no LLM)
  boot     — optional start claude
  quiz     — optional one-shot READY token
  status   — print state
  init     — write a short KNOWLEDGE.md template if missing
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


def _norm_product(product: str) -> tuple[str, str]:
    """Return (product_id, case_paths_id) — directmux → config paths directrux."""
    p = (product or "").strip().lower()
    case = p
    if p in ("directmux", "direct-mux", "director", "meta"):
        case = "directrux"
    return p, case


def _home() -> Path:
    return Path.home()


def config_dir(product: str) -> Path:
    from . import product_config as pc

    _, case = _norm_product(product)
    return pc.config_dir_for(case)


def resolve_repo(product: str, repo: str | Path | None = None) -> Path:
    """Find product repo: --repo, product.json repo/product_root, then defaults."""
    if repo:
        p = Path(repo).expanduser().resolve()
        if p.is_dir():
            return p
        raise FileNotFoundError(f"repo not a directory: {p}")

    prod, case = _norm_product(product)
    # product.json optional "repo" or "product_root"
    for cand in (config_dir(prod) / "product.json",):
        if cand.is_file():
            try:
                data = json.loads(cand.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            for key in ("repo", "product_root", "root"):
                raw = (data.get(key) or "").strip()
                if raw:
                    p = Path(raw).expanduser()
                    if p.is_dir():
                        return p.resolve()

    home = _home()
    for name in (prod, case):
        for base in (home / "repos-aic" / name, home / "repos-eidos-agi" / name):
            if base.is_dir():
                return base.resolve()
    raise FileNotFoundError(
        f"repo not found for product={prod!r}; pass --repo or set product.json repo="
    )


def seat_name(product: str, seat: str | None = None) -> str:
    if seat:
        return seat.strip()
    prod, _ = _norm_product(product)
    return f"{prod}-this-chat"


def state_dir(seat: str) -> Path:
    root = Path(
        os.environ.get("EMUX_HANDOFF_STATE_ROOT")
        or (Path.home() / ".local" / "share")
    ).expanduser()
    d = root / seat
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, text=True, capture_output=True, **kw)


def _tmux_has(seat: str) -> bool:
    return _run(["tmux", "has-session", "-t", seat]).returncode == 0


def _register(seat: str, product: str) -> None:
    emux = shutil.which("emux")
    if not emux:
        return
    _run(
        [
            emux,
            "register",
            seat,
            seat,
            "--description",
            f"Handoff home for {product} — read KNOWLEDGE.md",
            "--tags",
            f"{product},handoff,this-chat",
        ]
    )


KNOWLEDGE_TEMPLATE = """# {product} knowledge

## Compass
1. What this product is for (one line).
2. How we prove green (command).
3. What is not fixed (honest).
4. How to resume after handoff.

## Product
- Role:
- Room / port:
- Brand vs on-disk id:

## Proved green
-

## Not fixed
-

## Dogfood
```bash
emux handoff status --product {product}
```

## Do not
- Claim fixed without proof
- Overwrite product ALWAYS/CLAUDE with a mission dump
"""


def cmd_init(product: str, repo: str | Path | None = None, force: bool = False) -> int:
    """Write a short KNOWLEDGE.md template if missing."""
    prod, _ = _norm_product(product)
    root = resolve_repo(product, repo)
    path = root / "KNOWLEDGE.md"
    if path.is_file() and not force:
        print(f"handoff: already exists {path}")
        return 0
    path.write_text(KNOWLEDGE_TEMPLATE.format(product=prod), encoding="utf-8")
    print(f"handoff: wrote template {path}")
    return 0


def cmd_install(
    product: str,
    *,
    repo: str | Path | None = None,
    knowledge: str | Path | None = None,
    source_session: str | None = None,
    seat: str | None = None,
) -> int:
    prod, case = _norm_product(product)
    root = resolve_repo(product, repo)
    seat_n = seat_name(product, seat)
    state = state_dir(seat_n)

    ksrc = Path(knowledge).expanduser() if knowledge else root / "KNOWLEDGE.md"
    if not ksrc.is_file():
        print(f"handoff: write KNOWLEDGE.md first: {ksrc}", file=__import__("sys").stderr)
        return 2

    # Only KNOWLEDGE + handoff pointer — never clobber product CLAUDE/ALWAYS.
    shutil.copy2(ksrc, state / "KNOWLEDGE.md")
    dest_k = root / "KNOWLEDGE.md"
    if ksrc.resolve() != dest_k.resolve():
        shutil.copy2(ksrc, dest_k)

    handoff_md = (
        f"# Handoff: {seat_n}\n\n"
        f"- Product: `{prod}` (paths may use `{case}`)\n"
        f"- Seat: `{seat_n}` — `tmux attach -t {seat_n}`\n"
        f"- Repo: `{root}`\n"
        f"- Knowledge: `KNOWLEDGE.md`\n"
    )
    if source_session:
        handoff_md += f"- Source: `{source_session}`\n"
    handoff_md += (
        f"- Structural check: `emux handoff verify --product {prod}`\n"
        f"- Optional quiz: `emux handoff quiz --product {prod}`\n"
    )
    (root / "this-chat-handoff.md").write_text(handoff_md, encoding="utf-8")
    (state / "this-chat-handoff.md").write_text(handoff_md, encoding="utf-8")

    if not _tmux_has(seat_n):
        _run(["tmux", "new-session", "-d", "-s", seat_n, "-c", str(root)])
        _run(
            [
                "tmux",
                "send-keys",
                "-t",
                seat_n,
                f"clear; echo handoff seat {seat_n}; ls KNOWLEDGE.md; pwd",
                "Enter",
            ]
        )

    _register(seat_n, prod)
    print(f"handoff: installed {seat_n}")
    print(f"  knowledge → {root / 'KNOWLEDGE.md'}")
    print(f"  next: emux handoff verify --product {prod}")
    return 0


def _looks_like_claude_command(name: str) -> bool:
    """Claude Code often renames argv0 to its version (e.g. 2.1.220)."""
    n = (name or "").strip()
    if not n:
        return False
    low = n.lower()
    if low in ("claude", "claude.exe") or low.startswith("claude"):
        return True
    # version-shaped process name used by Claude Code builds
    import re

    return bool(re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][\w.]+)?", n))


def _claude_running_in_seat(seat: str) -> bool:
    """True if the seat pane is already running Claude Code (any argv0)."""
    cmd_r = _run(["tmux", "list-panes", "-t", seat, "-F", "#{pane_current_command}"])
    if _looks_like_claude_command((cmd_r.stdout or "").strip()):
        return True
    r = _run(["tmux", "list-panes", "-t", seat, "-F", "#{pane_pid}"])
    ppid = (r.stdout or "").strip().splitlines()[:1]
    if not ppid:
        return False
    # Exact name (older builds)
    if _run(["pgrep", "-P", ppid[0], "-x", "claude"]).returncode == 0:
        return True
    # Children of the pane shell: match name or args containing claude CLI
    ps = _run(["ps", "-ax", "-o", "pid=,ppid=,comm=,args="])
    if ps.returncode != 0:
        return False
    parent = ppid[0]
    for line in (ps.stdout or "").splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        _pid, p_ppid, comm = parts[0], parts[1], parts[2]
        args = parts[3] if len(parts) > 3 else ""
        if p_ppid != parent:
            continue
        if _looks_like_claude_command(comm):
            return True
        if "/claude" in args or args.strip().startswith("claude "):
            return True
    return False


def cmd_boot(product: str, *, repo: str | Path | None = None, seat: str | None = None) -> int:
    root = resolve_repo(product, repo)
    seat_n = seat_name(product, seat)
    if not _tmux_has(seat_n):
        print(f"handoff: seat missing — run install ({seat_n})", file=__import__("sys").stderr)
        return 2
    if _claude_running_in_seat(seat_n):
        print("handoff: claude already running")
        return 0
    _run(["tmux", "send-keys", "-t", seat_n, "C-c"])
    time.sleep(0.2)
    # Use send-keys with literal command
    launch = f"cd {root} && claude --dangerously-skip-permissions"
    _run(["tmux", "send-keys", "-t", seat_n, launch, "Enter"])
    print(f"handoff: launched claude in {seat_n}")
    return 0


def cmd_verify(product: str, *, repo: str | Path | None = None, seat: str | None = None) -> int:
    """Structural only — files + tmux. No LLM."""
    import sys

    root = resolve_repo(product, repo)
    seat_n = seat_name(product, seat)
    state = state_dir(seat_n)
    fail = False

    if not _tmux_has(seat_n):
        print(f"handoff: FAIL tmux seat missing: {seat_n}", file=sys.stderr)
        fail = True
    sk = state / "KNOWLEDGE.md"
    rk = root / "KNOWLEDGE.md"
    if not sk.is_file():
        print(f"handoff: FAIL missing {sk}", file=sys.stderr)
        fail = True
    if not rk.is_file():
        print(f"handoff: FAIL missing {rk}", file=sys.stderr)
        fail = True
    if sk.is_file():
        lines = len(sk.read_text(encoding="utf-8", errors="replace").splitlines())
        if lines < 5:
            print(f"handoff: FAIL KNOWLEDGE too short ({lines} lines)", file=sys.stderr)
            fail = True

    if fail:
        (state / "verify-status").write_text("fail\n", encoding="utf-8")
        (state / "verified-at").write_text(
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n", encoding="utf-8"
        )
        return 3

    print("handoff: VERIFY PASS (structural)")
    print(f"  seat:      {seat_n}")
    print(f"  knowledge: {sk}")
    (state / "verify-status").write_text("pass\n", encoding="utf-8")
    (state / "verified-at").write_text(
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n", encoding="utf-8"
    )
    return 0


def cmd_quiz(
    product: str,
    *,
    repo: str | Path | None = None,
    seat: str | None = None,
    timeout: int = 120,
) -> int:
    """Optional one-shot LLM quiz. No retries."""
    import sys

    root = resolve_repo(product, repo)
    seat_n = seat_name(product, seat)
    state = state_dir(seat_n)
    if not _tmux_has(seat_n):
        print("handoff: seat missing", file=sys.stderr)
        return 2
    if not (root / "KNOWLEDGE.md").is_file():
        print("handoff: KNOWLEDGE.md missing", file=sys.stderr)
        return 2
    emux = shutil.which("emux")
    if not emux:
        print("handoff: emux not on PATH", file=sys.stderr)
        return 2

    quiz = (
        "Read KNOWLEDGE.md. Short recap of compass + open gaps. "
        "Final line alone: READY_FOR_HANDOFF=yes if you can run without the source chat, "
        "else READY_FOR_HANDOFF=no."
    )
    print(f"handoff: quiz (one shot, timeout {timeout}s)…")
    # emux ask has its own wait; we don't thrash
    r = _run([emux, "ask", seat_n, quiz], timeout=max(timeout, 30))
    out = (r.stdout or "") + (r.stderr or "")
    print("\n".join(out.splitlines()[-40:]))
    (state / "last-quiz.txt").write_text(out, encoding="utf-8")

    lines = out.splitlines()
    has_yes = any(ln.strip() == "READY_FOR_HANDOFF=yes" for ln in lines)
    has_no = any(ln.strip() == "READY_FOR_HANDOFF=no" for ln in lines)
    if has_no and not has_yes:
        print("handoff: QUIZ FAIL (said no)", file=sys.stderr)
        (state / "quiz-status").write_text("quiz-fail\n", encoding="utf-8")
        return 3
    # last sole-line wins
    last_yes = max((i for i, ln in enumerate(lines) if ln.strip() == "READY_FOR_HANDOFF=yes"), default=-1)
    last_no = max((i for i, ln in enumerate(lines) if ln.strip() == "READY_FOR_HANDOFF=no"), default=-1)
    if last_no > last_yes:
        print("handoff: QUIZ FAIL (said no)", file=sys.stderr)
        (state / "quiz-status").write_text("quiz-fail\n", encoding="utf-8")
        return 3
    if last_yes >= 0:
        print("handoff: QUIZ PASS")
        (state / "quiz-status").write_text("quiz-pass\n", encoding="utf-8")
        return 0
    print("handoff: QUIZ FAIL (no sole-line READY token)", file=sys.stderr)
    (state / "quiz-status").write_text("quiz-fail\n", encoding="utf-8")
    return 3


def cmd_status(product: str, *, repo: str | Path | None = None, seat: str | None = None) -> int:
    prod, _ = _norm_product(product)
    seat_n = seat_name(product, seat)
    state = state_dir(seat_n)
    print(f"product: {prod}")
    print(f"seat:    {seat_n}")
    print(f"tmux:    {'UP' if _tmux_has(seat_n) else 'DOWN'}")
    sk = state / "KNOWLEDGE.md"
    if sk.is_file():
        n = len(sk.read_text(encoding="utf-8", errors="replace").splitlines())
        print(f"knowledge: {sk} ({n} lines)")
    else:
        print("knowledge: MISSING")
    vs = state / "verify-status"
    print(f"verify:  {vs.read_text(encoding='utf-8').strip() if vs.is_file() else 'never'}")
    qs = state / "quiz-status"
    print(f"quiz:    {qs.read_text(encoding='utf-8').strip() if qs.is_file() else 'never'}")
    try:
        r = resolve_repo(product, repo)
        print(f"repo:    {r}")
    except FileNotFoundError as e:
        print(f"repo:    ({e})")
    return 0


def doctor_all() -> int:
    """Quick handoff structural check for every known skin with a resolvable repo."""
    from . import skin as sk

    # Skin id on disk may differ from brand product id used for seats.
    brand_for = {"directrux": "directmux"}

    print("handoff doctor (all skins with a repo):")
    any_fail = False
    for name in sk.list_skins():
        brand = brand_for.get(name, name)
        try:
            root = resolve_repo(brand)
        except FileNotFoundError:
            try:
                root = resolve_repo(name)
            except FileNotFoundError:
                print(f"  {name:12} skip (no repo)")
                continue
        seat = seat_name(brand)
        k = root / "KNOWLEDGE.md"
        tmux = "UP" if _tmux_has(seat) else "down"
        k_ok = "yes" if k.is_file() else "no"
        label = brand if brand != name else name
        print(f"  {label:12} repo={root}  knowledge={k_ok}  seat={seat} tmux={tmux}")
        if k.is_file() and tmux == "down":
            any_fail = True
    return 1 if any_fail else 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry for `python -m emux.handoff`."""
    import argparse

    p = argparse.ArgumentParser(prog="emux-handoff")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("init", "install", "boot", "verify", "quiz", "status", "doctor"):
        sp = sub.add_parser(name)
        if name != "doctor":
            sp.add_argument("--product", required=True)
            sp.add_argument("--repo", default=None)
            sp.add_argument("--seat", default=None)
        if name in ("init", "install"):
            sp.add_argument("--knowledge", default=None)
            sp.add_argument("--source-session", default=None)
        if name == "init":
            sp.add_argument("--force", action="store_true")
        if name == "quiz":
            sp.add_argument("--timeout", type=int, default=120)
    args = p.parse_args(argv)

    if args.cmd == "doctor":
        return doctor_all()
    if args.cmd == "init":
        return cmd_init(args.product, args.repo, force=args.force)
    if args.cmd == "install":
        return cmd_install(
            args.product,
            repo=args.repo,
            knowledge=args.knowledge,
            source_session=args.source_session,
            seat=args.seat,
        )
    if args.cmd == "boot":
        return cmd_boot(args.product, repo=args.repo, seat=args.seat)
    if args.cmd == "verify":
        return cmd_verify(args.product, repo=args.repo, seat=args.seat)
    if args.cmd == "quiz":
        return cmd_quiz(args.product, repo=args.repo, seat=args.seat, timeout=args.timeout)
    if args.cmd == "status":
        return cmd_status(args.product, repo=args.repo, seat=args.seat)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
