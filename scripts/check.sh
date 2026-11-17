#!/bin/sh
# 在锁定的新环境中检查格式、规则、说明、类型及常规行为。
set -eu
cd "$(dirname "$0")/.."
uv run --locked --all-packages --all-extras ruff format --check src packages/inkparse/src packages/inkstore/src packages/inkrag-core/src packages/inkrag-platform/src tests typings examples
uv run --locked --all-packages --all-extras ruff check src packages/inkparse/src packages/inkstore/src packages/inkrag-core/src packages/inkrag-platform/src tests typings examples
uv run --locked --all-packages --all-extras ruff check src packages/inkparse/src packages/inkstore/src packages/inkrag-core/src packages/inkrag-platform/src --select D100,D101,D102,D103,D104,D105,D106,D107
uv run --locked --all-packages --all-extras pyright --warnings
uv run --locked --all-packages --all-extras pytest

(cd web && npm run check && npm test && npm run format:check && npm run build)
