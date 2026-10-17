#!/usr/bin/env bash
# Execute this script; sourcing it would apply shell options to the caller.
if [[ ${BASH_SOURCE[0]} != "$0" ]]; then
    printf '%s\n' 'Do not source this installer. Run: bash /path/to/install.sh' >&2
    return 1
fi
set -euo pipefail

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PYTHONDONTWRITEBYTECODE=1

# Never bootstrap packages ahead of the wizard's approved plan.
if ! /usr/bin/python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    printf '%s\n' 'The installer wizard requires system Python 3.11+ (Debian 12+, Ubuntu 24.04+, Raspberry Pi OS Bookworm+).' 'No packages or files have been changed.' >&2
    exit 1
fi
exec /usr/bin/python3 "$SOURCE_DIR/scripts/install.py" "$@"
