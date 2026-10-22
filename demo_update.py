"""Trigger one unattended update check and narrate what the agent does."""
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_ipc

SNAPSHOT = Path(r'C:\ProgramData\youziauth\runtime.json')
VERSION_FILE = Path(r'C:\Program Files (x86)\youziauth\_internal\VERSION')
TARGET = '1.8.26'


def ui_pids():
    try:
        out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq youziauth.exe', '/FO', 'CSV', '/NH'],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ''
    found = []
    for row in out.splitlines():
        cells = [c.strip('"') for c in row.split('","')]
        if len(cells) > 1 and cells[1].isdigit():
            found.append(cells[1])
    return ' '.join(found)


def installed():
    try:
        return VERSION_FILE.read_text(encoding='utf-8').strip()
    except OSError:
        return '?'


print('starting version :', installed())
reply = agent_ipc.send_command('youziauth-agent', agent_ipc.AgentCommand(command='check-update'),
                              timeout_ms=4000)
print('check-update sent:', reply.get('ok'))
print()

last = None
for step in range(160):
    time.sleep(5)
    try:
        update = agent_ipc.read_snapshot(SNAPSHOT).update
    except (OSError, ValueError):
        continue
    key = json.dumps(update, ensure_ascii=False, sort_keys=True)
    if update and key != last:
        last = key
        print('t={:>4}s  {:<11} v{} -> v{}  {:>3}%  UI pids: {}'.format(
            (step + 1) * 5, update.get('state'), update.get('current_version'),
            update.get('latest_version'), update.get('progress'), ui_pids() or '(window gone)'))
        if update.get('detail'):
            print('            detail:', str(update['detail'])[:150])
    if update.get('state') == 'installed' and update.get('latest_version') == TARGET:
        break
    if update.get('state') == 'error' and update.get('detail'):
        break

print()
print('final version    :', installed())
print('running          :', ui_pids() or '(none)')
