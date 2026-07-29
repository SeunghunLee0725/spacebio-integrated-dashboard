#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${PI_HOST:-}" || -z "${PI_USER:-}" ]]; then
  echo "PI_HOST and PI_USER are required" >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stamp="${CAPTURE_TIMESTAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
raw="$repo_root/deploy/baseline/raw/$stamp"
safe_parent="$repo_root/clinostat_extension/baseline/sanitized"
safe="$safe_parent/$stamp"
temporary="$safe_parent/.$stamp.tmp.$$"
work_tmp="$repo_root/clinostat_extension/.work.tmp.$$"
remote="/home/aiworker-1/clinostat"
target="${PI_USER}@${PI_HOST}"
ssh_opts=(-o StrictHostKeyChecking=yes)
tunnel_pid=""
published=0

cleanup() {
  result=$?
  if [[ -n "$tunnel_pid" ]]; then command kill "$tunnel_pid" 2>/dev/null || true; fi
  rm -rf "$temporary" "$work_tmp"
  if [[ $result -ne 0 && $published -eq 1 ]]; then rm -rf "$safe"; fi
  exit "$result"
}
trap cleanup EXIT INT TERM

[[ ! -e "$raw" && ! -e "$safe" ]] || {
  echo "capture timestamp already exists: $stamp" >&2
  exit 2
}
mkdir -p "$raw/static" "$safe_parent"

# All commands sent to the Pi are observational.
ssh "${ssh_opts[@]}" "$target" "cd '$remote' && git rev-parse HEAD" >"$raw/git_head.txt"
ssh "${ssh_opts[@]}" "$target" "cd '$remote' && git status --porcelain=v1" >"$raw/git_status.txt"
ssh "${ssh_opts[@]}" "$target" "cd '$remote' && git diff --no-ext-diff" >"$raw/git_diff.patch"
ssh "${ssh_opts[@]}" "$target" "curl --fail --silent http://127.0.0.1:8000/openapi.json" >"$raw/openapi.json"
scp "${ssh_opts[@]}" "$target:$remote/server.py" "$raw/server.py"
scp "${ssh_opts[@]}" "$target:$remote/static/index.html" "$raw/static/index.html"
scp "${ssh_opts[@]}" "$target:/etc/systemd/system/clinostat.service" "$raw/clinostat.service"
ssh "${ssh_opts[@]}" "$target" \
  "cd '$remote' && find . -type d \\( -name .git -o -name __pycache__ -o -name runs -o -name backups -o -name tests \\) -prune -o -type f -name '*.py' -print | LC_ALL=C sort | head -n 201" \
  >"$raw/python_manifest.txt"
[[ "$(wc -l <"$raw/python_manifest.txt" | tr -d ' ')" -le 200 ]] || {
  echo "Python source manifest exceeds the 200-file safety bound" >&2
  exit 2
}
mkdir -p "$raw/sources"
while IFS= read -r source_name; do
  [[ "$source_name" =~ ^\./[A-Za-z0-9_./-]+\.py$ ]] || {
    echo "unsafe Python source path in manifest: $source_name" >&2
    exit 2
  }
  relative_name="${source_name#./}"
  mkdir -p "$raw/sources/$(dirname "$relative_name")"
  scp "${ssh_opts[@]}" "$target:$remote/$relative_name" "$raw/sources/$relative_name"
done <"$raw/python_manifest.txt"
[[ -n "$(find "$raw/sources" -type f -name '*.py' -print -quit)" ]] || {
  echo "Python source manifest was empty" >&2
  exit 2
}

ws_path="$("$repo_root/deploy/build_pi_baseline.py" --discover-ws \
  "$raw/server.py" "$raw/static/index.html")"
command -v websocat >/dev/null || {
  echo "websocat is required locally for dependency-free WebSocket capture" >&2
  exit 2
}
ws_target_seconds=60
ws_minimum_seconds=55
if [[ "${CAPTURE_TEST_SHORT_WS:-0}" == 1 ]]; then
  [[ "$PI_HOST" == "fixture.invalid" ]] || {
    echo "short WebSocket clock is restricted to the fixture.invalid test host" >&2
    exit 2
  }
  ws_target_seconds=1
  ws_minimum_seconds=0.5
fi
ssh "${ssh_opts[@]}" -N -L 127.0.0.1:18080:127.0.0.1:8000 "$target" &
tunnel_pid=$!
sleep 1
"$repo_root/deploy/build_pi_baseline.py" --capture-ws \
  "$raw/websocket_samples.jsonl" "$raw/ws_capture.json" \
  "ws://127.0.0.1:18080$ws_path" "$ws_target_seconds" "$ws_minimum_seconds" 3
command kill "$tunnel_pid" 2>/dev/null || true
tunnel_pid=""

: >"$raw/status_samples.jsonl"
for sample_number in 1 2 3; do
  ssh "${ssh_opts[@]}" "$target" \
    "curl --fail --silent http://127.0.0.1:8000/api/control/status" \
    >>"$raw/status_samples.jsonl"
  printf '\n' >>"$raw/status_samples.jsonl"
  [[ $sample_number -eq 3 ]] || sleep 1
done

python3 - "$raw" "$stamp" <<'PY'
import json, pathlib, sys
raw, stamp = pathlib.Path(sys.argv[1]), sys.argv[2]
(raw / "capture.json").write_text(json.dumps({
    "captured_at": stamp,
    "source_path": "/home/aiworker-1/clinostat",
    "source_revision": (raw / "git_head.txt").read_text().strip(),
    "remote_actions": "read-only",
    "status_path": "/api/control/status",
}, indent=2, sort_keys=True) + "\n")
PY
(cd "$raw" && find . -type f ! -name sha256sums.txt -print0 | sort -z |
  xargs -0 shasum -a 256) >"$raw/sha256sums.txt"

patterns='gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|password[[:space:]]*[:=][[:space:]]*[^[:space:]]+|authorization[[:space:]]*:|set-cookie[[:space:]]*:|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|https?://[^/@:]+:[^/@]+@'
scan_tree() {
  tree=$1
  strong=0
  if command -v gitleaks >/dev/null; then
    gitleaks detect --no-git --source "$tree" --exit-code 1
    strong=1
  fi
  if command -v trufflehog >/dev/null; then
    trufflehog filesystem --fail --no-update "$tree"
    strong=1
  fi
  [[ $strong -eq 1 ]] || {
    echo "live capture requires gitleaks or trufflehog; no override is supported" >&2
    return 1
  }
  if LC_ALL=C grep -EIRn "$patterns" "$tree"; then
    echo "secret-like content found; capture publication rejected" >&2
    return 1
  fi
}

# Fail closed: raw is scanned before any sanitized tree is constructed.
scan_tree "$raw"
"$repo_root/deploy/build_pi_baseline.py" --build "$raw" "$temporary" "$stamp"
scan_tree "$temporary"
(cd "$temporary" && find . -type f ! -name sha256sums.txt -print0 | sort -z |
  xargs -0 shasum -a 256) >"$temporary/sha256sums.txt"
(cd "$temporary" && shasum -a 256 -c sha256sums.txt)

mkdir -p "$work_tmp/static"
cp "$temporary/server.py" "$work_tmp/server.py"
cp "$temporary/static/index.html" "$work_tmp/static/index.html"
mv "$temporary" "$safe"
published=1
rm -rf "$repo_root/clinostat_extension/work"
mv "$work_tmp" "$repo_root/clinostat_extension/work"
published=0
echo "sanitized capture created: $safe"
