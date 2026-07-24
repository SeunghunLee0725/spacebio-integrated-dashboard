#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${PI_HOST:-}" || -z "${PI_USER:-}" ]]; then
  echo "PI_HOST and PI_USER are required" >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stamp="${CAPTURE_TIMESTAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
raw="$repo_root/deploy/baseline/raw/$stamp"
safe="$repo_root/clinostat_extension/baseline/sanitized/$stamp"
remote="/home/aiworker-1/clinostat"
target="${PI_USER}@${PI_HOST}"
ssh_opts=(-o StrictHostKeyChecking=yes)
mkdir -p "$raw/static"

# Every remote command below is observational. The script never changes service or device state.
ssh "${ssh_opts[@]}" "$target" "cd '$remote' && git rev-parse HEAD" >"$raw/git_head.txt"
ssh "${ssh_opts[@]}" "$target" "cd '$remote' && git status --porcelain=v1" >"$raw/git_status.txt"
ssh "${ssh_opts[@]}" "$target" "cd '$remote' && git diff --no-ext-diff" >"$raw/git_diff.patch"
ssh "${ssh_opts[@]}" "$target" "curl --fail --silent http://127.0.0.1:8000/openapi.json" >"$raw/openapi.json"
for sample_number in 1 2 3; do
  ssh "${ssh_opts[@]}" "$target" "curl --fail --silent http://127.0.0.1:8000/api/status" \
    >>"$raw/status_samples.jsonl"
  printf '\n' >>"$raw/status_samples.jsonl"
done
ssh "${ssh_opts[@]}" "$target" \
  "timeout 60 python3 -c \"import websocket; w=websocket.create_connection('ws://127.0.0.1:8000/ws'); [print(w.recv(),flush=True) for _ in range(600)]\"" \
  >"$raw/websocket_samples.jsonl" || true
scp "${ssh_opts[@]}" "$target:$remote/server.py" "$raw/server.py"
scp "${ssh_opts[@]}" "$target:$remote/static/index.html" "$raw/static/index.html"
scp "${ssh_opts[@]}" "$target:/etc/systemd/system/clinostat.service" "$raw/clinostat.service"

RAW="$raw" SAFE="$safe" STAMP="$stamp" python3 - <<'PY'
import ast, hashlib, html.parser, json, os, pathlib, re, shutil

raw = pathlib.Path(os.environ["RAW"])
safe = pathlib.Path(os.environ["SAFE"])

