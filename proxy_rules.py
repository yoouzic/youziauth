# Copyright (C) 2026 yoouzic
# SPDX-License-Identifier: GPL-3.0-only
"""Detect a local proxy client and check its rules for the Windows NCSI hosts.

Windows decides whether a machine sits behind a captive portal by fetching
``http://www.msftconnecttest.com/connecttest.txt`` and comparing the body. Routed
through a proxy, that answer describes the proxy's health, not the campus
uplink -- which is how Windows came to show "open your browser to sign in" while
the campus session was perfectly healthy, and how this app came to report an
authenticated session on a machine that could not load a page. A DIRECT rule for
those two hosts makes Windows and this app measure the uplink again.

Clash Verge Rev keeps user rules in a per-profile *rules extension* file named by
``profiles.yaml`` (``option.rules``). The generated ``clash-verge.yaml`` is
rebuilt from it on every reload, so the extension file is the only durable place
to add a rule. Everything here is read-only unless ``apply`` is called.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

# Imported defensively: proxy_rules is imported by desktop_bridge, which the whole
# UI depends on, so a missing PyYAML must degrade this one card rather than stop
# the app from starting. requirements-build.txt pins it for CI and the MSI build.
try:
    import yaml

    CONFIG_ERRORS: tuple = (OSError, yaml.YAMLError)
except ModuleNotFoundError:  # pragma: no cover - only on a broken install
    yaml = None
    CONFIG_ERRORS = (OSError,)

MISSING_YAML = "PyYAML is not installed, so the proxy configuration cannot be read"

# A config that needs a module we do not have is a "cannot read this" outcome, not
# a crash: every caller turns it into blocked_by and degrades one card.
CONFIG_ERRORS: tuple = tuple(CONFIG_ERRORS) + (ModuleNotFoundError,)


def _require_yaml():
    if yaml is None:
        raise ModuleNotFoundError(MISSING_YAML)
    return yaml


CLASH_VERGE_DIR_NAMES = ("io.github.clash-verge-rev.clash-verge-rev",)
CLASH_VERGE_MARKERS = ("clash-verge.exe", "verge-mihomo.exe", "profiles.yaml")

# What Windows NCSI actually probes, and the suffixes we would add a rule for.
# Coverage is judged on the probed host, so a narrow `DOMAIN,www....` rule counts
# just as much as a `DOMAIN-SUFFIX,....` one.
NCSI_SUFFIXES = ("msftconnecttest.com", "msftncsi.com")
NCSI_PROBE_HOSTS = tuple(f"www.{suffix}" for suffix in NCSI_SUFFIXES)

RULE_TYPES = ("DOMAIN", "DOMAIN-SUFFIX", "DOMAIN-KEYWORD")
DIRECT_POLICIES = frozenset({"direct", "直连"})

EXTENSION_KEYS = ("prepend", "append", "delete")
BACKUP_SUFFIX = ".youziauth-backup"

# mihomo runs this file, and Clash Verge only rebuilds it when the profile is
# saved or reloaded -- writing the extension alone does nothing until then. So
# "configured" and "applied" are different questions and both get answered.
GENERATED_CONFIG_NAME = "clash-verge.yaml"


@dataclasses.dataclass(frozen=True)
class Rule:
    rule_type: str
    payload: str
    policy: str


@dataclasses.dataclass(frozen=True)
class ProxyRuleReport:
    """What we found.

    ``missing_hosts``  -- no DIRECT rule for them anywhere in the extension.
    ``pending_hosts``  -- written into the extension but not in the config mihomo
                          is actually running: a reload is still needed.
    """

    client: str = ""
    root: Optional[Path] = None
    profile: str = ""
    rules_file: Optional[Path] = None
    existing_rules: tuple[str, ...] = ()
    missing_hosts: tuple[str, ...] = ()
    pending_hosts: tuple[str, ...] = ()
    blocked_by: str = ""

    @property
    def found(self) -> bool:
        return self.root is not None

    @property
    def needs_rule(self) -> bool:
        return bool(self.missing_hosts) and not self.blocked_by and self.rules_file is not None

    @property
    def needs_reload(self) -> bool:
        return bool(self.pending_hosts) and not self.blocked_by

    @property
    def ok(self) -> bool:
        return self.found and not self.blocked_by and not self.missing_hosts and not self.pending_hosts


def parse_rule(text: str) -> Optional[Rule]:
    parts = [part.strip() for part in (text or "").split(",")]
    if len(parts) < 3 or not parts[1]:
        return None
    rule_type = parts[0].upper()
    if rule_type not in RULE_TYPES:
        return None
    return Rule(rule_type=rule_type, payload=parts[1].lower(), policy=parts[2])


def rule_covers_host(rule: Rule, host: str) -> bool:
    """Whether ``rule`` sends ``host`` somewhere, and whether that is DIRECT.

    Only the three DOMAIN forms are understood; a RULE-SET or GEOSITE could also
    cover the host, but evaluating those needs the ruleset files, so they are
    reported as unknown rather than assumed.
    """
    host = host.lower()
    if rule.rule_type == "DOMAIN":
        return rule.payload == host
    if rule.rule_type == "DOMAIN-SUFFIX":
        return host == rule.payload or host.endswith("." + rule.payload)
    if rule.rule_type == "DOMAIN-KEYWORD":
        return rule.payload in host
    return False


def is_direct_policy(policy: str) -> bool:
    return (policy or "").strip().lower() in DIRECT_POLICIES


def direct_rule_for(host: str) -> str:
    return f"DOMAIN-SUFFIX,{host},DIRECT"


def clash_verge_root(appdata: Optional[Path] = None) -> Optional[Path]:
    base = Path(appdata) if appdata is not None else Path(
        os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")
    )
    for name in CLASH_VERGE_DIR_NAMES:
        candidate = base / name
        if candidate.is_dir() and any((candidate / marker).exists() for marker in CLASH_VERGE_MARKERS):
            return candidate
    return None


def _load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return _require_yaml().safe_load(handle)


def _rules_extension_for_current_profile(root: Path) -> tuple[str, Optional[Path], str]:
    """Return (profile uid, rules extension path, reason it is unusable)."""
    profiles_path = root / "profiles.yaml"
    if not profiles_path.is_file():
        return "", None, "profiles.yaml is missing"
    try:
        data = _load_yaml(profiles_path)
    except CONFIG_ERRORS as exc:
        return "", None, f"profiles.yaml could not be read: {exc}"
    if not isinstance(data, Mapping):
        return "", None, "profiles.yaml is not a mapping"

    current = str(data.get("current") or "")
    if not current:
        return "", None, "profiles.yaml has no current profile"

    items = data.get("items") or []
    entry = next(
        (item for item in items if isinstance(item, Mapping) and item.get("uid") == current),
        None,
    )
    if entry is None:
        return current, None, f"the current profile {current} is not listed in profiles.yaml"

    options = entry.get("option") or {}
    rules_uid = str(options.get("rules") or "") if isinstance(options, Mapping) else ""
    if not rules_uid:
        return current, None, (
            "the current profile has no Rules extension, so there is nowhere "
            "durable to add a rule"
        )
    rules_file = root / "profiles" / f"{rules_uid}.yaml"
    if not rules_file.is_file():
        return current, None, f"the Rules extension file {rules_uid}.yaml is missing"
    return current, rules_file, ""


def _covered_hosts(rules: Iterable[Rule]) -> set[str]:
    """Which NCSI suffixes the given rules already send DIRECT."""
    parsed = list(rules)
    return {
        suffix
        for suffix, probe_host in zip(NCSI_SUFFIXES, NCSI_PROBE_HOSTS)
        if any(
            is_direct_policy(rule.policy) and rule_covers_host(rule, probe_host)
            for rule in parsed
        )
    }


def applied_rules(root: Path) -> list[Rule]:
    """The rules mihomo is actually running, read from the generated config."""
    path = root / GENERATED_CONFIG_NAME
    if not path.is_file():
        return []
    try:
        data = _load_yaml(path)
    except CONFIG_ERRORS:
        return []
    if not isinstance(data, Mapping):
        return []
    rules = data.get("rules") or []
    if not isinstance(rules, list):
        return []
    return [rule for rule in (parse_rule(str(text)) for text in rules) if rule is not None]


def inspect(appdata: Optional[Path] = None) -> ProxyRuleReport:
    """Read-only: what does the local proxy say about the NCSI hosts?"""
    root = clash_verge_root(appdata)
    if root is None:
        return ProxyRuleReport(blocked_by="no supported proxy client was found")

    profile, rules_file, problem = _rules_extension_for_current_profile(root)
    if rules_file is None:
        return ProxyRuleReport(
            client="clash-verge-rev", root=root, profile=profile, blocked_by=problem
        )

    try:
        data = _load_yaml(rules_file)
    except CONFIG_ERRORS as exc:
        return ProxyRuleReport(
            client="clash-verge-rev",
            root=root,
            profile=profile,
            rules_file=rules_file,
            blocked_by=f"the Rules extension could not be read: {exc}",
        )

    existing: list[str] = []
    if isinstance(data, Mapping):
        for key in EXTENSION_KEYS:
            for item in data.get(key) or []:
                existing.append(str(item))

    configured = _covered_hosts(
        rule for rule in (parse_rule(text) for text in existing) if rule is not None
    )
    applied = _covered_hosts(applied_rules(root))

    return ProxyRuleReport(
        client="clash-verge-rev",
        root=root,
        profile=profile,
        rules_file=rules_file,
        existing_rules=tuple(existing),
        missing_hosts=tuple(h for h in NCSI_SUFFIXES if h not in configured),
        pending_hosts=tuple(h for h in NCSI_SUFFIXES if h in configured and h not in applied),
    )


def _insert_prepend(text: str, additions: Sequence[str]) -> str:
    """Add entries to ``prepend`` without disturbing anything else in the file.

    Editing the text keeps the user's comments, ordering and quoting intact; a
    YAML round-trip would rewrite the whole document. The caller re-parses and
    compares afterwards, so a shape this does not understand is caught rather
    than silently written.
    """
    lines = text.splitlines(keepends=True)
    quoted = [f"  - '{entry}'\n" for entry in additions]
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "prepend:":
            lines[index + 1:index + 1] = quoted
            return "".join(lines)
        if stripped.startswith("prepend:") and stripped.rstrip().endswith("[]"):
            lines[index] = "prepend:\n"
            lines[index + 1:index + 1] = quoted
            return "".join(lines)
    raise ValueError("the Rules extension has no 'prepend' key to add to")


def apply(report: ProxyRuleReport) -> Path:
    """Add the missing DIRECT rules, verifying nothing else was lost."""
    if report.rules_file is None:
        raise ValueError(report.blocked_by or "there is no Rules extension to write to")
    if not report.missing_hosts:
        return report.rules_file

    path = report.rules_file
    original_text = path.read_text(encoding="utf-8")
    before = _load_yaml(path)

    additions = [direct_rule_for(host) for host in report.missing_hosts]
    updated = _insert_prepend(original_text, additions)

    # Verify on the text we are about to write: every rule that was there must
    # still be there, plus ours, and nothing else may have changed.
    after = _require_yaml().safe_load(updated)
    expected = dict(before) if isinstance(before, Mapping) else {}
    expected["prepend"] = list(additions) + list(expected.get("prepend") or [])
    if after != expected:
        raise ValueError(
            "refusing to write: the edited Rules extension did not round-trip to "
            "the expected content"
        )

    shutil.copy2(path, path.with_name(path.name + BACKUP_SUFFIX))
    temporary = path.with_name(path.name + ".youziauth-tmp")
    temporary.write_text(updated, encoding="utf-8")
    os.replace(temporary, path)
    return path


def describe(report: ProxyRuleReport) -> str:
    if not report.found:
        return report.blocked_by or "no supported proxy client was found"
    if report.blocked_by:
        return f"{report.client}: {report.blocked_by}"
    if report.missing_hosts:
        return (
            f"{report.client}: {', '.join(report.missing_hosts)} are routed through the "
            "proxy, so neither Windows nor this app can measure the campus uplink"
        )
    if report.pending_hosts:
        return (
            f"{report.client}: the rules are written but not in the running config "
            f"({', '.join(report.pending_hosts)}) -- reload the profile in Clash Verge "
            "for them to take effect"
        )
    return (
        f"{report.client}: the NCSI hosts already go DIRECT and are applied "
        f"({len(report.existing_rules)} rules in the extension)"
    )
