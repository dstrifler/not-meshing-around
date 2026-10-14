#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

case "${1:-}" in
    -h|--help)
        cat <<'EOF'
Usage: sudo bash install.sh [--dry-run]

Install not-meshing-around on Debian/Ubuntu or Raspberry Pi OS with systemd.
Prompts for installation directory, system group/user, and TCP or serial radio.
Defaults: /opt/meshbot, group meshbot, user meshbot.
--dry-run collects and validates inputs, then prints the plan without changes.
EOF
        exit 0
        ;;
    ""|--dry-run) ;;
    *) echo "Unknown option. Run: bash install.sh --help" >&2; exit 1 ;;
esac
[[ $# -le 1 ]] || { echo "Too many arguments." >&2; exit 1; }
[[ $EUID -eq 0 ]] || { echo "Run this installer as root or with sudo: sudo bash install.sh" >&2; exit 1; }
[[ -f /etc/debian_version ]] && command -v apt-get >/dev/null || {
    echo "This installer supports Debian/Ubuntu and Raspberry Pi OS." >&2; exit 1;
}
if [[ ${1:-} != --dry-run && ! -d /run/systemd/system ]]; then
    echo "A running systemd host is required; this is not a container installer." >&2
    exit 1
fi

if ! /usr/bin/python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    [[ ${1:-} != --dry-run ]] || { echo "Dry run requires Python 3.11+ already installed." >&2; exit 1; }
    echo "Bootstrapping system Python and venv support..."
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv ca-certificates
    /usr/bin/python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || {
        echo "Python 3.11+ is required. Use Debian 12+, Ubuntu 24.04+, or Raspberry Pi OS Bookworm+." >&2
        exit 1
    }
fi
exec /usr/bin/python3 "$SOURCE_DIR/scripts/install.py" "$@"
