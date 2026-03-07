from __future__ import annotations

import re
from dataclasses import dataclass


_STABLE_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    micro: int

    @classmethod
    def parse(cls, s: str) -> "Version":
        m = _STABLE_VERSION_RE.match(s.strip())
        if not m:
            raise ValueError(f"Unsupported version format: {s!r}")
        return cls(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    def to_release_slug(self) -> str:
        # python.org release URLs use dotless versions, e.g. 3.12.10 -> 31210
        return f"{self.major}{self.minor}{self.micro}"

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.micro}"


def is_stable_version_dirname(dirname: str) -> bool:
    # ftp listing entries look like "3.12.10/".
    s = dirname.strip().rstrip("/")
    return bool(_STABLE_VERSION_RE.match(s))

