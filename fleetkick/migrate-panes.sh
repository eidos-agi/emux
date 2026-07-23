#!/usr/bin/env bash
# One-shot migration: sessions created before 0.12 hold several agents as tmux PANES inside
# one session. The slot model gives each agent its own session (and so its own iframe), but
# it does not retroactively unpack panes that already exist — so an old tab still renders as
# one terminal with the old split inside it.
#
# This moves each extra pane into its own slot session, keeping the running agent alive.
# join-pane MOVES a pane, it does not copy or restart it, so the conversation survives.
# Safe to re-run: sessions that already have one pane are skipped.
set -uo pipefail

POOL=(todd sally marcus nina omar wren)
migrated=0

for sess in $(tmux ls -F '#{session_name}' 2>/dev/null | grep -E '^fleetkick-[0-9a-f]{8}-[0-9]+$'); do
  install=$(sed -E 's/^fleetkick-([0-9a-f]{8})-([0-9]+)$/\1/' <<< "$sess")
  tab=$(sed -E 's/^fleetkick-([0-9a-f]{8})-([0-9]+)$/\2/' <<< "$sess")

  mapfile -t panes < <(tmux list-panes -t "$sess" -F '#{pane_id}|#{@fk_role}|#{@fk_agent}' 2>/dev/null)
  [ "${#panes[@]}" -gt 1 ] || continue
  echo "== $sess : ${#panes[@]} panes"

  # The first pane stays put and becomes slot 0; it inherits its own pane's role/agent.
  first="${panes[0]}"
  tmux set-option -t "$sess" @fk_role  "$(cut -d'|' -f2 <<< "$first" | grep . || echo solo)"   2>/dev/null
  tmux set-option -t "$sess" @fk_agent "$(cut -d'|' -f3 <<< "$first" | grep . || echo claude)" 2>/dev/null
  tmux set-option -t "$sess" @fk_slot 0 2>/dev/null
  [ -n "$(tmux show-options -v -t "$sess" @fk_name 2>/dev/null)" ] \
    || tmux set-option -t "$sess" @fk_name "${POOL[0]}" 2>/dev/null

  slot=1
  for row in "${panes[@]:1}"; do
    pane=$(cut -d'|' -f1 <<< "$row")
    role=$(cut -d'|' -f2 <<< "$row"); [ -n "$role" ] || role=worker
    agent=$(cut -d'|' -f3 <<< "$row"); [ -n "$agent" ] || agent=claude

    while tmux has-session -t "$sess-$slot" 2>/dev/null; do slot=$((slot + 1)); done
    dest="$sess-$slot"

    # tmux cannot break a pane straight into a NEW session, so make the session with a
    # placeholder, move the real pane in beside it, then drop the placeholder.
    ph=$(tmux new-session -d -s "$dest" -P -F '#{pane_id}' 'sleep 2147483647' 2>/dev/null)
    if [ -z "$ph" ]; then echo "   ! could not create $dest"; continue; fi
    if tmux join-pane -s "$pane" -t "$dest" 2>/dev/null; then
      tmux kill-pane -t "$ph" 2>/dev/null
      tmux set-option -t "$dest" @fk_role "$role"   2>/dev/null
      tmux set-option -t "$dest" @fk_agent "$agent" 2>/dev/null
      tmux set-option -t "$dest" @fk_slot "$slot"   2>/dev/null
      tmux set-option -t "$dest" @fk_name "${POOL[$((slot % ${#POOL[@]}))]}" 2>/dev/null
      tmux set-option -t "$dest" mouse on 2>/dev/null
      echo "   $pane ($role/$agent) -> $dest as ${POOL[$((slot % ${#POOL[@]}))]}"
      migrated=$((migrated + 1))
    else
      echo "   ! join-pane failed for $pane; leaving it where it is"
      tmux kill-session -t "$dest" 2>/dev/null
    fi
    slot=$((slot + 1))
  done

  # The old split left border decoration behind; the panel draws dividers now.
  tmux set-option -w -t "$sess" pane-border-status off 2>/dev/null
done

# Legacy panes can carry several managers each, because the pre-0.9 split model let
# "+ manager" chain: every click made a new manager that took charge of one neighbour.
# One manager per tab is the rule, so keep the lowest slot and demote the rest.
for base in $(tmux ls -F '#{session_name}' 2>/dev/null | grep -E '^fleetkick-[0-9a-f]{8}-[0-9]+$'); do
  seen_mgr=0
  for sess in $(tmux ls -F '#{session_name}' 2>/dev/null \
      | grep -E "^${base}(-[0-9]+)?$" | sort -t- -k4 -n); do
    if [ "$(tmux show-options -v -t "$sess" @fk_role 2>/dev/null)" = "manager" ]; then
      if [ "$seen_mgr" -eq 1 ]; then
        tmux set-option -t "$sess" @fk_role worker 2>/dev/null
        echo "   demoted extra manager: $sess"
      fi
      seen_mgr=1
    fi
  done
done

echo "migrated $migrated pane(s)"
