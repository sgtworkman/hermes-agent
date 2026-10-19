"""Command and child-environment fragments for native service definitions."""

import json
from html import escape
from pathlib import Path

from hermes_cli._launchers import installation_cli_path


def systemd_launcher_environment(project_root: Path) -> str:
    command = installation_cli_path(project_root)
    if command is None:
        return ""
    # Environment= expands percent specifiers, but treats dollar signs literally.
    assignment = "HERMES_BIN=" + str(command).replace("%", "%%")
    return "Environment=" + json.dumps(assignment, ensure_ascii=False) + "\n"


def launchd_launcher_environment(project_root: Path) -> str:
    command = installation_cli_path(project_root)
    if command is None:
        return ""
    return ("        <key>HERMES_BIN</key>\n"
            f"        <string>{escape(str(command))}</string>\n")


def systemd_command(argv: list[str]) -> str:
    """Quote argv for systemd, including its non-shell specifier expansion."""
    return " ".join('"' + part.replace("\\", "\\\\").replace('"', '\\"')
                    .replace("%", "%%").replace("$", "$$") + '"' for part in argv)
