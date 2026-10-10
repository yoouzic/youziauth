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

    def test_release_notes_are_prepared_and_version_checked_before_publishing(self):
        # 「这次更新改了什么」优先读 release-notes.md 资产，所以发布流程必须在
        # gh release create 之前备好它，并挡住版本号写错的文件。
        text = ROOT.joinpath(".github", "workflows", "release.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("docs/release-notes/v$env:RELEASE_VERSION.md", text)
        self.assertIn("release\\release-notes.md", text)
        # 版本号检查与发布顺序：先校验，后创建 Release。
        self.assertLess(text.index("Publish the hand-written Chinese notes"),
                        text.index("gh release create"))
        self.assertIn("throw", text.split("Publish the hand-written Chinese notes")[1])
        self.assertIn("release-notes.md", text.split("gh release create")[1])

    @staticmethod
    def _version_key(text):
        parts = [int(part) for part in text.split(".") if part.isdigit()]
        return tuple(parts + [0] * (3 - len(parts)))[:3]

    def _published_tag_versions(self):
        # 已发布 = 有 tag。直接读 .git 里的引用，不走 subprocess：这里既不需要管道，也不想要
        # 子进程句柄 —— 同一个文件里的其它用例已经在用 subprocess，句柄状态会互相干扰
        # （实测整文件运行时报 WinError 6）。
        git_dir = ROOT / ".git"
        if not git_dir.is_dir():
            return None
        names = [path.name for path in git_dir.joinpath("refs", "tags").glob("*")]
        packed = git_dir / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith(("#", "^")) or " refs/tags/" not in line:
                    continue
                names.append(line.rsplit("refs/tags/", 1)[1])
        return {
            self._version_key(name.lstrip("v"))
            for name in names if re.fullmatch(r"v\d+(?:\.\d+)*", name)
        }

    def test_the_notes_reader_is_documented_with_a_writable_template(self):
        readme = ROOT.joinpath("docs", "release-notes", "README.md")
        self.assertTrue(readme.is_file(), "The notes format must be documented where it is written")
        text = readme.read_text(encoding="utf-8")
        self.assertIn("docs/release-notes/v<版本>.md", text)
        self.assertIn("**重点**", text)

    def test_every_published_version_keeps_its_notes_file(self):
        # 发布说明是 Release 的资产，Release 一发出去就加不了资产 —— 所以打 tag 之前
        # docs/release-notes/v<版本>.md 必须先随版本号一起提交（release.yml 就是这么卡的）。
        # 反过来说：只要某个版本上架过，它的说明文件就必须留在仓库里，否则再也补不进那一版。
        # 判据是「有 tag」，不是「比 VERSION 旧」：说明文件正是和版本号同时提交、随后才打
        # tag 的，拿 VERSION 当已发布会让这条断言和发布流程互相矛盾，把每一次版本号提升都
        # 拦死（实测：升到 1.8.7 时撞上）。
        published = self._published_tag_versions()
        if published is None:
            self.skipTest("git cannot list tags locally or from origin")
        notes = {self._version_key(path.stem.lstrip("v"))
                 for path in ROOT.joinpath("docs", "release-notes").glob("v*.md")}
        current = self._version_key(
            ROOT.joinpath("VERSION").read_text(encoding="utf-8").strip()
        )
        # Release 一发出去就加不了资产，所以 release.yml 要求当前版本的说明文件先随版本号一起
        # 提交，缺了就中止发布。这一条等同于把那个门槛前移：光看 tag 会放过「当前版本根本没
        # 写说明」，而那正是最常漏、也最没法事后补的一种。
        self.assertIn(
            current, notes,
            f"v{'.'.join(map(str, current))} has no notes file; release.yml refuses to publish "
            "without docs/release-notes/v<VERSION>.md",
        )
        # 说明文件是 v1.8.6 才引入的：比首个有说明的版本更早的 tag 无法追溯补写，只能豁免。
        first = min(notes)
        for version in sorted(published):
            if version < first:
                continue
            self.assertIn(
                version, notes,
                f"v{'.'.join(map(str, version))} is published but has no notes file; "
                "a published Release cannot gain that asset later",
            )
        # 反过来，说明文件也不该指向一个还没发布的版本：那通常意味着 tag 漏打了，
        # 或者 VERSION 被写成了旧值。
        self.assertFalse(
            [version for version in notes if version > current and version not in published],
            "A notes file describes a version that is neither published nor the current one",
        )

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
