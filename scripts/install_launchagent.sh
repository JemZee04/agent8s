#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
uv run python -c "
from agent8s import launchagent
path = launchagent.install()
print(f'Установлен и загружен: {path}')
print('Бот будет сам подниматься при логине и перезапускаться при краше.')
"
