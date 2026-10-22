"""Watch the installed 1.8.11 update itself to 1.8.12 with no user action.

Triggers the agent's update check the same way the interface does (a bare signal),
then samples the runtime snapshot and the installation on disk until the running
build is the new one.
"""
import datetime
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_ipc

SNAPSHOT = Path(r'C:\ProgramData\youziauth\runtime.json')
INSTALL = Path(r'C:\Program Files (x86)\youziauth')
EXE = INSTALL / 'youziauth.exe'


def installed_version():
    for candidate in (INSTALL / 'VERSION', INSTALL / '_internal' / 'VERSION'):
        try:
            return candidate.read_text(encoding='utf-8').strip()
        except OSError:
            continue
    return '?'


def running_pids():
    try:
        out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq youziauth.exe', '/FO', 'CSV', '/NH'],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ''
    return ' '.join(part for part in out.split('","') if part.strip('"').isdigit())


def stamp():
    return datetime.datetime.now().strftime('%H:%M:%S')


print('BEFORE  version on disk :', installed_version())
print('BEFORE  youziauth pids  :', running_pids() or '(none)')
print('BEFORE  agent status    :', json.dumps(agent_ipc.read_snapshot(SNAPSHOT).update, ensure_ascii=False))
print()

reply = agent_ipc.send_command('youziauth-agent', agent_ipc.AgentCommand(command='check-update'),
                              timeout_ms=4000)
print(stamp(), 'sent check-update ->', reply.get('ok'))
print()

seen = []
deadline = time.monotonic() + 240
final = None
while time.monotonic() < deadline:
    try:
        update = agent_ipc.read_snapshot(SNAPSHOT).update
    except (OSError, ValueError):
        update = {}
    key = json.dumps(update, ensure_ascii=False, sort_keys=True)
    if update and key not in seen:
        seen.append(key)
        print('{:>8}  {:<12} v{} -> v{}  progress={:<3}  {}'.format(
            stamp(), update.get('state', '?'), update.get('current_version', ''),
            update.get('latest_version', ''), update.get('progress', 0),
            (update.get('message') or '')[:60]))
    if update.get('state') in ('installed', 'up_to_date', 'error') and update.get('latest_version') == '1.8.12':
        final = update
        break
    time.sleep(1.5)

print()
print('AFTER   version on disk :', installed_version())
time.sleep(6)
print('AFTER   youziauth pids  :', running_pids() or '(none)')
print('AFTER   agent update    :', json.dumps(final or agent_ipc.read_snapshot(SNAPSHOT).update,
                                            ensure_ascii=False))