class IDs(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(); self.ids = {}
    def handle_starttag(self, _tag, attrs):
        for key, value in attrs:
            if key == "id" and value:
                self.ids[value] = self.ids.get(value, 0) + 1

parser = IDs()
parser.feed((raw / "static/index.html").read_text(errors="replace"))
(raw / "dom_ids.json").write_text(json.dumps({"ids": parser.ids}, indent=2, sort_keys=True))

def typename(value):
    if isinstance(value, bool): return "boolean"
    if isinstance(value, int): return "integer"
    if isinstance(value, float): return "number"
    if isinstance(value, str): return "string"
    if isinstance(value, list): return "array"
    if isinstance(value, dict): return "object"
    return None

messages = []
for line in (raw / "websocket_samples.jsonl").read_text(errors="replace").splitlines():
    try:
        value = json.loads(line)
        if isinstance(value, dict): messages.append(value)
    except json.JSONDecodeError:
        pass
if not messages:
    raise SystemExit("no WebSocket messages captured in bounded sample")
keys = set().union(*(message for message in messages))
properties = {}
for key in sorted(keys):
    values = [message.get(key) for message in messages]
    properties[key] = {
        "types": sorted({typename(value) for value in values if value is not None}),
        "nullable": any(value is None for value in values),
    }
(raw / "websocket_schema.json").write_text(json.dumps({
    "sample_window_seconds": 60, "hardware_started": False,
    "message_count": len(messages), "properties": properties,
}, indent=2, sort_keys=True))

aliases = {
    "TARGET_RPM": ("TARGET_RPM", "TARGET_SPEED", "DEFAULT_RPM"),
    "KP": ("KP", "K_P", "PROPORTIONAL_GAIN"),
    "KI": ("KI", "K_I", "INTEGRAL_GAIN"),
}
allowed_source_names = {name for names in aliases.values() for name in names}
tree = ast.parse((raw / "server.py").read_text())
source_constants = {}
for node in tree.body:
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value_node = node.value
        for target in targets:
            if isinstance(target, ast.Name) and target.id in allowed_source_names:
                value = ast.literal_eval(value_node)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise SystemExit(f"control constant {target.id} is not numeric")
                source_constants[target.id] = value
constants, mapping = {}, {}
for canonical, source_names in aliases.items():
    matches = [name for name in source_names if name in source_constants]
    if len(matches) > 1:
        raise SystemExit(f"ambiguous control constant {canonical}: {matches}")
    if matches:
        source_name = matches[0]
        constants[canonical] = source_constants[source_name]
        mapping[canonical] = {
            "source_name": source_name,
            "justification": "exact source name" if source_name == canonical
                             else f"explicit allowlisted synonym for {canonical}",
        }
missing = aliases.keys() - constants.keys()
if missing:
    raise SystemExit("required control constants absent: " + ", ".join(sorted(missing)))
(raw / "control_constants.json").write_text(json.dumps({
    "extraction": "python-ast-explicit-allowlist", "required": sorted(aliases),
    "constants": constants, "name_mapping": mapping,
}, indent=2, sort_keys=True))

# Pure reference calculation only: no import or execution of server.py and no constructors.
cases = []
for error, integral in ((0, 0), (1, 0), (-2, 0.5)):
    output = constants["KP"] * error + constants["KI"] * integral
    cases.append({"input": {"error": error, "integral": integral}, "output": output})
(raw / "control_golden.json").write_text(json.dumps({
    "formula": "KP * error + KI * integral", "hardware_imported": False,
    "constructor_strategy": "ast-pure-calculation", "cases": cases,
}, indent=2, sort_keys=True))

sanitized_samples = []
for index, line in enumerate((raw / "status_samples.jsonl").read_text().splitlines()):
    try: sample = json.loads(line)
    except json.JSONDecodeError: continue
    schema = {}
    for key, value in sample.items():
        schema[key] = {"type": typename(value), "nullable": value is None}
    timing = sample.get("loop_timing")
    sanitized_samples.append({
        "offset_seconds": index,
        "schema": schema,
        "loop_timing": {"type": typename(timing), "nullable": timing is None},
    })

safe.mkdir(parents=True)
(safe / "static").mkdir()
for name in ("openapi.json", "dom_ids.json", "websocket_schema.json",
             "control_constants.json", "control_golden.json", "server.py",
             "clinostat.service"):
    shutil.copy2(raw / name, safe / name)
shutil.copy2(raw / "static/index.html", safe / "static/index.html")
(safe / "status_samples.sanitized.jsonl").write_text(
    "".join(json.dumps(sample, sort_keys=True) + "\n" for sample in sanitized_samples))
head = (raw / "git_head.txt").read_text().strip()
capture_metadata = {
    "captured_at": os.environ["STAMP"], "source_path": "/home/aiworker-1/clinostat",
    "source_revision": head, "remote_actions": "read-only",
    "websocket_window_seconds": 60,
}
(raw / "capture.json").write_text(json.dumps(capture_metadata, indent=2, sort_keys=True))
(safe / "capture.json").write_text(json.dumps(capture_metadata, indent=2, sort_keys=True))
PY

(cd "$raw" && find . -type f ! -name sha256sums.txt -print0 | sort -z | xargs -0 shasum -a 256) \
  >"$raw/sha256sums.txt"

patterns='gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|password[[:space:]]*[:=][[:space:]]*[^[:space:]]+|authorization[[:space:]]*:|set-cookie[[:space:]]*:|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|https?://[^/@:]+:[^/@]+@'
if command -v gitleaks >/dev/null; then gitleaks detect --no-git --source "$raw" --exit-code 1; fi
if command -v trufflehog >/dev/null; then trufflehog filesystem --fail --no-update "$raw"; fi
if LC_ALL=C grep -EIRn "$patterns" "$raw"; then
  echo "secret-like content found in raw capture; sanitized capture rejected" >&2
  exit 3
fi

# Credential-bearing remote strings are not copied. Scan the allowlisted output independently.
if LC_ALL=C grep -EIRn "$patterns" "$safe"; then
  echo "secret-like content found in sanitized capture" >&2
  exit 4
fi
(cd "$safe" && find . -type f ! -name sha256sums.txt -print0 | sort -z | xargs -0 shasum -a 256) \
  >"$safe/sha256sums.txt"
mkdir -p "$repo_root/clinostat_extension/work/static"
cp "$safe/server.py" "$repo_root/clinostat_extension/work/server.py"
cp "$safe/static/index.html" "$repo_root/clinostat_extension/work/static/index.html"
echo "sanitized capture created: $safe"
