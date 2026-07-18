#!/usr/bin/env bash
# Load a passphrase-protected SSH key into ai_dev_loop's persistent SSH agent.
#
# Usage:
#   ./scripts/load-ai-dev-loop-ssh-key.sh
#   ./scripts/load-ai-dev-loop-ssh-key.sh ~/.ssh/another_key

set -euo pipefail

readonly service_name="ai-dev-loop-ssh-agent.service"
readonly socket_path="${AI_DEV_LOOP_SSH_AUTH_SOCK:-$HOME/.ssh/ai-dev-loop-ssh-agent.sock}"
readonly key_path="${1:-$HOME/.ssh/id_ed25519}"

if [[ ! -f "$key_path" ]]; then
  printf 'SSH key not found: %s\n' "$key_path" >&2
  printf 'Pass the key path as the first argument if it is not ~/.ssh/id_ed25519.\n' >&2
  exit 1
fi

if ! systemctl --user start "$service_name"; then
  printf 'Could not start %s. Check its user-service status.\n' "$service_name" >&2
  exit 1
fi

if [[ ! -S "$socket_path" ]]; then
  printf 'Persistent SSH-agent socket is unavailable: %s\n' "$socket_path" >&2
  printf 'Check: systemctl --user status %s\n' "$service_name" >&2
  exit 1
fi

export SSH_AUTH_SOCK="$socket_path"

printf 'Enter the passphrase for %s when prompted.\n' "$key_path"
ssh-add "$key_path"

if ! ssh-add -l >/dev/null 2>&1; then
  printf 'The persistent SSH agent still has no usable identities.\n' >&2
  exit 1
fi

set +e
github_output="$(ssh -T git@github.com 2>&1)"
github_status=$?
set -e

# GitHub intentionally exits 1 after a successful SSH authentication because
# it does not provide shell access.
if [[ $github_status -eq 1 && "$github_output" == *"successfully authenticated"* ]]; then
  printf 'SSH key loaded into the persistent ai_dev_loop agent; GitHub authentication verified.\n'
  exit 0
fi

printf '%s\n' "$github_output" >&2
printf 'The key was loaded, but GitHub SSH authentication could not be verified.\n' >&2
exit 1
