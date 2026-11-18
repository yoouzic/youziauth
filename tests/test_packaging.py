from pathlib import Path
import ctypes
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET


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
        # pycryptodome 是乐跑模块（lptiyu.cipher）协议信封加密的依赖：缺失时
        # 整个乐跑功能降级为不可用，同样必须钉住（2026-11-11 实测 4.4.0）。
        self.assertEqual(
            lines,
            ["Pillow==12.2.0", "PyInstaller==6.16.0", "numpy==2.4.4", "PyYAML==6.0.3",
             "pycryptodome==4.4.0", "cryptography==50.0.2"],
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

    def test_msi_packages_the_verified_tray_launcher(self):
        script = ROOT.joinpath("build_msi.ps1").read_text(encoding="utf-8")
        self.assertIn("TrayLauncher.cs", script)
        self.assertIn("youziauth-tray-launcher.exe", script)
        self.assertIn("--verify-runtime", script)
        self.assertLess(script.index("--verify-runtime"), script.index("generate_wix_files.py"))

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


@unittest.skipUnless(os.name == "nt", "Windows launcher process integration")
class TrayLauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework/v4.0.30319/csc.exe"
        if not compiler.is_file():
            raise unittest.SkipTest(".NET Framework C# compiler is unavailable")
        temp = tempfile.TemporaryDirectory(prefix="youziauth-launcher-test-")
        cls.addClassCleanup(temp.cleanup)
        cls.bundle = Path(temp.name)
        runtime = cls.bundle / "_internal"
        runtime.mkdir()
        library_bytes = b"base-library fixture"
        (runtime / "base_library.zip").write_bytes(library_bytes)
        (runtime / "python314.dll").write_bytes(b"runtime fixture")
        harness = cls.bundle / "Harness.cs"
        harness.write_text(
            f"""
using System;
internal static class TrayRuntime {{
    internal const string PythonDll = "python314.dll";
    internal const string BaseLibraryHash = "{hashlib.sha256(library_bytes).hexdigest()}";
}}
internal static class Harness {{
    public static int Main() {{
        return TrayLauncher.Run(AppDomain.CurrentDomain.BaseDirectory, TimeSpan.FromSeconds(3));
    }}
}}
""",
            encoding="utf-8",
        )
        fixture = cls.bundle / "FakeTray.cs"
        fixture.write_text(
            r"""
using System;
using System.Diagnostics;
using System.IO;
using System.Threading;
internal static class FakeTray {
    public static int Main(string[] args) {
        if (args.Length == 1 && args[0] == "--unrelated") {
            Thread.Sleep(15000);
            return 0;
        }
        if (args.Length != 3 || args[0] != "--tray-startup"
            || args[1] != "--startup-ready-event"
            || !args[2].StartsWith(@"Local\youziauth-tray-ready-")) return 2;
        File.WriteAllText("fixture.pid", Process.GetCurrentProcess().Id.ToString());
        string mode = File.ReadAllText("fixture-mode.txt");
        if (mode == "early") return 0;
        if (mode == "ready") {
            using (var ready = EventWaitHandle.OpenExisting(args[2])) ready.Set();
            Thread.Sleep(250);
            File.WriteAllText("fixture-finished", "true");
            return 7;
        }
        Thread.Sleep(15000);
        return 2;
    }
}
""",
            encoding="utf-8",
        )
        for name, entry, sources in (
            ("launcher.exe", "Harness", (ROOT / "packaging" / "TrayLauncher.cs", harness)),
            ("youziauth.exe", "FakeTray", (fixture,)),
        ):
            result = subprocess.run(
                [str(compiler), "/nologo", "/target:winexe", f"/main:{entry}",
                 f"/out:{cls.bundle / name}", *map(str, sources)],
                capture_output=True, text=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode:
                raise AssertionError(result.stdout + result.stderr)

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="youziauth launcher 漢字 [test] ")
        self.addCleanup(temp.cleanup)
        self.install_dir = Path(temp.name)
        shutil.copytree(self.bundle / "_internal", self.install_dir / "_internal")
        for name in ("launcher.exe", "youziauth.exe"):
            shutil.copy2(self.bundle / name, self.install_dir / name)
        self.environment = {**os.environ, "LOCALAPPDATA": str(self.install_dir)}
        self.failure_log = self.install_dir / "youziauth" / "tray-startup-error.log"

    def _start(self, name, *args):
        process = subprocess.Popen(
            [str(self.install_dir / name), *args],
            cwd=self.install_dir, env=self.environment, creationflags=subprocess.CREATE_NO_WINDOW,
        )

        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)

        self.addCleanup(cleanup)
        return process

    def _launch_tray(self, mode):
        (self.install_dir / "fixture-mode.txt").write_text(mode, encoding="ascii")
        return self._start("launcher.exe")

    def test_ready_tray_runs_to_its_own_exit_and_forwards_that_status(self):
        self.assertEqual(self._launch_tray("ready").wait(timeout=8), 7)
        self.assertTrue((self.install_dir / "fixture-finished").is_file())

    def test_zero_exit_without_readiness_fails_and_is_logged(self):
        self.assertEqual(self._launch_tray("early").wait(timeout=8), 1)
        self.assertIn("exited before readiness", self.failure_log.read_text(encoding="utf-8"))

    def test_hung_tray_times_out_and_only_its_child_is_stopped(self):
        unrelated = self._start("youziauth.exe", "--unrelated")
        launcher = self._launch_tray("hang")
        pid_file = self.install_dir / "fixture.pid"
        deadline = time.monotonic() + 2.5
        pid = ""
        while not pid and time.monotonic() < deadline:
            if pid_file.is_file():
                pid = pid_file.read_text(encoding="ascii")
            if not pid:
                time.sleep(0.025)
        self.assertTrue(pid)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x00100000, False, int(pid))
        self.assertTrue(handle)
        try:
            self.assertEqual(launcher.wait(timeout=8), 1)
            self.assertEqual(kernel32.WaitForSingleObject(handle, 0), 0)
            self.assertIsNone(unrelated.poll())
            self.assertIn("readiness timed out", self.failure_log.read_text(encoding="utf-8"))
        finally:
            kernel32.CloseHandle(handle)

    def test_wrong_stdlib_is_rejected_without_starting_the_tray(self):
        (self.install_dir / "_internal" / "base_library.zip").write_bytes(b"incomplete runtime")
        self.assertEqual(self._launch_tray("ready").wait(timeout=8), 1)
        self.assertFalse((self.install_dir / "fixture.pid").exists())
        self.assertIn("base_library.zip", self.failure_log.read_text(encoding="utf-8"))


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
        root = ET.fromstring(source)
        ns = {"wix": "http://wixtoolset.org/schemas/v4/wxs"}
        for action_id, passes in (
            ("StopYouziauthProcesses", 2),
            ("StopYouziauthProcessesElevated", 1),
        ):
            with self.subTest(action=action_id):
                action = root.find(f".//wix:CustomAction[@Id='{action_id}']", ns)
                self.assertIsNotNone(action)
                tray_commands = [
                    command.removeprefix("cmd.exe /c ") for command in action.get("ExeCommand").split(" & ")
                    if "youziauth-tray-launcher.exe" in command or "youziauth.exe" in command
                    or r"\youziauth\Tray" in command
                ]
                self.assertEqual(
                    tray_commands,
                    [r'schtasks /End /TN "\youziauth\Tray" >nul 2>nul',
                     "taskkill /f /im youziauth-tray-launcher.exe >nul 2>nul",
                     "taskkill /f /t /im youziauth.exe >nul 2>nul"] * passes,
                )

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
