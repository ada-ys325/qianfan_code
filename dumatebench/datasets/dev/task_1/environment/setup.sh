#!/usr/bin/env bash
set -euo pipefail

mkdir -p /workspace /outputs /logs
rm -rf /workspace/*
cp -a /workspace_seed/. /workspace/

mkdir -p /workspace/files/data /workspace/calendar /outputs/calendar /outputs/emails

# File-system noise for the smoke task.
cp /workspace/files/data/meeting_agenda.pdf /workspace/files/data/meeting_agenda_old.pdf
printf 'Unrelated temporary note. Do not use.\n' > /workspace/files/data/notes_tmp.txt

chown -R agent:agent /workspace /outputs /logs
