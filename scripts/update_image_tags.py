#!/usr/bin/env python3
"""Update Home Assistant add-on config.yaml version fields to pinned Docker tags.

Policies:
- linuxserver/sonarr: preserve channel inferred from config.version (develop/stable/nightly)
- linuxserver/radarr: preserve channel inferred from config.version (stable if no develop/nightly)
  and prefer a precise LinuxServer -lsN build tag whose amd64/arm64 digests match the channel alias.
- haugene/transmission-openvpn: select the highest strict X.Y.Z release tag only, requiring
  linux/amd64 and linux/arm64 images. Tags such as latest, 5, 5.5, master, edge, and dev are ignored.

The Home Assistant add-on `version:` must equal the Docker tag when `image:` is present.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
API_BASE = "https://hub.docker.com/v2/repositories/{repository}/tags?page_size=100&ordering=last_updated"
USER_AGENT = "ha-addon-image-tag-updater/2.0"
MAX_PAGES = 10  # Docker Hub legacy tags API may deny page 11+ on large repositories
REQUIRED_ARCHES = ("amd64", "arm64")

LS_IMAGE_RE = re.compile(
    r"^\s*image:\s*[\"']?(?:(?:ghcr\.io|lscr\.io|docker\.io)/)?linuxserver/(radarr|sonarr)[\"']?\s*(?:#.*)?$"
)
TRANSMISSION_IMAGE_RE = re.compile(
    r"^\s*image:\s*[\"']?(?:(?:docker\.io)/)?haugene/transmission-openvpn[\"']?\s*(?:#.*)?$"
)
VERSION_RE = re.compile(r"^\s*version:\s*[\"']?([^\"'\s#]+)")

CHANNELS: dict[str, dict[str, Any]] = {
    "stable": {
        "alias": "latest",
        "patterns": [
            re.compile(r"^\d+\.\d+\.\d+\.\d+-ls\d+$"),
            re.compile(r"^version-\d+\.\d+\.\d+\.\d+$"),
        ],
    },
    "develop": {
        "alias": "develop",
        "patterns": [
            re.compile(r"^develop-\d+\.\d+\.\d+\.\d+-ls\d+$"),
            re.compile(r"^develop-version-\d+\.\d+\.\d+\.\d+$"),
            re.compile(r"^\d+\.\d+\.\d+-develop$"),
        ],
    },
    "nightly": {
        "alias": "nightly",
        "patterns": [
            re.compile(r"^nightly-\d+\.\d+\.\d+\.\d+-ls\d+$"),
            re.compile(r"^\d+\.\d+\.\d+\.\d+-ls\d+-nightly$"),
            re.compile(r"^nightly-version-\d+\.\d+\.\d+\.\d+$"),
            re.compile(r"^\d+\.\d+\.\d+-nightly$"),
        ],
    },
}
STRICT_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def get_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Impossible de lire l'API Docker Hub ({url}): {exc}") from exc


def fetch_all_tags(repository: str) -> dict[str, dict[str, Any]]:
    url: str | None = API_BASE.format(repository=repository)
    tags: dict[str, dict[str, Any]] = {}
    for _ in range(MAX_PAGES):
        if not url:
            return tags
        data = get_json(url)
        for tag in data.get("results", []):
            name = tag.get("name")
            if isinstance(name, str):
                tags[name] = tag
        url = data.get("next")
    if url:
        # Docker Hub's legacy tags endpoint can return HTTP 403 when paging past
        # the first 1,000 tags of large public repositories. Since results are
        # ordered by last_updated, the first 1,000 are sufficient for finding
        # the currently published channel aliases and their matching build tags.
        # Return the partial recent-tag set rather than failing the whole run.
        print(
            f"AVERTISSEMENT: {repository} contient plus de {MAX_PAGES * 100} tags; "
            f"seuls les {MAX_PAGES * 100} tags les plus récemment mis à jour "
            "seront examinés (limitation volontaire pour éviter le HTTP 403 sur les pages profondes).",
            file=sys.stderr,
        )
    return tags


def digest_map(tag: dict[str, Any] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    if not tag:
        return result
    for image in tag.get("images", []):
        if image.get("os") != "linux":
            continue
        arch = image.get("architecture")
        digest = image.get("digest")
        if arch in REQUIRED_ARCHES and digest:
            result[arch] = digest
    return result


def channel_for_version(version: str) -> str:
    lowered = version.lower()
    if "nightly" in lowered:
        return "nightly"
    if "develop" in lowered:
        return "develop"
    return "stable"


def linuxserver_target_tag(app: str, channel: str, all_tags: dict[str, dict[str, Any]] | None = None) -> str:
    config = CHANNELS[channel]
    alias = config["alias"]
    tags = all_tags if all_tags is not None else fetch_all_tags(f"linuxserver/{app}")
    alias_digests = digest_map(tags.get(alias))
    if any(not alias_digests.get(arch) for arch in REQUIRED_ARCHES):
        raise RuntimeError(
            f"Le tag alias '{alias}' de linuxserver/{app} ne fournit pas les architectures "
            f"{', '.join(REQUIRED_ARCHES)}. Aucun changement effectué."
        )

    for pattern in config["patterns"]:
        candidates: list[dict[str, Any]] = []
        for name, tag in tags.items():
            if not pattern.fullmatch(name):
                continue
            candidate_digests = digest_map(tag)
            if all(candidate_digests.get(arch) == alias_digests.get(arch) for arch in REQUIRED_ARCHES):
                candidates.append(tag)
        if candidates:
            candidates.sort(key=lambda item: item.get("last_updated", ""), reverse=True)
            return candidates[0]["name"]

    raise RuntimeError(
        f"Aucun tag versionné trouvé pour linuxserver/{app} (canal {channel}) "
        f"avec les mêmes digests amd64/arm64 que '{alias}'. Aucun changement effectué."
    )


def transmission_target_tag(all_tags: dict[str, dict[str, Any]] | None = None) -> str:
    tags = all_tags if all_tags is not None else fetch_all_tags("haugene/transmission-openvpn")
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for name, tag in tags.items():
        match = STRICT_SEMVER.fullmatch(name)
        if not match:
            continue
        arches = digest_map(tag)
        if not all(arches.get(arch) for arch in REQUIRED_ARCHES):
            continue
        version_tuple = tuple(int(part) for part in match.groups())
        candidates.append((version_tuple, name))
    if not candidates:
        raise RuntimeError(
            "Aucun tag Transmission au format strict X.Y.Z trouvé avec les architectures "
            "amd64 et arm64. Aucun changement effectué."
        )
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def get_config_identity(content: str) -> tuple[str, str] | None:
    image: str | None = None
    for line in content.splitlines():
        if LS_IMAGE_RE.match(line):
            match = LS_IMAGE_RE.match(line)
            assert match
            return ("linuxserver", match.group(1))
        if TRANSMISSION_IMAGE_RE.match(line):
            return ("transmission", "haugene/transmission-openvpn")
    return None


def update_version_line(content: str, new_version: str) -> tuple[str, str | None]:
    lines = content.splitlines(keepends=True)
    for index, line in enumerate(lines):
        match = VERSION_RE.match(line.rstrip("\r\n"))
        if not match:
            continue
        old_version = match.group(1)
        if old_version == new_version:
            return content, old_version
        newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        indent_match = re.match(r"^\s*", line)
        indent = indent_match.group(0) if indent_match else ""
        lines[index] = f'{indent}version: "{new_version}"{newline}'
        return "".join(lines), old_version
    return content, None


def main() -> int:
    configs = sorted(ROOT.rglob("config.yaml"))
    checked = changed = 0
    targets: dict[tuple[str, str], str] = {}

    for path in configs:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"AVERTISSEMENT: impossible de lire {path}: {exc}", file=sys.stderr)
            continue

        identity = get_config_identity(content)
        if not identity:
            continue
        version_match = next((VERSION_RE.match(line) for line in content.splitlines() if VERSION_RE.match(line)), None)
        if not version_match:
            print(f"AVERTISSEMENT: aucun champ version: trouvé dans {path}", file=sys.stderr)
            continue

        old_version = version_match.group(1)
        provider, app = identity
        if provider == "linuxserver":
            channel = channel_for_version(old_version)
            key = (provider, f"{app}:{channel}")
            if key not in targets:
                tags = fetch_all_tags(f"linuxserver/{app}")
                targets[key] = linuxserver_target_tag(app, channel, tags)
            target_version = targets[key]
            print(f"{path.relative_to(ROOT)}: LinuxServer {app}, canal {channel}")
        else:
            key = (provider, app)
            if key not in targets:
                tags = fetch_all_tags(app)
                targets[key] = transmission_target_tag(tags)
            target_version = targets[key]
            print(f"{path.relative_to(ROOT)}: Transmission, tag SemVer strict X.Y.Z")

        checked += 1
        if old_version == target_version:
            print(f"  Déjà à jour: {old_version}")
            continue
        updated, actual_old = update_version_line(content, target_version)
        if actual_old is None:
            print(f"AVERTISSEMENT: aucune ligne version: modifiable dans {path}", file=sys.stderr)
            continue
        path.write_text(updated, encoding="utf-8")
        changed += 1
        print(f"  MIS À JOUR: {actual_old} -> {target_version}")

    if checked == 0:
        print(
            "Aucun config.yaml reconnu. Images prises en charge: linuxserver/radarr, "
            "linuxserver/sonarr et haugene/transmission-openvpn.",
            file=sys.stderr,
        )
        return 1
    print(f"Terminé: {changed} fichier(s) modifié(s), {checked} add-on(s) vérifié(s).")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"ERREUR: {exc}", file=sys.stderr)
        raise SystemExit(1)
