#!/usr/bin/env bash
# Pulls the latest version from GitHub and restarts the dashboard.
# Run as root from the install folder:  ./update.sh
set -euo pipefail
cd "$(dirname "$0")"
git pull --ff-only
./install.sh
