import tempfile
import unittest
from pathlib import Path

try:
    import proxy_rules
except ModuleNotFoundError as exc:  # pragma: no cover - mirror the other suites
    proxy_rules = None
    import_error = exc
else:
    import_error = None


PROFILES_YAML = """\
# Profiles Config for Clash Verge

current: SUB001
items:
- uid: SUB001
  type: remote
  name: demo
  file: SUB001.yaml
  option:
    rules: RULES01
- uid: RULES01
  type: rules
  file: RULES01.yaml
"""

RULES_EXTENSION = """\
prepend:
  - 'DOMAIN-SUFFIX,api.example.com,DIRECT'
append: []
delete: []
"""


class FakeClash:
    """A Clash Verge Rev directory tree in a temp dir."""

    def __init__(
        self,
        rules_extension=RULES_EXTENSION,
        profiles=PROFILES_YAML,
        markers=True,
        generated=None,
    ):
        self._temporary = tempfile.TemporaryDirectory()
        self.appdata = Path(self._temporary.name)
        self.root = self.appdata / proxy_rules.CLASH_VERGE_DIR_NAMES[0]
        (self.root / "profiles").mkdir(parents=True)
        (self.root / "profiles.yaml").write_text(profiles, encoding="utf-8")
        if rules_extension is not None:
            (self.root / "profiles" / "RULES01.yaml").write_text(
                rules_extension, encoding="utf-8"
            )
        if markers:
            (self.root / "clash-verge.exe").write_bytes(b"")
        if generated is not None:
            (self.root / proxy_rules.GENERATED_CONFIG_NAME).write_text(
                generated, encoding="utf-8"
            )

    def close(self):
        self._temporary.cleanup()


BOTH_RULES_EXTENSION = (
    "prepend:\n"
    "  - 'DOMAIN-SUFFIX,msftncsi.com,DIRECT'\n"
    "  - 'DOMAIN-SUFFIX,msftconnecttest.com,DIRECT'\n"
    "append: []\n"
    "delete: []\n"
)

BOTH_RULES_APPLIED = (
    "mode: rule\n"
    "rules:\n"
    "  - 'DOMAIN-SUFFIX,msftncsi.com,DIRECT'\n"
    "  - 'DOMAIN-SUFFIX,msftconnecttest.com,DIRECT'\n"
    "  - 'MATCH,PROXY'\n"
)


class RuleParsingTests(unittest.TestCase):
    def test_parses_the_three_domain_forms(self):
        self.assertIsNotNone(proxy_rules, import_error)

        self.assertEqual(
            proxy_rules.parse_rule("DOMAIN-SUFFIX,msftconnecttest.com,DIRECT").payload,
            "msftconnecttest.com",
        )
        self.assertEqual(proxy_rules.parse_rule("DOMAIN,a.com,DIRECT").rule_type, "DOMAIN")
        self.assertEqual(
            proxy_rules.parse_rule("DOMAIN-KEYWORD,ncsi,DIRECT").rule_type, "DOMAIN-KEYWORD"
        )

    def test_ignores_rule_types_it_cannot_evaluate(self):
        self.assertIsNotNone(proxy_rules, import_error)

        self.assertIsNone(proxy_rules.parse_rule("RULE-SET,ads,REJECT"))
        self.assertIsNone(proxy_rules.parse_rule("GEOIP,CN,DIRECT"))
        self.assertIsNone(proxy_rules.parse_rule("MATCH,PROXY"))
        self.assertIsNone(proxy_rules.parse_rule(""))

    def test_suffix_rule_covers_the_host_and_its_subdomains(self):
        self.assertIsNotNone(proxy_rules, import_error)
        rule = proxy_rules.parse_rule("DOMAIN-SUFFIX,msftconnecttest.com,DIRECT")

        self.assertTrue(proxy_rules.rule_covers_host(rule, "msftconnecttest.com"))
        self.assertTrue(proxy_rules.rule_covers_host(rule, "www.msftconnecttest.com"))
        self.assertFalse(proxy_rules.rule_covers_host(rule, "notmsftconnecttest.com"))

    def test_only_a_direct_policy_counts(self):
        self.assertIsNotNone(proxy_rules, import_error)

        self.assertTrue(proxy_rules.is_direct_policy("DIRECT"))
        self.assertTrue(proxy_rules.is_direct_policy("直连"))
        self.assertFalse(proxy_rules.is_direct_policy("PROXY"))
        self.assertFalse(proxy_rules.is_direct_policy("七七云"))


