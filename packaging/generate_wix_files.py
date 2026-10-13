from __future__ import annotations

import argparse
import html
import uuid
from pathlib import Path


WIX_NS = "http://wixtoolset.org/schemas/v4/wxs"
PRODUCT_NAMESPACE = uuid.UUID("79df1cda-236e-43a1-9981-a437dd93bce9")


def make_id(prefix: str, relative_path: Path) -> str:
    digest = uuid.uuid5(PRODUCT_NAMESPACE, relative_path.as_posix()).hex
    return f"{prefix}_{digest}"


def esc(value: Path | str) -> str:
    return html.escape(str(value), quote=True)


def msi_file_name(relative_path: Path) -> str:
    """The extension-normalized identity introduced by the 1.8.35 package.

    Windows treats case-only file names as the same destination. WiX preserves
    the long file name's case: the 1.8.35 File table contains
    ``0oz6cnc3.dll|python314.DLL``. The short name is not the identity rule.

    Keep this normalization to preserve the GUIDs already installed by 1.8.35;
    it matches the earlier lower-case Python runtime identity without changing
    the many existing mixed-case base names. Sources and long names retain their
    original spelling. This cannot repair an upgrade from a different GUID when
    the old product removes its file after the new product has installed it.
    """
    path = relative_path.as_posix()
    parent, _, name = path.rpartition("/")
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        return path
    normalized = f"{stem}.{suffix.lower()}"
    return f"{parent}/{normalized}" if parent else normalized


def component_guid(relative_path: Path) -> str:
    """Stable component GUID for one payload file.

    Preserve the extension-normalized GUIDs installed by 1.8.35.

    2026-10-13: PyInstaller's CArchive spelled the runtime ``_internal/python314.DLL``
    in 1.8.34 where 1.8.33 had ``.dll``. Deriving the GUID from the raw path made
    that one file two components: the new one saw an installed file "of an equal
    version" and skipped the copy, then the old product's removal deleted the file
    it owned. The Python runtime disappeared from the installed tree mid-upgrade,
    so the later helper could not start and the MSI rolled back after stopping
    the running processes. Changing the GUID back in 1.8.35 fixed its identity
    but the same late-removal migration deleted the file again; ignoring the
    helper's error made that damaged installation report success.
    """
    key = "component:" + msi_file_name(relative_path)
    return str(uuid.uuid5(PRODUCT_NAMESPACE, key)).upper()


def write_directory(path: Path, app_dir: Path, indent: int, component_ids: list[str]) -> list[str]:
    lines: list[str] = []
    pad = " " * indent
    for file_path in sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name.lower())):
        relative = file_path.relative_to(app_dir)
        if file_path.is_dir():
            directory_id = make_id("dir", relative)
            lines.append(f'{pad}<Directory Id="{directory_id}" Name="{esc(file_path.name)}">')
            lines.extend(write_directory(file_path, app_dir, indent + 2, component_ids))
            lines.append(f"{pad}</Directory>")
        elif file_path.is_file():
            component_id = make_id("cmp", relative)
            file_id = make_id("fil", relative)
            guid = component_guid(relative)
            component_ids.append(component_id)
            lines.append(f'{pad}<Component Id="{component_id}" Guid="{{{guid}}}">')
            lines.append(
                f'{pad}  <File Id="{file_id}" Source="{esc(file_path)}" KeyPath="yes" />'
            )
            lines.append(f"{pad}</Component>")
    return lines


def generate(app_dir: Path, output: Path) -> None:
    component_ids: list[str] = []
    lines = [
        f'<Wix xmlns="{WIX_NS}">',
        "  <Fragment>",
        '    <DirectoryRef Id="INSTALLFOLDER">',
    ]
    lines.extend(write_directory(app_dir, app_dir, 6, component_ids))
    lines.extend(
        [
            "    </DirectoryRef>",
            "  </Fragment>",
            "  <Fragment>",
            '    <ComponentGroup Id="ApplicationFiles">',
        ]
    )
    for component_id in component_ids:
        lines.append(f'      <ComponentRef Id="{component_id}" />')
    lines.extend(
        [
            "    </ComponentGroup>",
            "  </Fragment>",
            "</Wix>",
            "",
        ]
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate WiX file entries for app files.")
    parser.add_argument("--app-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    generate(args.app_dir.resolve(), args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
