# datasette-tailscale

[![PyPI](https://img.shields.io/pypi/v/datasette-tailscale.svg)](https://pypi.org/project/datasette-tailscale/)
[![Changelog](https://img.shields.io/github/v/release/datasette/datasette-tailscale?include_prereleases&label=changelog)](https://github.com/datasette/datasette-tailscale/releases)
[![Tests](https://github.com/datasette/datasette-tailscale/actions/workflows/test.yml/badge.svg)](https://github.com/datasette/datasette-tailscale/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/datasette/datasette-tailscale/blob/main/LICENSE)

Run a Datasette instance on a Tailscale network

## Installation

Install this plugin in the same environment as Datasette.
```bash
datasette install datasette-tailscale
```
## Usage

Usage instructions go here.

## Development

To set up this plugin locally, first checkout the code. You can confirm it is available like this:
```bash
cd datasette-tailscale
# Confirm the plugin is visible
uv run datasette plugins
```
To run the tests:
```bash
uv run pytest
```
