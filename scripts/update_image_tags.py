#!/usr/bin/env python3
"""Update Home Assistant add-on config.yaml version fields to pinned Docker tags.

Algorithmes de sélection :
- linuxserver/sonarr et linuxserver/radarr : déduire le canal (stable, develop ou nightly)
  du champ version actuel, lire l'alias du canal, puis choisir un tag de build précis dont les
  digests linux/amd64 et linux/arm64 correspondent exactement à ceux de cet alias.
- haugene/transmission-openvpn : choisir la plus grande version au format strict X.Y.Z.
  Les tags flottants (latest, 5, 5.5, master, edge, dev) et les préversions sont ignorés.
- Seerr officiel : choisir la plus grande version stable au format strict vX.Y.Z.
  Le préfixe v est obligatoire ; les alias (latest, v3, v3.0), develop et les préversions sont ignorés.

Dans un add-on Home Assistant, le champ `version:` sert aussi de tag Docker lorsque `image:`
est défini. Le script met donc ce champ à jour, mais ne déploie pas lui-même l'image dans Home Assistant.

Le script suit trois étapes : (1) reconnaître l'image déclarée dans chaque config.yaml ;
(2) récupérer et filtrer les tags disponibles selon les règles propres à cette image ;
(3) remplacer uniquement la ligne version: si une nouvelle version acceptable a été trouvée.
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
USER_AGENT = "ha-addon-image-tag-updater/3.0"
MAX_PAGES = 10  # Docker Hub legacy tags API may deny page 11+ on large repositories
REQUIRED_ARCHES = ("amd64", "arm64")

LS_IMAGE_RE = re.compile(
    r"^\s*image:\s*[\"']?(?:(?:ghcr\.io|lscr\.io|docker\.io)/)?linuxserver/(radarr|sonarr)[\"']?\s*(?:#.*)?$"
)
TRANSMISSION_IMAGE_RE = re.compile(
    r"^\s*image:\s*[\"']?(?:(?:docker\.io)/)?haugene/transmission-openvpn[\"']?\s*(?:#.*)?$"
)
# Seerr officiel publie les mêmes tags stables sur Docker Hub et GHCR.
# On lit la liste via le dépôt Docker Hub public `seerr/seerr`, même si l'add-on
# téléchargera finalement son image depuis `ghcr.io/seerr-team/seerr`.
SEERR_IMAGE_RE = re.compile(
    r"^\s*image:\s*[\"']?(?:ghcr\.io/seerr-team/seerr|(?:(?:docker\.io)/)?seerr/seerr)[\"']?\s*(?:#.*)?$"
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
# Transmission attend 3 composants numériques sans préfixe : 5.5.2.
STRICT_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
# Seerr suit la même comparaison numérique, mais le `v` initial est obligatoire : v3.0.0.
STRICT_V_SEMVER = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def get_json(url: str) -> dict[str, Any]:
    """Lire une réponse JSON de l'API des tags Docker Hub avec un timeout."""
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
    """Récupérer les tags récents, sans dépasser 10 pages (1000 tags).

    Docker Hub peut renvoyer HTTP 403 sur les pages profondes des gros dépôts.
    On conserve alors le jeu de résultats partiel et récent, avec un avertissement.
    Les sélecteurs plus bas échouent explicitement si ces résultats ne permettent
    pas de prouver qu'un tag est valide : aucune version n'est devinée.
    """
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
    """Retourner les digests des images Linux amd64/arm64 d'un tag.

    Utilisé pour vérifier qu'un tag précis LinuxServer est réellement la même
    build que l'alias de canal (latest/develop/nightly), et pour exclure les tags
    de Transmission/Seerr qui ne publient pas les deux architectures requises.
    """
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
    """Déduire le canal depuis le tag déjà configuré, afin de ne pas changer de canal."""
    lowered = version.lower()
    if "nightly" in lowered:
        return "nightly"
    if "develop" in lowered:
        return "develop"
    return "stable"


