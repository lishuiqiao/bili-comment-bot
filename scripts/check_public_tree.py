"""Check tracked (or staged) files without printing potentially sensitive values."""

import argparse
import re
import subprocess
from pathlib import Path, PurePosixPath

PRIVATE_DIRECTORIES = {".local", ".lh", "data", "backups", "logs", "reports", ".venv"}
PRIVATE_NAMES = {"config.toml", "auth.json", "login.png"}
PRIVATE_SUFFIXES = {".db", ".db-shm", ".db-wal", ".key", ".pem", ".bundle", ".log"}
CONTENT_RULES = {
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "provider token": re.compile(
        rb"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{30,}"
        rb"|github_pat_[A-Za-z0-9_]{40,})"
    ),
    "personal filesystem path": re.compile(rb"/(?:Users|Volumes)/[^\s\"'<>]+"),
    "private conversation or tunnel": re.compile(
        rb"(?:chatgpt\.com/c/[A-Za-z0-9-]+|[A-Za-z0-9-]+\.trycloudflare\.com)"
    ),
}


def private_path(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(PRIVATE_DIRECTORIES.intersection(path.parts))
        or path.name in PRIVATE_NAMES
        or (path.name.startswith(".env") and name != ".env.example")
        or path.suffix in PRIVATE_SUFFIXES
        or path.name.endswith(".local.toml")
        or path.name.endswith("-report.json")
        or bool(re.fullmatch(r".*(?:cookie|credential).*\.json", path.name, re.IGNORECASE))
    )


def check(staged: bool) -> int:
    root = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
    command = (
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"]
        if staged
        else ["git", "ls-files", "-z"]
    )
    names = subprocess.check_output(command, cwd=root).decode().split("\0")
    names = list(filter(None, names))
    staged_bodies = {}
    if staged and names:
        content = subprocess.check_output(
            ["git", "cat-file", "--batch"],
            input=("\n".join(":" + name for name in names) + "\n").encode(),
            cwd=root,
        )
        offset = 0
        for name in names:
            end = content.index(b"\n", offset)
            header = content[offset:end].split()
            if len(header) != 3 or header[1] != b"blob":
                raise ValueError("Could not read staged file")
            size = int(header[2])
            staged_bodies[name] = content[end + 1 : end + 1 + size]
            offset = end + size + 2
    failures = []
    count = 0
    for name in names:
        path = root / name
        if not staged and not path.exists() and not path.is_symlink():
            continue
        count += 1
        if private_path(name):
            failures.append((name, "private runtime file"))
            continue
        if path.is_symlink():
            failures.append((name, "symlinks are not permitted in the public tree"))
            continue
        body = staged_bodies[name] if staged else path.read_bytes()
        for label, pattern in CONTENT_RULES.items():
            if pattern.search(body):
                failures.append((name, label))
    for name, reason in failures:
        print(f"FAIL {name}: {reason}")
    if failures:
        print(f"Public tree check failed: {len(failures)} findings; values were not printed.")
        return 1
    print(f"Public tree check passed: {count} files; no prohibited paths or known markers.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--staged", action="store_true", help="Read staged content, not working files"
    )
    raise SystemExit(check(parser.parse_args().staged))