class InspectionTests(unittest.TestCase):
    def test_reports_the_missing_hosts_when_the_rule_is_absent(self):
        self.assertIsNotNone(proxy_rules, import_error)
        clash = FakeClash()
        try:
            report = proxy_rules.inspect(clash.appdata)
        finally:
            clash.close()

        self.assertTrue(report.found)
        self.assertEqual(report.profile, "SUB001")
        self.assertEqual(report.missing_hosts, proxy_rules.NCSI_SUFFIXES)
        self.assertTrue(report.needs_rule)
        self.assertEqual(report.rules_file.name, "RULES01.yaml")

    def test_reports_nothing_missing_when_the_rules_are_there(self):
        self.assertIsNotNone(proxy_rules, import_error)
        clash = FakeClash(
            rules_extension=BOTH_RULES_EXTENSION, generated=BOTH_RULES_APPLIED
        )
        try:
            report = proxy_rules.inspect(clash.appdata)
        finally:
            clash.close()

        self.assertEqual(report.missing_hosts, ())
        self.assertEqual(report.pending_hosts, ())
        self.assertFalse(report.needs_rule)
        self.assertTrue(report.ok)
        self.assertIn("already go DIRECT and are applied", proxy_rules.describe(report))

    def test_configured_but_not_applied_asks_for_a_reload(self):
        self.assertIsNotNone(proxy_rules, import_error)
        # Written into the extension, but the config mihomo runs has not been
        # rebuilt -- exactly what a raw file write produces.
        clash = FakeClash(
            rules_extension=BOTH_RULES_EXTENSION,
            generated="mode: rule\nrules:\n  - 'MATCH,PROXY'\n",
        )
        try:
            report = proxy_rules.inspect(clash.appdata)
        finally:
            clash.close()

        self.assertEqual(report.missing_hosts, ())
        self.assertEqual(report.pending_hosts, proxy_rules.NCSI_SUFFIXES)
        self.assertFalse(report.needs_rule)
        self.assertTrue(report.needs_reload)
        self.assertFalse(report.ok)
        self.assertIn("reload the profile", proxy_rules.describe(report))

    def test_a_stale_generated_config_does_not_count_as_applied(self):
        self.assertIsNotNone(proxy_rules, import_error)
        clash = FakeClash(rules_extension=BOTH_RULES_EXTENSION, generated=None)
        try:
            report = proxy_rules.inspect(clash.appdata)
        finally:
            clash.close()

        # No generated config at all: we cannot claim it is applied.
        self.assertEqual(report.pending_hosts, proxy_rules.NCSI_SUFFIXES)
        self.assertTrue(report.needs_reload)

    def test_a_rule_only_in_the_running_config_is_still_reported_as_missing(self):
        self.assertIsNotNone(proxy_rules, import_error)
        # It works right now, but nothing durable carries it: the next profile
        # reload rebuilds the config from the extension and the rule is gone. So
        # it still has to be written there.
        clash = FakeClash(
            rules_extension="prepend: []\nappend: []\ndelete: []\n",
            generated=BOTH_RULES_APPLIED,
        )
        try:
            report = proxy_rules.inspect(clash.appdata)
        finally:
            clash.close()

        self.assertEqual(report.missing_hosts, proxy_rules.NCSI_SUFFIXES)
        self.assertEqual(report.pending_hosts, ())
        self.assertTrue(report.needs_rule)

    def test_a_proxy_policy_does_not_count_as_direct(self):
        self.assertIsNotNone(proxy_rules, import_error)
        clash = FakeClash(
            rules_extension=(
                "prepend:\n"
                "  - 'DOMAIN-SUFFIX,msftconnecttest.com,PROXY'\n"
                "  - 'DOMAIN-SUFFIX,msftncsi.com,DIRECT'\n"
                "append: []\n"
                "delete: []\n"
            )
        )
        try:
            report = proxy_rules.inspect(clash.appdata)
        finally:
            clash.close()

        self.assertEqual(report.missing_hosts, ("msftconnecttest.com",))

    def test_an_append_rule_also_counts(self):
        self.assertIsNotNone(proxy_rules, import_error)
        clash = FakeClash(
            rules_extension=(
                "prepend: []\n"
                "append:\n"
                "  - 'DOMAIN,www.msftconnecttest.com,DIRECT'\n"
                "delete: []\n"
            )
        )
        try:
            report = proxy_rules.inspect(clash.appdata)
        finally:
            clash.close()

        # msftncsi.com is still unrouted; the exact DOMAIN rule covers only the host.
        self.assertEqual(report.missing_hosts, ("msftncsi.com",))

    def test_no_proxy_client_is_reported_not_guessed(self):
        self.assertIsNotNone(proxy_rules, import_error)
        with tempfile.TemporaryDirectory() as empty:
            report = proxy_rules.inspect(Path(empty))

        self.assertFalse(report.found)
        self.assertFalse(report.needs_rule)
        self.assertIn("no supported proxy client", proxy_rules.describe(report))

    def test_a_profile_without_a_rules_extension_is_reported(self):
        self.assertIsNotNone(proxy_rules, import_error)
        clash = FakeClash(
            profiles=(
                "current: SUB001\n"
                "items:\n"
                "- uid: SUB001\n"
                "  type: remote\n"
                "  option: {}\n"
            )
        )
        try:
            report = proxy_rules.inspect(clash.appdata)
        finally:
            clash.close()

        self.assertTrue(report.found)
        self.assertFalse(report.needs_rule)
        self.assertIn("no Rules extension", report.blocked_by)


