#!/bin/sh
# Rig job of the display's sound: the touched unit tests and lint in the rig's venv, then the audio proof against the game.
# Usage: sh ci/display_audio_job.sh WORKDIR RAGEWARS ROM [--curl]   (--curl: no tests, no browser; the route alone)
set -u
PATH=/home/abu/venv-decomp/bin:$PATH
case " $* " in *" --curl "*) exec python -u ci/display_audio_proof.py "$@" ;; esac
PYTHONPATH=src python -m pytest -q -p no:cacheprovider tests/test_display.py tests/test_viewer.py tests/test_viewer_audio.py \
  tests/test_tas_serve.py 2>&1 | tail -5
python -m ruff check src/abu_decomp/display.py src/abu_decomp/viewer.py src/abu_decomp/tas_serve.py ci/display_audio_proof.py \
  tests/test_display.py tests/test_viewer_audio.py tests/test_tas_serve.py 2>&1 | tail -8
python -m ruff format --check src/abu_decomp ci/display_audio_proof.py tests 2>&1 | tail -4
exec python -u ci/display_audio_proof.py "$@"
