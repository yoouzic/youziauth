from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))

import make_icons  # noqa: E402


class PackagingWorkflowTests(unittest.TestCase):
    def test_build_dependencies_are_exactly_pinned(self):
        lines = [
            line.strip()
            for line in ROOT.joinpath("requirements-build.txt")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip() and not line.startswith("#")
        ]
        # numpy 是验证码识别（captcha_ocr.py）的推理依赖：模型为纯 NumPy 前向，
        # 不带深度学习框架。仍要求精确固定版本，避免构建产物随上游漂移。
        # PyYAML 是 proxy_rules.py 读取 Clash Verge 配置（YAML）的依赖：它在本机
        # 只是别的包的传递依赖，不写在这里 CI 就装不到，测试会以
        # No module named yaml 失败（实测踩过）。
        self.assertEqual(
            lines,
            ["Pillow==12.2.0", "PyInstaller==6.16.0", "numpy==2.4.4", "PyYAML==6.0.3"],
        )
        self.assertTrue(all("==" in line for line in lines))

    def test_pyinstaller_bundle_includes_third_party_license_materials(self):
        spec = ROOT.joinpath("packaging", "youziauth.spec").read_text(
            encoding="utf-8"
        )

        self.assertTrue(ROOT.joinpath("THIRD_PARTY_NOTICES.md").exists())
        self.assertTrue(
            ROOT.joinpath("third_party_licenses", "CPYTHON-3.14-LICENSE.txt").exists()
        )
        self.assertTrue(
            ROOT.joinpath(
                "third_party_licenses", "PYINSTALLER-6.16-COPYING.txt"
            ).exists()
        )
        self.assertIn("THIRD_PARTY_NOTICES.md", spec)
        self.assertIn("third_party_licenses", spec)

    def test_frozen_application_can_read_the_bundled_version(self):
        analysis = []
        scope = {'Analysis': lambda *args, **kwargs: analysis.append(kwargs) or type('Bundle', (), {'pure': [], 'scripts': [], 'binaries': [], 'datas': []})(),
                 'MERGE': lambda *args: None, 'PYZ': lambda *args: None,
                 'EXE': lambda *args, **kwargs: None, 'COLLECT': lambda *args, **kwargs: None}
        exec(compile(ROOT.joinpath('packaging', 'youziauth.spec').read_text(encoding='utf-8'), 'youziauth.spec', 'exec'), scope)
        bundled = {Path(source).name: destination for source, destination in analysis[0]['datas']}
        self.assertEqual(bundled.get('VERSION'), '.')

    def test_pyinstaller_spec_builds_gui_and_system_agent_without_user_secrets(self):
        spec = ROOT.joinpath("packaging", "youziauth.spec").read_text(
            encoding="utf-8"
        )

        self.assertIn("campus_auth_gui.py", spec)
        self.assertIn("campus_auth_agent.py", spec)
        self.assertIn('name="youziauth"', spec)
        self.assertIn('name="youziauth-agent"', spec)
        self.assertIn("config.example.ini", spec)
        self.assertIn("yuzu_app.ico", spec)
        self.assertNotIn("config.ini", spec)
        self.assertNotIn("campus_auth_password.txt", spec)
        self.assertNotIn("campus_auth.log", spec)

    def test_icon_generation_uses_generated_yuzu_source(self):
        script = ROOT.joinpath("packaging", "make_icons.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("yuzu_app_source.png", script)
        self.assertIn("make_yuzu_icon_images", script)
        self.assertTrue(ROOT.joinpath("assets", "yuzu_app_source.png").exists())

    def test_generated_yuzu_icon_has_transparent_corners(self):
        from PIL import Image

        icon = Image.open(ROOT / "assets" / "yuzu_app.png").convert("RGBA")

        self.assertEqual(icon.size, (256, 256))
        self.assertEqual(icon.getpixel((0, 0))[3], 0)
        self.assertGreater(icon.getpixel((128, 128))[3], 200)

    def test_checkerboard_source_is_matted_to_alpha(self):
        from PIL import Image

        image = Image.new("RGB", (4, 4), (255, 255, 255))
        image.putpixel((0, 0), (250, 250, 250))
        image.putpixel((1, 0), (236, 236, 236))
        image.putpixel((2, 2), (245, 210, 40))

        matted = make_icons.remove_light_checkerboard_background(image)

        self.assertEqual(matted.getpixel((0, 0))[3], 0)
        self.assertEqual(matted.getpixel((1, 0))[3], 0)
        self.assertEqual(matted.getpixel((2, 2))[3], 255)

    def test_msi_build_script_runs_pyinstaller_then_wix(self):
        script = ROOT.joinpath("build_msi.ps1").read_text(encoding="utf-8")

        self.assertIn("PyInstaller", script)
        self.assertIn("wix.exe", script)
        self.assertIn("youziauth.msi", script)
        self.assertIn("youziauth.spec", script)
        self.assertIn("youziauth.wxs", script)
        self.assertIn("youziauth.exe", script)
        self.assertIn("youziauth-agent.exe", script)
        self.assertIn("InstallDependencies", script)
        self.assertIn("--acceptEula wix7", script)
        self.assertIn("$LASTEXITCODE", script)

    def test_wix_source_has_start_menu_shortcut_and_uninstall_metadata(self):
        source = ROOT.joinpath("packaging", "youziauth.wxs").read_text(
            encoding="utf-8"
        )

        self.assertIn("youziauth", source)
        self.assertIn('Manufacturer="yoouzic"', source)
        self.assertIn("ProgramMenuFolder", source)
        self.assertIn("Shortcut", source)
        self.assertIn("youziauth.exe", source)
        self.assertIn('Version="$(var.ProductVersion)"', source)
        self.assertIn("System.AppUserModel.ID", source)
        self.assertIn('Value="youziauth"', source)
        self.assertNotIn("Campus Network Auth", source)
        self.assertNotIn("CampusNetworkAuth", source)

    def test_wix_source_installs_desktop_shortcut(self):
        source = ROOT.joinpath("packaging", "youziauth.wxs").read_text(
            encoding="utf-8"
        )

        self.assertIn("DesktopFolder", source)
        self.assertIn("ApplicationDesktopShortcut", source)

    def test_wix_source_registers_notification_protocol(self):
        source = ROOT.joinpath("packaging", "youziauth.wxs").read_text(
            encoding="utf-8"
        )

        self.assertIn(r"Software\Classes\youziauth", source)
        self.assertIn("URL Protocol", source)
        self.assertIn("--notification-action", source)
        self.assertIn("ProtocolRegistrationComponent", source)


class PayloadCompletenessTests(unittest.TestCase):
    """1.4.0 shipped an MSI whose running processes made Windows Installer defer 41
    file copies (including _internal\\base_library.zip) to the next reboot, so the
    installed app died with "Failed to import encodings module"."""

    def test_wix_source_stops_running_processes_before_install_validate(self):
        source = ROOT.joinpath("packaging", "youziauth.wxs").read_text(
            encoding="utf-8"
        )

        self.assertIn("StopYouziauthProcesses", source)
        self.assertIn('Before="InstallValidate"', source)
        self.assertIn("taskkill", source)
        self.assertIn("youziauth-agent.exe", source)
        self.assertIn("youziauth.exe", source)
        self.assertIn('Condition="NOT REMOVE"', source)

    def test_build_script_verifies_the_wix_manifest_and_can_verify_the_payload(self):
        script = ROOT.joinpath("build_msi.ps1").read_text(encoding="utf-8")

        self.assertIn("Assert-WixManifestIsComplete", script)
        self.assertIn("verify_msi_payload.ps1", script)
        self.assertIn("[switch]$VerifyPayload", script)
        self.assertIn("ApplicationFiles.wxs", script)

    def test_payload_verifier_compares_every_bundle_file(self):
        verifier = ROOT.joinpath(
            "packaging", "verify_msi_payload.ps1"
        ).read_text(encoding="utf-8")

        self.assertIn("'/a'", verifier)
        self.assertIn("base_library", verifier)
        self.assertIn("PFiles", verifier)
        self.assertIn("Get-ChildItem -LiteralPath $AppDir -Recurse -File", verifier)

    def test_release_workflow_verifies_the_payload_before_signing(self):
        text = ROOT.joinpath(".github", "workflows", "release.yml").read_text(
            encoding="utf-8"
        )
        build = text.index("Build MSI")
        sign = text.index("Sign MSI with the release key")
        verify = text.index("Verify signed release")
        publish = text.index("Publish signed release")
        self.assertIn("-VerifyPayload", text)
        self.assertLess(build, sign)
        # The signature must be verified before anything is published.
        self.assertLess(sign, verify)
        self.assertLess(verify, publish)

    def test_pyinstaller_bundle_keeps_the_interpreter_standard_library(self):
        # The frozen interpreter cannot start without base_library.zip; it must stay
        # inside the PyInstaller build tree that feeds the WiX manifest.
        spec = ROOT.joinpath("packaging", "youziauth.spec").read_text(
            encoding="utf-8"
        )

        self.assertIn("COLLECT", spec)
        self.assertIn('name="youziauth"', spec)
        manifest = ROOT.joinpath("build", "wix", "ApplicationFiles.wxs")
        if manifest.exists():
            text = manifest.read_text(encoding="utf-8")
            self.assertIn("base_library.zip", text)


class ReleaseMetadataTests(unittest.TestCase):
    def test_version_file_is_strict_semver(self):
        version = ROOT.joinpath("VERSION").read_text(encoding="utf-8").strip()
        self.assertRegex(version, r"^[0-9]+\.[0-9]+\.[0-9]+$")

    def test_version_is_ahead_of_the_last_shipped_msi(self):
        # Windows refuses to install at or below an installed version, so a VERSION
        # that lags a shipped build produces an uninstallable MSI. 1.4.0 shipped with a
        # payload defect, 1.4.1 carried the payload fix and 1.4.2 the location-source
        # selector, so this build must be strictly newer than all of them.
        version = ROOT.joinpath("VERSION").read_text(encoding="utf-8").strip()
        shipped = tuple(int(part) for part in version.split("."))
        self.assertGreater(shipped, (1, 4, 2))

    def test_wix_version_comes_from_build_variable(self):
        source = ROOT.joinpath("packaging", "youziauth.wxs").read_text(
            encoding="utf-8"
        )
        self.assertIn('Version="$(var.ProductVersion)"', source)
        self.assertNotIn('Version="1.1.3"', source)

    def test_pyinstaller_uses_generated_version_resources_without_upx(self):
        source = ROOT.joinpath("packaging", "youziauth.spec").read_text(
            encoding="utf-8"
        )
        self.assertIn('version=str(VERSION_DIR / "youziauth.version")', source)
        self.assertIn(
            'version=str(VERSION_DIR / "youziauth-agent.version")', source
        )
        self.assertNotIn("upx=True", source)

    def test_version_generator_writes_distinct_descriptions(self):
        version = ROOT.joinpath("VERSION").read_text(encoding="utf-8").strip()
        numeric = ", ".join(part for part in version.split(".")) + ", 0"
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "packaging" / "generate_version_info.py"),
                    "--output",
                    str(output),
                ],
                check=True,
            )
            gui = (output / "youziauth.version").read_text(encoding="utf-8")
            agent = (output / "youziauth-agent.version").read_text(
                encoding="utf-8"
            )
            self.assertIn("youziauth campus network tray and settings", gui)
            self.assertIn(
                "youziauth SYSTEM campus network authentication agent", agent
            )
            self.assertIn(numeric, gui)
            self.assertIn(version, agent)


if __name__ == "__main__":
    unittest.main()
