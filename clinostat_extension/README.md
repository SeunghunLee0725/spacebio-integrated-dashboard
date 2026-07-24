# Clinostat extension

`deploy/capture_pi_baseline.sh` makes a read-only snapshot of the existing Pi
application. Raw evidence is written below the ignored `deploy/baseline/raw/`
directory, scanned for secrets, and never committed. Only the explicit,
sanitized contract allowlist is copied to `baseline/sanitized/<timestamp>/`.

Run it with `PI_HOST` and `PI_USER` set. SSH host verification is mandatory and
authentication remains interactive; credentials must never be supplied as
arguments or stored by this repository.

The `work/` directory contains exact editable copies of the captured
`server.py` and `static/index.html`. Baseline files are evidence and should not
be edited.
