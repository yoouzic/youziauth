"""Verify native MSI UI wiring without ever launching or installing a package."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
UI_SOURCE = ROOT / "packaging" / "installer-ui.wxs"
NS = {"w": "http://wixtoolset.org/schemas/v4/wxs"}


class InstallerUiTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(UI_SOURCE.is_file(), "manual MSI feedback UI is missing")
        self.ui = ET.parse(UI_SOURCE).find("w:Fragment/w:UI", NS)
        self.assertIsNotNone(self.ui)

    def test_manual_install_shows_modeless_progress_before_execute(self):
        dialog = self.ui.find("w:Dialog[@Id='YouziauthProgressDlg']", NS)
        self.assertIsNotNone(dialog)
        self.assertEqual(dialog.get("Modeless"), "yes")
        progress = dialog.find("w:Control[@Type='ProgressBar']/w:Subscribe", NS)
        self.assertIsNotNone(progress)
        self.assertEqual(progress.get("Event"), "SetProgress")
        self.assertEqual(progress.get("Attribute"), "Progress")
        action = dialog.find("w:Control[@Id='ActionText']/w:Subscribe", NS)
        self.assertEqual(action.get("Event"), "ActionText")
        show = self.ui.find(
            "w:InstallUISequence/w:Show[@Dialog='YouziauthProgressDlg']", NS
        )
        self.assertEqual(show.get("Before"), "ExecuteAction")

    def test_success_error_and_cancel_have_distinct_terminal_pages(self):
        shows = {
            show.get("OnExit"): show.get("Dialog")
            for show in self.ui.findall("w:InstallUISequence/w:Show", NS)
            if show.get("OnExit")
        }
        self.assertEqual(set(shows), {"success", "error", "cancel"})
        self.assertEqual(len(set(shows.values())), 3)
        for outcome, dialog_id in shows.items():
            dialog = self.ui.find(f"w:Dialog[@Id='{dialog_id}']", NS)
            self.assertIsNotNone(dialog, outcome)
            finish = dialog.find("w:Control[@Id='Finish']/w:Publish", NS)
            self.assertEqual(finish.get("Event"), "EndDialog")
            self.assertEqual(finish.get("Value"), "Return")
            self.assertTrue(dialog.find("w:Control[@Id='Description']", NS).get("Text"))

    def test_cancel_requires_confirmation_before_aborting(self):
        cancel = self.ui.find(
            "w:Dialog[@Id='YouziauthProgressDlg']/w:Control[@Id='Cancel']/w:Publish",
            NS,
        )
        self.assertEqual(cancel.get("Event"), "SpawnDialog")
        confirmation = self.ui.find(
            f"w:Dialog[@Id='{cancel.get('Value')}']", NS
        )
        choices = {
            publish.get("Value")
            for publish in confirmation.findall("w:Control/w:Publish", NS)
            if publish.get("Event") == "EndDialog"
        }
        self.assertEqual(choices, {"Exit", "Return"})

    def test_silent_install_cannot_be_forced_to_show_ui(self):
        # Native InstallUISequence is skipped by /qn. There must be no executable
        # UI launcher or execute-sequence dialog that bypasses that engine behavior.
        tree = ET.parse(UI_SOURCE)
        self.assertFalse(tree.findall(".//w:CustomAction", NS))
        self.assertFalse(tree.findall(".//w:InstallExecuteSequence", NS))
        self.assertFalse(tree.findall(".//w:Property[@Id='UILevel']", NS))
        shows = self.ui.findall("w:InstallUISequence/w:Show", NS)
        self.assertTrue(shows)
        for show in shows:
            self.assertEqual(show.get("Condition"), "UILevel >= 4")

    @unittest.skipUnless(os.name == "nt", "MSI tables require Windows Installer")
    def test_wix_compiles_feedback_into_native_msi_tables(self):
        wix = ROOT / ".tools" / "wix.exe"
        if not wix.is_file():
            found = shutil.which("wix.exe")
            if not found:
                self.skipTest("WiX is not installed")
            wix = Path(found)
        with tempfile.TemporaryDirectory(prefix="youziauth-ui-test-") as temporary:
            directory = Path(temporary)
            payload = directory / "payload.txt"
            payload.write_text("harmless test payload", encoding="utf-8")
            fixture = directory / "fixture.wxs"
            fixture.write_text(
                '''<Wix xmlns="http://wixtoolset.org/schemas/v4/wxs">
  <Package Name="youziauth UI test" Manufacturer="test" Version="0.0.1"
           UpgradeCode="{CFD66AEC-9CB0-4158-93C1-7A08AB4DB7D4}"
           Language="2052" Codepage="936" Scope="perUser">
    <MediaTemplate EmbedCab="yes" />
    <UIRef Id="YouziauthInstallerUI" />
    <StandardDirectory Id="LocalAppDataFolder">
      <Directory Id="INSTALLFOLDER" Name="youziauth-ui-test">
        <Component Id="PayloadComponent" Guid="*">
          <File Id="Payload" Source="$(var.PayloadPath)" />
        </Component>
      </Directory>
    </StandardDirectory>
    <Feature Id="Main" Level="1"><ComponentRef Id="PayloadComponent" /></Feature>
  </Package>
</Wix>''',
                encoding="utf-8",
            )
            msi = directory / "fixture.msi"
            build = subprocess.run(
                [str(wix), "--acceptEula", "wix7", "build", str(fixture),
                 str(UI_SOURCE), "-d", f"PayloadPath={payload}", "-out", str(msi)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=60,
            )
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            self.assertTrue(msi.is_file())
            tables = self._read_msi_tables(msi)

        sequence = {row[0]: (row[1], int(row[2])) for row in tables["sequence"]}
        progress = sequence["YouziauthProgressDlg"]
        self.assertGreater(progress[1], sequence["CostFinalize"][1])
        self.assertLess(progress[1], sequence["ExecuteAction"][1])
        expected = {
            "YouziauthSuccessDlg": -1,
            "YouziauthFailureDlg": -3,
            "YouziauthCancelledDlg": -2,
        }
        for dialog_id, termination in expected.items():
            self.assertEqual(sequence[dialog_id], ("UILevel >= 4", termination))
        self.assertIn(
            ["YouziauthProgressDlg", "Progress", "SetProgress", "Progress"],
            tables["mapping"],
        )
        self.assertIn(
            ["YouziauthProgressDlg", "ActionText", "ActionText", "Text"],
            tables["mapping"],
        )
        for dialog_id in expected:
            self.assertIn([dialog_id, "Finish", "EndDialog", "Return"], tables["events"])
        self.assertIn(["ErrorDialog", "YouziauthErrorDlg"], tables["properties"])

    @staticmethod
    def _read_msi_tables(msi):
        script = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$installer = New-Object -ComObject WindowsInstaller.Installer
$database = $installer.OpenDatabase($env:YOUZIAUTH_UI_TEST_MSI, 0)
function Read-Rows([string]$query, [int]$columns) {
    $view = $database.OpenView($query)
    $null = $view.Execute()
    $rows = [System.Collections.Generic.List[object]]::new()
    while ($null -ne ($record = $view.Fetch())) {
        $row = @()
        for ($index = 1; $index -le $columns; $index++) {
            $row += $record.StringData($index)
        }
        $rows.Add($row)
    }
    $null = $view.Close()
    return ,$rows.ToArray()
}
$result = @{
    sequence = Read-Rows 'SELECT `Action`, `Condition`, `Sequence` FROM `InstallUISequence`' 3
    mapping = Read-Rows 'SELECT `Dialog_`, `Control_`, `Event`, `Attribute` FROM `EventMapping`' 4
    events = Read-Rows 'SELECT `Dialog_`, `Control_`, `Event`, `Argument` FROM `ControlEvent`' 4
    properties = Read-Rows 'SELECT `Property`, `Value` FROM `Property`' 2
}
$result | ConvertTo-Json -Depth 5 -Compress
'''
        environment = os.environ.copy()
        environment["YOUZIAUTH_UI_TEST_MSI"] = str(msi)
        read = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True, encoding="utf-8", errors="replace", env=environment,
            timeout=30, check=True,
        )
        return json.loads(read.stdout)


if __name__ == "__main__":
    unittest.main()
