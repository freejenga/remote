#!/usr/bin/env bash
# Cross-platform helper: format Python code in the project
python -m isort .
python -m black .
