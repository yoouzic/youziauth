import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WorkflowPolicyTests(unittest.TestCase):
    def test_actions_are_pinned_to_full_commit_shas(self):
        for path in ROOT.joinpath(".github", "workflows").glob("*.yml"):
            text = path.read_text(encoding="utf-8")
            for reference in re.findall(r"uses:\s*([^\s]+)", text):
                self.assertRegex(
                    reference,
                    r"^[^@]+@[0-9a-f]{40}$",
                    msg=f"{path}: {reference}",
                )

    def test_ci_uses_github_hosted_windows_and_runs_full_suite(self):
        text = ROOT.joinpath(".github", "workflows", "ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("runs-on: windows-latest", text)
        self.assertIn("python -m unittest discover -s tests -v", text)
        self.assertIn("build_msi.ps1", text)
        self.assertIn("permissions:\n  contents: read", text)

    def test_python_cache_uses_the_pinned_build_requirements(self):
        for name in ("ci.yml", "release.yml"):
            text = ROOT.joinpath(".github", "workflows", name).read_text(
                encoding="utf-8"
            )
            self.assertIn(
                "cache-dependency-path: requirements-build.txt",
                text,
                msg=name,
            )

    def test_both_workflows_run_the_front_end_suite(self):
        # `unittest discover` only matches test*.py, so tests/test_desktop_ui.cjs is invisible to
        # the Python step: it drives desktop_ui/app.js against a DOM stub built from index.html.
        # Leaving it unwired is how the 1.8.2 account-label regression reached a published release.
        for name in ("ci.yml", "release.yml"):
            text = ROOT.joinpath(".github", "workflows", name).read_text(encoding="utf-8")
            self.assertIn("node --test tests/test_desktop_ui.cjs", text, msg=name)

    def test_signpath_configuration_is_gone(self):
        # SignPath was declined; nothing may still depend on it at release time.
        self.assertFalse(ROOT.joinpath(".signpath").exists())
        for name in ("ci.yml", "release.yml"):
            text = ROOT.joinpath(".github", "workflows", name).read_text(encoding="utf-8")
            self.assertNotIn("signpath", text.lower(), msg=name)

    def test_release_signs_with_the_pinned_key_and_never_publishes_unsigned_msi(self):
        text = ROOT.joinpath(".github", "workflows", "release.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("tools\\sign_release.py", text)
        self.assertIn("secrets.YOUZIAUTH_RELEASE_KEY", text)
        self.assertIn("packaging/verify_release.ps1", text)
        self.assertIn("gh release create", text)
        self.assertNotIn("dist/youziauth.msi ${{", text)
        # The private key must never be written into the workspace or an artifact.
        self.assertNotIn("YOUZIAUTH_RELEASE_KEY >", text)
        self.assertNotIn("Out-File", text.split("Check release key configuration")[1].split("setup-python")[0])
        self.assertNotIn("signpath", text.lower())

    def test_signing_key_is_never_committed(self):
        ignore = ROOT.joinpath(".gitignore").read_text(encoding="utf-8")
        self.assertIn(".release-key/", ignore)
        self.assertIn("*.key", ignore)
        # Only the public half may live in the source tree.
        source = ROOT.joinpath("windows_update.py").read_text(encoding="utf-8")
        self.assertIn("PUBLIC_KEY_B64", source)
        self.assertNotIn("SECRET_KEY", source)

    def test_private_key_material_is_never_tracked_by_git(self):
        # Ask git itself: whatever it would commit must not include key material.
        try:
            listed = subprocess.run(
                ["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True
            ).stdout.splitlines()
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("git is unavailable")
        for name in listed:
            with self.subTest(tracked=name):
                self.assertNotRegex(name.lower(), r"\.(key|pfx|p12|pem)$")
                self.assertNotIn("release-key", name.lower())
        # The private key must also be ignored if it exists in the checkout.
        if (ROOT / ".release-key").exists():
            ignored = subprocess.run(
                ["git", "check-ignore", "-q", ".release-key/ed25519-release.key"],
                cwd=ROOT, capture_output=True,
            )
            self.assertEqual(ignored.returncode, 0, "the release key must be gitignored")

    def test_release_audit_requires_a_valid_signature_versions_and_hashes(self):
        text = ROOT.joinpath("packaging", "verify_release.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("--verify-signature-file", text)
        self.assertIn("sign_release.py", text)
        self.assertIn("msiexec.exe", text)
        self.assertIn("FileVersion", text)
        self.assertIn("SHA256SUMS.txt", text)
        # The gate must fail closed: no Authenticode fallback, and a mismatch throws.
        self.assertNotIn("Get-AuthenticodeSignature", text)
        self.assertIn("throw", text)


if __name__ == "__main__":
    unittest.main()
