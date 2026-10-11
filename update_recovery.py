# Copyright (C) 2026 yoouzic
# SPDX-License-Identifier: GPL-3.0-only

"""Bounded, passive recovery of the detached installer's completion record.

These records only describe a past attempt. They never authorize an installation,
contain executable commands, or choose a package for the privileged updater.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path


INSTALL_RESULT_FILE = 'install-result.json'
CONSUMED_RESULT_FILE = 'install-result-consumed.json'
RESULT_LIMIT = 4096
_RESULT_FIELDS = frozenset({'code', 'relaunched', 'healthy', 'agent_restarted', 'launch_exit_code'})


@dataclasses.dataclass(frozen=True)
class InstallResult:
    version: str
    result: dict
    checked: str
    fingerprint: str


def timestamp(value: object) -> dt.datetime | None:
    """Parse a bounded ISO timestamp with an explicit timezone."""
    if not isinstance(value, str) or not 0 < len(value) <= 64:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
        return parsed if parsed.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def _unique_object(pairs):
    values = {}
    for key, value in pairs:
        if key in values:
            raise ValueError('duplicate JSON key')
        values[key] = value
    return values


def _read_object(path: Path):
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(RESULT_LIMIT + 1)
        if len(raw) > RESULT_LIMIT:
            return None
        value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique_object)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError, RecursionError):
        return None


def read_install_result(path: Path) -> InstallResult | None:
    value = _read_object(path)
    if value is None or set(value) != {'version', 'result', 'checked'}:
        return None
    version, result, checked = value['version'], value['result'], value['checked']
    if (not isinstance(version, str) or re.fullmatch(
            r'(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,4})', version) is None
            or any(part > limit for part, limit in zip(map(int, version.split('.')), (255, 255, 65535)))
            or timestamp(checked) is None or not isinstance(result, dict)):
        return None
    if 'error' in result:
        if set(result) != {'error'} or type(result['error']) is not int or not 0 < result['error'] <= 0xFFFFFFFF:
            return None
    else:
        if ('code' not in result or set(result) - _RESULT_FIELDS
                or type(result['code']) is not int or not -0x80000000 <= result['code'] <= 0xFFFFFFFF):
            return None
        for field in ('relaunched', 'healthy', 'agent_restarted'):
            if field in result and type(result[field]) is not bool:
                return None
        if ('launch_exit_code' in result and (type(result['launch_exit_code']) is not int
                or not -0x80000000 <= result['launch_exit_code'] <= 0xFFFFFFFF)):
            return None
    canonical = json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return InstallResult(version, result, checked, hashlib.sha256(canonical).hexdigest())


def is_consumed(path: Path, record: InstallResult) -> bool:
    value = _read_object(path)
    return value == {'sha256': record.fingerprint}


def mark_consumed(path: Path, record: InstallResult) -> bool:
    """Remember the exact record atomically, including any later health fields.

    Keep the worker's record intact for diagnosis. A new attempt (or a subsequent
    health result with the same timestamp) gets a different fingerprint.
    """
    path = Path(path)
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump({'sha256': record.fingerprint}, stream, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return True
    except (OSError, ValueError, TypeError):
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
