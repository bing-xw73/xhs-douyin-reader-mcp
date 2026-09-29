"""Fail when publishable files contain likely credentials or private deployment data."""
import ipaddress
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP_PARTS = {".git", ".venv", "__pycache__", ".pytest_cache"}
TEXT_SUFFIXES = {
    "", ".py", ".md", ".txt", ".example", ".service", ".conf",
    ".toml", ".yaml", ".yml", ".json", ".gitignore",
}

PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b"),
    "generic API token": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "MCP URL credential": re.compile(r"https?://[^\s/]+/[A-Za-z0-9_-]{43,128}/mcp\b"),
}
ASSIGNMENT = re.compile(
    r"(?im)^[ \t]*(?:MCP_SECRET|SILICONFLOW_API_KEY|GITHUB_TOKEN|GH_TOKEN)[ \t]*=[ \t]*(\S*)[ \t]*$"
)
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")


def publishable_files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in SKIP_PARTS for part in path.parts):
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name in {"LICENSE", ".gitignore"}:
            yield path


def main():
    findings = []
    for path in publishable_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        relative = path.relative_to(ROOT)
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                findings.append((str(relative), label))
        for match in ASSIGNMENT.finditer(text):
            value = match.group(1)
            if value and not value.startswith(("REPLACE_", "${", "<", "YOUR_")):
                findings.append((str(relative), "non-placeholder secret assignment"))
        for candidate in IPV4.findall(text):
            try:
                address = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if address.is_global:
                findings.append((str(relative), "public IPv4 address"))
    if (ROOT / ".env").exists():
        findings.append((".env", "private environment file exists in release tree"))
    if findings:
        for path, label in sorted(set(findings)):
            print("%s: %s" % (path, label))
        return 1
    print("PASS: no private keys, likely API tokens, credential-bearing MCP URLs, or public IPv4 addresses found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
