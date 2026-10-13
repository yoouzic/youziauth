"""Component identity in the generated WiX manifest.

2026-10-13: a case-only rename of ``_internal/python314.dll`` between 1.8.33 and
1.8.34 changed the derived component GUID, so the upgrade saw two components for
one file. The new component skipped its copy ("existing file is of an equal
version") and the old product's removal then deleted the file it owned -- taking
the Python runtime out of the installed tree mid-transaction. Every later custom
action failed with error -1 ("Could not load Python DLL") and the MSI rolled back
after killing its processes. Restoring the earlier runtime GUID in 1.8.35 did
not prevent a late-removal migration from the broken 1.8.34 GUID deleting it
again. These tests cover identity compatibility, not native upgrade behavior.
"""

from __future__ import annotations

import tempfile
import unittest
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))

import generate_wix_files  # noqa: E402


WIX_NS = {"w": generate_wix_files.WIX_NS}
# The GUID the 1.8.33 package used for its (lower-case) python314.dll component,
# read out of C:\Windows\Installer\28340be.msi during the incident. Upgrades from
# that package only work while this stays the same.
LEGACY_PYTHON_COMPONENT = "4437BEA9-58C8-5190-8193-9AD5F8E0F56F"


class ComponentGuidTests(unittest.TestCase):
    def test_the_license_identities_already_installed_by_1835_remain_unchanged(self):
        published = {
            '_internal/cryptography-50.0.0.dist-info/licenses/LICENSE.APACHE':
                '95C3C213-EFEA-5B94-813E-D7E8AA4B9DD6',
            '_internal/cryptography-50.0.0.dist-info/licenses/LICENSE.BSD':
                'B59BAD58-B870-5E33-AC3C-C9FB69D35AC0',
            '_internal/greenlet-3.2.4.dist-info/licenses/LICENSE.PSF':
                '25E832A9-230B-5ADA-B718-1D22B4085FFC',
        }
        for path, expected in published.items():
            with self.subTest(path=path):
                self.assertEqual(generate_wix_files.component_guid(Path(path)), expected)

    def test_case_only_differences_share_one_component(self):
        lower = generate_wix_files.component_guid(Path("_internal/python314.dll"))
        upper = generate_wix_files.component_guid(Path("_internal/python314.DLL"))

        self.assertEqual(lower, upper)
        # The exception that bit us is an extension case flip; that is what identity
        # normalizes. The base name is deliberately left alone (see the test below).
        self.assertEqual(
            generate_wix_files.component_guid(Path("_internal/LICENCE.TXT")),
            generate_wix_files.component_guid(Path("_internal/LICENCE.txt")),
        )

    def test_the_previous_lower_case_identity_is_unchanged(self):
        # This preserves the lower-case identity used by 1.8.33 and restored by
        # 1.8.35. 1.8.34 used a different identity, so this alone cannot prove its
        # native upgrade migration is safe.
        self.assertEqual(
            generate_wix_files.component_guid(Path("_internal/python314.dll")),
            LEGACY_PYTHON_COMPONENT,
        )

    def test_only_the_extension_is_normalized(self):
        # Existing mixed-case base names (timezone data, .NET assemblies) retain
        # their earlier identities. This limited compatibility normalization is
        # deliberately not a complete Windows path case-folding algorithm.
        self.assertEqual(
            generate_wix_files.component_guid(Path("_internal/zoneinfo/New_York")),
            str(uuid.uuid5(
                generate_wix_files.PRODUCT_NAMESPACE,
                "component:_internal/zoneinfo/New_York",
            )).upper(),
        )
        self.assertEqual(
            generate_wix_files.component_guid(Path("_internal/ClrLoader.dll")),
            str(uuid.uuid5(
                generate_wix_files.PRODUCT_NAMESPACE,
                "component:_internal/ClrLoader.dll",
            )).upper(),
        )

    def test_a_name_without_an_extension_is_used_as_is(self):
        self.assertEqual(
            generate_wix_files.msi_file_name(Path("_internal/tzdata/README")),
            "_internal/tzdata/README",
        )
        self.assertEqual(
            generate_wix_files.msi_file_name(Path("_internal/python314.DLL")),
            "_internal/python314.dll",
        )
        self.assertEqual(
            generate_wix_files.msi_file_name(Path("youziauth.exe")),
            "youziauth.exe",
        )

    def test_different_files_still_get_different_components(self):
        self.assertNotEqual(
            generate_wix_files.component_guid(Path("_internal/python314.dll")),
            generate_wix_files.component_guid(Path("_internal/python3.dll")),
        )
        self.assertNotEqual(
            generate_wix_files.component_guid(Path("youziauth.exe")),
            generate_wix_files.component_guid(Path("_internal/youziauth.exe")),
        )


class GeneratedManifestTests(unittest.TestCase):
    def build_manifest(self, root: Path) -> ET.Element:
        (root / "_internal").mkdir(parents=True)
        (root / "youziauth.exe").write_bytes(b"stub")
        # Upper-case extension: exactly what PyInstaller produced from its CArchive.
        (root / "_internal" / "python314.DLL").write_bytes(b"stub")
        (root / "_internal" / "python3.dll").write_bytes(b"stub")
        output = root.parent / "ApplicationFiles.wxs"
        generate_wix_files.generate(root, output)
        return ET.parse(output).getroot()

    def test_the_python_runtime_keeps_the_legacy_component_guid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "youziauth"
            xml = self.build_manifest(root)

        components = {
            Path(element.find("w:File", WIX_NS).get("Source")).name.lower(): element.get("Guid").strip("{}")
            for element in xml.findall(".//w:Component", WIX_NS)
        }
        self.assertEqual(
            components["python314.dll"],
            LEGACY_PYTHON_COMPONENT,
            "a case-only rename must not mint a new component for the same file",
        )
        # And it must not collide with its differently-named neighbour.
        self.assertNotEqual(components["python314.dll"], components["python3.dll"])

    def test_source_spelling_is_preserved_while_identity_uses_the_1835_rule(self):
        # WiX's long file name preserves its source spelling. The normalization
        # below applies only to GUID compatibility, not the installed long name.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "youziauth"
            xml = self.build_manifest(root)

        names = sorted(
            element.find("w:File", WIX_NS).get("Source").split("\\")[-1]
            for element in xml.findall(".//w:Component", WIX_NS)
        )
        self.assertEqual(
            names,
            ["python3.dll", "python314.DLL", "youziauth.exe"],
            "the manifest must keep pointing at the real payload files",
        )
        self.assertEqual(
            generate_wix_files.msi_file_name(Path("_internal/python314.DLL")),
            "_internal/python314.dll",
        )

    def test_every_payload_file_is_referenced_exactly_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / "youziauth"
            xml = self.build_manifest(root)

        refs = [element.get("Id") for element in xml.findall(".//w:ComponentRef", WIX_NS)]
        self.assertEqual(len(refs), 3)
        self.assertEqual(len(set(refs)), 3)


if __name__ == "__main__":
    unittest.main()
