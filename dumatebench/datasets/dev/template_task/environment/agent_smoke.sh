#!/usr/bin/env bash
set -euo pipefail

exec > >(tee -a /logs/agent_smoke.log) 2>&1

echo "[agent] starting DuMateBench template task"
id
pwd

echo "[agent] verify network-stack fault injection"
sleep 1
if getent hosts deb.debian.org; then
  echo "[agent] network fault window may have already cleared"
else
  echo "[agent] expected DNS/network fault observed"
fi

echo "[agent] wait for configured startup network fault window to clear"
sleep 26

echo "[agent] install missing OS and Python dependencies"
sudo apt-get update
sudo apt-get install -y --no-install-recommends tesseract-ocr
python3 -m pip install --no-cache-dir --break-system-packages \
  icalendar requests pypdf pillow pytesseract

echo "[agent] verify wrappers are first in PATH"
echo "[agent] PATH=${PATH}"
which tesseract || true
readlink -f "$(which tesseract)" || true
which calendar_write || true
readlink -f "$(which calendar_write)" || true
which ocr_extract || true
readlink -f "$(which ocr_extract)" || true

echo "[agent] inspect workspace"
find /workspace -maxdepth 4 -type f | sort

PDF="/workspace/files/data/meeting_agenda.pdf"
IMG_PREFIX="/workspace/work/meeting_agenda"
mkdir -p /workspace/work /workspace/calendar /outputs/calendar

echo "[agent] convert PDF to image"
pdftoppm -png -singlefile "${PDF}" "${IMG_PREFIX}"

echo "[agent] run OCR with fallback"
if tesseract "${IMG_PREFIX}.png" /workspace/work/meeting_agenda_ocr; then
  echo "[agent] OCR succeeded on first attempt"
else
  echo "[agent] expected OCR wrapper failure observed; retrying"
  tesseract "${IMG_PREFIX}.png" /workspace/work/meeting_agenda_ocr
fi

echo "[agent] write calendar through tool wrapper"
if calendar_write --input-text /workspace/work/meeting_agenda_ocr.txt --output /workspace/calendar/Alice.ics; then
  echo "[agent] calendar write succeeded on first attempt"
else
  echo "[agent] expected calendar wrapper failure observed; retrying"
  calendar_write --input-text /workspace/work/meeting_agenda_ocr.txt --output /workspace/calendar/Alice.ics
fi

echo "[agent] write final calendar artifact to /outputs"
calendar_write --input-text /workspace/work/meeting_agenda_ocr.txt --output /outputs/calendar/Alice.ics

echo "[agent] show tool fault log if present"
if [ -f /logs/tool_faults.jsonl ]; then
  cat /logs/tool_faults.jsonl
else
  echo "[agent] missing /logs/tool_faults.jsonl"
fi

echo "[agent] final outputs"
find /outputs -maxdepth 4 -type f -print -exec ls -lh {} \;
