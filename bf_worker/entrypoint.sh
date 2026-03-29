#!/bin/sh
# Write SSH key from environment variable to file if provided
if [ -n "$SSH_PRIVATE_KEY" ]; then
    mkdir -p /root/.ssh
    echo "$SSH_PRIVATE_KEY" > /root/.ssh/id_ed25519
    chmod 600 /root/.ssh/id_ed25519
    echo "StrictHostKeyChecking no" > /root/.ssh/config
    echo "UserKnownHostsFile /dev/null" >> /root/.ssh/config
fi

exec python bf_worker.py "$@"
