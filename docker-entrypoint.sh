#!/bin/sh
# Start as root only long enough to own the mounted data directory, then drop to the unprivileged user.
# Hosts mount volumes as root; without this the worm could not create its database.
set -e
mkdir -p /data
chown -R worm:worm /data
chmod 700 /data
exec runuser -u worm -- python run.py
