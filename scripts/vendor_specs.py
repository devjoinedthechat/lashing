"""Vendor the DCSA OpenAPI specs and conformance sample messages at pinned commits.

Run with `uv run python scripts/vendor_specs.py`. It rewrites src/lashing/dcsa/specs/ and
tests/fixtures/dcsa/. Commit the result; lashing never fetches specs at runtime.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SPECS = ROOT / "src" / "lashing" / "dcsa" / "specs"
FIXTURES = ROOT / "tests" / "fixtures" / "dcsa"

OPENAPI_REPO = "dcsaorg/DCSA-OpenAPI"
OPENAPI_SHA = "d03a1c7969b4d8807512db4907f4802ee4befdba"
CONFORMANCE_REPO = "dcsaorg/Conformance-Gateway"
CONFORMANCE_SHA = "1d0bf380123a849f6b15ad255055f4abcfb96f1a"

# name -> (repo, sha, path)
SPEC_SOURCES = {
    "bkg-2.0.5": (OPENAPI_REPO, OPENAPI_SHA, "bkg/v2/BKG_v2.0.5.yaml"),
    "cs-1.0.4": (OPENAPI_REPO, OPENAPI_SHA, "cs/v1/CS_v1.0.4.yaml"),
    # DCSA-OpenAPI main still carries 3.0.0-Beta-1; the Conformance Gateway has the released 3.0.0.
    "tnt-3.0.0": (CONFORMANCE_REPO, CONFORMANCE_SHA, "tnt/src/main/resources/standards/tnt/schemas/TNT_v3.0.0.yaml"),
}

_BOOKING_MESSAGES = "booking/src/main/resources/standards/booking/messages"
FIXTURE_SOURCES = {
    f"booking-{kind}.json": (CONFORMANCE_REPO, CONFORMANCE_SHA, f"{_BOOKING_MESSAGES}/booking-api-2.0.0-{kind}.json")
    for kind in (
        "dry-cargo",
        "reefer",
        "non-operating-reefer",
        "dg",
        "routing-reference",
        "store-door-at-origin",
        "store-door-at-destination",
    )
}
_TNT_MESSAGES = "tnt/src/main/resources/standards/tnt/messages"
FIXTURE_SOURCES |= {
    f"tnt-{kind}.json": (CONFORMANCE_REPO, CONFORMANCE_SHA, f"{_TNT_MESSAGES}/tnt-300-{kind}.json")
    for kind in ("response", "response-nextpage")
}


def fetch(repo: str, sha: str, path: str) -> bytes:
    url = f"https://raw.githubusercontent.com/{repo}/{sha}/{path}"
    with urllib.request.urlopen(url, timeout=60) as response:
        return bytes(response.read())


def main() -> None:
    SPECS.mkdir(parents=True, exist_ok=True)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    sources: dict[str, dict[str, str]] = {}
    for name, (repo, sha, path) in SPEC_SOURCES.items():
        document = yaml.safe_load(fetch(repo, sha, path))
        (SPECS / f"{name}.json").write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        sources[name] = {"repository": repo, "commit": sha, "path": path, "version": document["info"]["version"]}
    for name, (repo, sha, path) in FIXTURE_SOURCES.items():
        (FIXTURES / name).write_bytes(fetch(repo, sha, path))
        sources[f"fixture:{name}"] = {"repository": repo, "commit": sha, "path": path}
    (SPECS / "SOURCES.json").write_text(json.dumps(sources, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"vendored {len(SPEC_SOURCES)} specs and {len(FIXTURE_SOURCES)} fixtures")


if __name__ == "__main__":
    main()
