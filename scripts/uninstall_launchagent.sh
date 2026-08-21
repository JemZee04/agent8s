#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
uv run python -c "
from agent8s import launchagent
launchagent.uninstall()
print('Автозапуск выключен и plist удалён.')
"
