"""Subprocess execution shared by the gcloud and kubectl clients.

Injectable so clients and steps test without touching real binaries.
"""

import logging
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner:
    """Runs a command, returns its result."""

    def run(self, args: list[str], stdin: str | None = None) -> CommandResult:
        log.debug("exec: %s", " ".join(args))
        proc = subprocess.run(args, input=stdin, capture_output=True, text=True)
        return CommandResult(proc.returncode, proc.stdout.strip(), proc.stderr.strip())
