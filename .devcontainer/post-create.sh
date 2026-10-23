#!/usr/bin/env bash
set -euo pipefail

# New Docker volumes belong to root until the development user takes ownership.
sudo chown "$(id -u):$(id -g)" node_modules
uv sync --locked
npm ci
