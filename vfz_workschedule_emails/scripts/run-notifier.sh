#!/usr/bin/env bash
#
# Cron wrapper for the Wrike off-day approver notifier.
#
#   crontab entry:  5 7-17 * * 1-5  /srv/vfz_work_schedule/vfz_workschedule_emails/scripts/run-notifier.sh
#
# Cron gives a job almost no environment: no PATH to speak of, no shell profile,
# no working directory. Everything the run needs is therefore set explicitly
# here rather than inherited.
set -euo pipefail

# The integration this script belongs to, and the repo root above it. The
# virtualenv lives at the REPO root and is shared by all three integrations, so
# looking for it beside this script (as an earlier version did) found nothing
# and silently fell back to the system python -- which has none of the
# dependencies, so the run failed on `import mysql.connector`.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INTEGRATION_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$INTEGRATION_DIR/.." && pwd)"

# Run from the integration directory: every module resolves ../shared relative
# to its own file, but the CLI expects to be invoked from here.
cd "$INTEGRATION_DIR"

# Which interpreter to run. In order: an explicit override, the repo-root
# .venv, any other virtualenv sitting at the repo root, a per-integration one,
# then whatever is on PATH.
#
# The third rule is not decoration. A venv does not have to be called .venv --
# this box's is `env_work_schedule` -- and a crontab line naming
# `$REPO_ROOT/.venv/bin/python` on such a box exits 127 before Python starts.
# With `>/dev/null 2>&1` on the entry that is completely silent: cron logs the
# CMD, nothing runs, and the table quietly stops being updated. Finding the
# venv here rather than in the crontab is what keeps that from recurring.
#
# A virtualenv is identified by its pyvenv.cfg, not by its name.
PYTHON="${NOTIFIER_PYTHON:-${VFZ_PYTHON:-}}"
PYTHON_SOURCE="explicit override"

if [[ -z "$PYTHON" ]]; then
    if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
        PYTHON="$REPO_ROOT/.venv/bin/python"
        PYTHON_SOURCE="repo venv"
    elif [[ -x "$REPO_ROOT/.venv/Scripts/python.exe" ]]; then
        PYTHON="$REPO_ROOT/.venv/Scripts/python.exe"
        PYTHON_SOURCE="repo venv (windows)"
    else
        # Sorted, so a box with two of them picks the same one every run
        # rather than depending on directory order.
        for candidate in "$REPO_ROOT"/*/pyvenv.cfg; do
            [[ -e "$candidate" ]] || continue
            venv_dir="$(dirname "$candidate")"
            if [[ -x "$venv_dir/bin/python" ]]; then
                PYTHON="$venv_dir/bin/python"
                PYTHON_SOURCE="repo venv ($(basename "$venv_dir"))"
                break
            fi
        done
    fi
fi

if [[ -z "$PYTHON" && -x "$INTEGRATION_DIR/.venv/bin/python" ]]; then
    PYTHON="$INTEGRATION_DIR/.venv/bin/python"
    PYTHON_SOURCE="integration venv"
fi

if [[ -z "$PYTHON" ]]; then
    PYTHON="$(command -v python3 || true)"
    PYTHON_SOURCE="PATH fallback - dependencies may be missing"
fi

if [[ -z "${PYTHON:-}" ]]; then
    echo "$(date -Is) FATAL: no python interpreter found" >&2
    exit 127
fi

LOG_DIR="${NOTIFIER_LOG_DIR:-$INTEGRATION_DIR/logs}"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/notifier.log"

# The interpreter exists -- but the wrong one exists too, and a system python3
# gets all the way to `import mysql.connector` before failing. Checking here
# turns that into one line naming the interpreter, rather than a traceback that
# a cron entry redirecting to /dev/null would throw away.
if ! "$PYTHON" -c "import mysql.connector, requests" >/dev/null 2>&1; then
    s_hint="Point NOTIFIER_PYTHON at the right interpreter, or install requirements.txt into this one."
    echo "$(date -Is) FATAL: $PYTHON ($PYTHON_SOURCE) cannot import the dependencies. $s_hint" | tee -a "$LOG_FILE" >&2
    exit 127
fi

LOCK_FILE="${NOTIFIER_LOCK_FILE:-/tmp/wrike-offday-notifier.lock}"

# Single instance only. A run that overruns the hour must not be joined by the
# next one: two concurrent runs could both read an empty notification history
# and post the same comment twice.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "$(date -Is) SKIP: previous run still in progress" >>"$LOG_FILE"
    exit 0
fi

{
    echo "----- $(date -Is) starting ($PYTHON_SOURCE) -----"
    # `|| status=$?` is required: under `set -e` a bare non-zero exit would
    # abort the block before the status could be logged.
    status=0
    "$PYTHON" run_notifier.py --watch "$@" || status=$?
    echo "----- $(date -Is) finished, exit $status -----"
    exit $status
} >>"$LOG_FILE" 2>&1
