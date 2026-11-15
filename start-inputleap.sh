#!/bin/bash
# ==============================================================================
# InputLeap Client Startup Runner
# ==============================================================================

# Launch InputLeap client in background if not already running
pgrep -x input-leapc >/dev/null || input-leapc --name compooter 10.42.0.183 &
