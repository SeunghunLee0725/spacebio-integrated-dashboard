# Clinostat extension

`deploy/capture_pi_baseline.sh` makes a read-only snapshot of the existing Pi
application. Raw evidence is written below the ignored `deploy/baseline/raw/`
directory, scanned for secrets, and never committed. Only the explicit,
sanitized contract allowlist is copied to `baseline/sanitized/<timestamp>/`.

Run it with `PI_HOST` and `PI_USER` set. SSH host verification is mandatory and
authentication remains interactive; credentials must never be supplied as
arguments or stored by this repository.

Live capture requires local `websocat` and at least one of `gitleaks` or
`trufflehog`. The WebSocket route is discovered from the captured Python/HTML
sources and observed through a local SSH port forward. Status is read three
times from `/api/control/status`; neither observation starts or changes control
hardware. Publication is fail-closed: raw evidence is scanned first, sanitized
output is built in a temporary sibling, rescanned, hashed, and atomically moved.

The `work/` directory contains exact editable copies of the captured
`server.py` and `static/index.html`. Baseline files are evidence and should not
be edited.
