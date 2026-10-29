"""Aktuellen Digest eines Image-Tags bei der Registry abfragen (ohne etwas herunterzuladen).

Es wird nur das Manifest per HEAD angefragt. Bei Docker Hub zählt das nicht gegen das
Pull-Limit. Öffentliche Images brauchen keine Zugangsdaten; private Registries melden
„Anmeldung nötig“ und werden übersprungen.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request

DOCKER_HUB = "registry-1.docker.io"
ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
))
TIMEOUT = 15


class RegistryError(Exception):
    pass


def parse_ref(ref: str) -> tuple[str, str, str]:
    """„nginx:1.27“ -> (registry-1.docker.io, library/nginx, 1.27)."""
    name, tag = ref, "latest"
    if "@" in name:
        raise RegistryError("per Digest festgelegt")
    letzter = name.rsplit("/", 1)[-1]
    if ":" in letzter:
        name, tag = name.rsplit(":", 1)
    teile = name.split("/", 1)
    if len(teile) == 2 and ("." in teile[0] or ":" in teile[0] or teile[0] == "localhost"):
        registry, repo = teile
    else:
        registry, repo = DOCKER_HUB, name
    if registry in ("docker.io", "index.docker.io"):
        registry = DOCKER_HUB
    if registry == DOCKER_HUB and "/" not in repo:
        repo = "library/" + repo
    return registry, repo, tag


def normalize_repo(name: str) -> str:
    """Repository-Namen vergleichbar machen (für RepoDigests)."""
    try:
        registry, repo, _ = parse_ref(name.split("@")[0])
    except RegistryError:
        return name
    return f"{registry}/{repo}"


def _parse_challenge(header: str) -> dict[str, str]:
    return dict(re.findall(r'(\w+)="([^"]*)"', header))


def _token(challenge: dict[str, str], repo: str) -> str:
    realm = challenge.get("realm")
    if not realm or not realm.startswith("https://"):
        raise RegistryError("unbekannte Anmeldung der Registry")
    params = {"scope": challenge.get("scope") or f"repository:{repo}:pull"}
    if challenge.get("service"):
        params["service"] = challenge["service"]
    url = realm + ("&" if "?" in realm else "?") + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            daten = json.load(r)
    except urllib.error.HTTPError as exc:
        raise RegistryError("Anmeldung nötig (privates Image?)" if exc.code in (401, 403)
                            else f"Token-Abruf: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RegistryError(f"Registry nicht erreichbar: {exc}") from exc
    token = daten.get("token") or daten.get("access_token")
    if not token:
        raise RegistryError("Registry hat kein Token geliefert")
    return token


def _request(url: str, method: str, token: str | None):
    req = urllib.request.Request(url, method=method, headers={"Accept": ACCEPT})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def remote_digest(ref: str) -> str:
    registry, repo, tag = parse_ref(ref)
    url = f"https://{registry}/v2/{repo}/manifests/{tag}"
    token = None
    for versuch in range(2):
        try:
            with _request(url, "HEAD", token) as r:
                digest = r.headers.get("Docker-Content-Digest")
            if digest:
                return digest
            # Manche Registries liefern den Digest nur beim GET – dann selbst berechnen.
            with _request(url, "GET", token) as r:
                return "sha256:" + hashlib.sha256(r.read()).hexdigest()
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and versuch == 0:
                header = exc.headers.get("WWW-Authenticate", "")
                if not header.lower().startswith("bearer"):
                    raise RegistryError("Anmeldung nötig (privates Image?)") from exc
                token = _token(_parse_challenge(header), repo)
                continue
            if exc.code == 404:
                raise RegistryError("Tag in der Registry nicht gefunden") from exc
            if exc.code == 429:
                raise RegistryError("Abruf-Limit der Registry erreicht") from exc
            raise RegistryError(f"Registry: HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise RegistryError(f"Registry nicht erreichbar: {getattr(exc, 'reason', exc)}") from exc
    raise RegistryError("Anmeldung an der Registry fehlgeschlagen")