class ApplyTests(unittest.TestCase):
    def test_adding_prepends_the_rules_and_keeps_everything_else(self):
        self.assertIsNotNone(proxy_rules, import_error)
        clash = FakeClash()
        try:
            report = proxy_rules.inspect(clash.appdata)
            written = proxy_rules.apply(report)

            text = written.read_text(encoding="utf-8")
            self.assertIn("DOMAIN-SUFFIX,msftconnecttest.com,DIRECT", text)
            self.assertIn("DOMAIN-SUFFIX,msftncsi.com,DIRECT", text)
            # The user's own rule must survive.
            self.assertIn("api.example.com", text)

            import yaml

            data = yaml.safe_load(text)
            self.assertEqual(
                data["prepend"][:2],
                [
                    "DOMAIN-SUFFIX,msftconnecttest.com,DIRECT",
                    "DOMAIN-SUFFIX,msftncsi.com,DIRECT",
                ],
            )
            self.assertEqual(data["prepend"][2], "DOMAIN-SUFFIX,api.example.com,DIRECT")

            # A backup of the original must exist next to it.
            self.assertTrue(
                written.with_name(written.name + proxy_rules.BACKUP_SUFFIX).is_file()
            )

            # Configured now, but still not applied until Clash reloads.
            after = proxy_rules.inspect(clash.appdata)
            self.assertEqual(after.missing_hosts, ())
            self.assertTrue(after.needs_reload)

            # Simulate what a profile reload produces.
            (clash.root / proxy_rules.GENERATED_CONFIG_NAME).write_text(
                "rules:\n" + "".join(f"  - '{r}'\n" for r in data["prepend"]),
                encoding="utf-8",
            )
            self.assertTrue(proxy_rules.inspect(clash.appdata).ok)
        finally:
            clash.close()

    def test_applying_twice_is_a_no_op(self):
        self.assertIsNotNone(proxy_rules, import_error)
        clash = FakeClash()
        try:
            proxy_rules.apply(proxy_rules.inspect(clash.appdata))
            first = (clash.root / "profiles" / "RULES01.yaml").read_text(encoding="utf-8")
            proxy_rules.apply(proxy_rules.inspect(clash.appdata))
            second = (clash.root / "profiles" / "RULES01.yaml").read_text(encoding="utf-8")
        finally:
            clash.close()

        self.assertEqual(first, second)

    def test_an_inline_empty_prepend_is_expanded(self):
        self.assertIsNotNone(proxy_rules, import_error)
        clash = FakeClash(rules_extension="prepend: []\nappend: []\ndelete: []\n")
        try:
            written = proxy_rules.apply(proxy_rules.inspect(clash.appdata))
            import yaml

            data = yaml.safe_load(written.read_text(encoding="utf-8"))
        finally:
            clash.close()

        self.assertEqual(data["prepend"], [proxy_rules.direct_rule_for(h) for h in proxy_rules.NCSI_SUFFIXES])

    def test_a_shape_it_does_not_understand_is_refused(self):
        self.assertIsNotNone(proxy_rules, import_error)
        clash = FakeClash(rules_extension="something_else:\n  - a\n")
        try:
            report = proxy_rules.inspect(clash.appdata)
            with self.assertRaises(ValueError):
                proxy_rules.apply(report)
        finally:
            clash.close()

    def test_it_will_not_write_without_a_rules_extension(self):
        self.assertIsNotNone(proxy_rules, import_error)
        with tempfile.TemporaryDirectory() as empty:
            report = proxy_rules.inspect(Path(empty))
            with self.assertRaises(ValueError):
                proxy_rules.apply(report)


if __name__ == "__main__":
    unittest.main()
