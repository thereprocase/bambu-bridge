"""Check versioned paths and text for common private deployment artifacts.

Reports locations and categories, never matching credential values. Review
synthetic fixtures separately with a secret scanner before publication.
"""

from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = re.compile(
    r"(?:^|/)(?:dev-notes|captures|node_modules|\.venv|\.local)/|"
    r"\.(?:keystore|jks|p12|key|apk|aab|db|sqlite3|jsonl)$|"
    r"(?:^|/)(?:local\.properties|AGENTS\.md|CLAUDE\.md|\.env)$"
)
CHECKS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub credential": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b"),
    "personal home path": re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]+/"),
    "tailnet hostname": re.compile(r"\b[\w.-]+\.tail[0-9a-f]{5,}\.ts\.net\b"),
}


def main() -> int:
    names = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=ROOT
    ).decode().split("\0")
    findings = []
    for name in filter(None, names):
        if FORBIDDEN.search(name):
            findings.append(f"{name}: excluded artifact type")
        file = ROOT / name
        if file.is_symlink():
            findings.append(f"{name}: review symlink target")
            continue
        data = file.read_bytes()
        if b"\0" in data:
            continue
        for number, line in enumerate(data.decode("utf8", "replace").splitlines(), 1):
            for category, pattern in CHECKS.items():
                if pattern.search(line):
                    findings.append(f"{name}:{number}: {category}")
    print("\n".join(findings) if findings else "Privacy pattern checks passed.")
    return int(bool(findings))


if __name__ == "__main__":
    sys.exit(main())