def linuxserver_target_tag(app: str, channel: str, all_tags: dict[str, dict[str, Any]] | None = None) -> str:
    """Trouver le tag de build LinuxServer qui correspond au canal sélectionné.

    On compare les digests amd64/arm64 de chaque candidat avec ceux de l'alias
    du canal. On ne se contente donc pas de choisir le nom de version le plus grand :
    cela évite de sélectionner une ancienne build ou un tag d'une autre branche.
    """
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
    """Sélectionner la plus grande release Transmission au format strict X.Y.Z.

    La comparaison est faite sur les trois entiers, pas sur les chaînes : par
    exemple 5.10.0 doit être plus récent que 5.9.9. Les alias et préversions ne
    correspondent pas à STRICT_SEMVER et sont donc automatiquement ignorés.
    """
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


def seerr_target_tag(all_tags: dict[str, dict[str, Any]] | None = None) -> str:
    """Sélectionner la plus grande release stable de Seerr au format vX.Y.Z.

    La règle est volontairement presque identique à celle de Transmission, avec
    deux différences : le tag doit commencer par `v`, et la source de tags est
    le dépôt officiel Docker Hub `seerr/seerr`. Les alias `latest`, `v3`, `v3.0`,
    `develop`, les tags `sha-*` et les préversions `vX.Y.Z-rc...` sont exclus.
    Chaque tag retenu doit publier des images linux/amd64 et linux/arm64.
    """
    tags = all_tags if all_tags is not None else fetch_all_tags("seerr/seerr")
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for name, tag in tags.items():
        # Le fullmatch impose exactement v + trois composants numériques.
        match = STRICT_V_SEMVER.fullmatch(name)
        if not match:
            continue
        # Écarter les tags incomplets pour les architectures visées par l'add-on.
        arches = digest_map(tag)
        if not all(arches.get(arch) for arch in REQUIRED_ARCHES):
            continue
        # Convertir chaque composant en entier pour comparer les versions correctement.
        version_tuple = tuple(int(part) for part in match.groups())
        candidates.append((version_tuple, name))

    if not candidates:
        raise RuntimeError(
            "Aucun tag Seerr stable au format strict vX.Y.Z trouvé avec les architectures "
            "amd64 et arm64. Aucun changement effectué."
        )

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def get_config_identity(content: str) -> tuple[str, str] | None:
    """Identifier le registre/image déclaré dans config.yaml à partir de sa ligne image:."""
    for line in content.splitlines():
        if LS_IMAGE_RE.match(line):
            match = LS_IMAGE_RE.match(line)
            assert match
            return ("linuxserver", match.group(1))
        if TRANSMISSION_IMAGE_RE.match(line):
            return ("transmission", "haugene/transmission-openvpn")
        if SEERR_IMAGE_RE.match(line):
            # Toujours lire les tags du dépôt Docker Hub officiel, même si l'image
            # configurée dans l'add-on est téléchargée depuis GHCR.
            return ("seerr", "seerr/seerr")
    return None


def update_version_line(content: str, new_version: str) -> tuple[str, str | None]:
    """Remplacer uniquement la première ligne `version:` en préservant l'indentation.

    Le reste du YAML n'est pas réécrit, ce qui limite le diff généré par Actions.
    """
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
    """Parcourir les config.yaml du dépôt, calculer une fois chaque cible, puis modifier."""
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
        elif provider == "transmission":
            key = (provider, app)
            if key not in targets:
                tags = fetch_all_tags(app)
                targets[key] = transmission_target_tag(tags)
            target_version = targets[key]
            print(f"{path.relative_to(ROOT)}: Transmission, tag SemVer strict X.Y.Z")
        else:  # provider == "seerr"
            key = (provider, app)
            if key not in targets:
                tags = fetch_all_tags(app)
                targets[key] = seerr_target_tag(tags)
            target_version = targets[key]
            print(f"{path.relative_to(ROOT)}: Seerr, tag SemVer strict vX.Y.Z")

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
            "linuxserver/sonarr, haugene/transmission-openvpn et seerr/seerr.",
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
