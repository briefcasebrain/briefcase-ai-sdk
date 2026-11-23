"""The offline image bundle: an OCI image layout archive (``engine-images-<version>-<arch>.tar``).

The bundle's sha256 and size are pinned by the signed manifest, so once
:func:`briefcase.engine.trust.check_file` passed, every byte of it is the release's. This
module then reads the layout's ``index.json`` (no extraction) to find, for every image the
manifest names by digest, the descriptor with exactly that digest and the image ID Docker
will give it, loads the archive into Docker, and checks the loaded images are those IDs.

Two loaders, tried in order:

* ``docker load -i FILE``: Docker 25+ loads OCI layout archives (and ``docker save`` tarballs);
* ``skopeo copy oci-archive:FILE:<ref> docker-daemon:<name>``, per image, when ``docker load``
  fails and ``skopeo`` is on PATH (older Docker that only reads ``docker save`` tarballs).

A plain ``docker save`` tarball (``manifest.json`` only, no ``index.json``) is accepted too, but
it carries no registry digests: images are then matched by repository name and image ID only,
and the install says so.
"""
from __future__ import annotations

import json
import posixpath
import re
import shutil
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

INDEX_TYPES = ("application/vnd.oci.image.index.v1+json",
               "application/vnd.docker.distribution.manifest.list.v2+json")
GOARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
MAX_JSON = 4 << 20


class BundleError(RuntimeError):
    def __init__(self, message: str, fix: Optional[str] = None) -> None:
        super().__init__(message)
        self.fix = fix


@dataclass
class Expected:
    """One manifest image as found in the bundle."""
    name: str
    digest: str
    candidates: List[str]          # image IDs Docker may give it, most likely first
    ref_name: Optional[str] = None  # the layout's org.opencontainers.image.ref.name, for skopeo
    position: int = 0               # its position in index.json, for skopeo's @N
    digest_checked: bool = True


@dataclass
class Layout:
    path: Path
    oci: bool
    index: Dict[str, Any] = field(default_factory=dict)
    docker_manifest: List[Dict[str, Any]] = field(default_factory=list)
    _names: Dict[str, str] = field(default_factory=dict)

    def read(self, name: str) -> bytes:
        member = self._names.get(_norm(name))
        if member is None:
            raise BundleError(f"{self.path.name} has no {name}")
        with tarfile.open(self.path, "r:*") as tar:
            handle = tar.extractfile(member)
            if handle is None:
                raise BundleError(f"{self.path.name}: {name} is not a file")
            data = handle.read(MAX_JSON + 1)
        if len(data) > MAX_JSON:
            raise BundleError(f"{self.path.name}: {name} is too large for a JSON document")
        return data

    def blob_json(self, digest: str) -> Dict[str, Any]:
        algo, _, hexpart = digest.partition(":")
        try:
            return json.loads(self.read(f"blobs/{algo}/{hexpart}"))
        except ValueError as exc:
            raise BundleError(f"{self.path.name}: blob {digest} is not JSON") from exc


def _norm(name: str) -> str:
    name = posixpath.normpath(name.lstrip("/"))
    return name[2:] if name.startswith("./") else name


def open_layout(path: Path) -> Layout:
    """Index the archive's members and read ``index.json`` / ``manifest.json``."""
    try:
        with tarfile.open(path, "r:*") as tar:
            names = {_norm(m.name): m.name for m in tar.getmembers() if m.isfile()}
    except (tarfile.TarError, OSError) as exc:
        raise BundleError(f"{path.name} is not a readable tar archive ({exc})",
                          fix="download the bundle again from the portal") from exc
    layout = Layout(path=path, oci="index.json" in names, _names=names)
    if layout.oci:
        layout.index = json.loads(layout.read("index.json"))
    elif "manifest.json" in names:
        data = json.loads(layout.read("manifest.json"))
        layout.docker_manifest = data if isinstance(data, list) else []
    else:
        raise BundleError(f"{path.name} is neither an OCI image layout (index.json) nor a "
                          "`docker save` archive (manifest.json)",
                          fix="download the offline bundle again from the portal")
    return layout


def _walk(layout: Layout, index: Mapping[str, Any], depth: int = 0):
    """Every descriptor reachable from an index, with its top-level position."""
    for position, desc in enumerate(index.get("manifests") or []):
        if not isinstance(desc, dict):
            continue
        yield position, desc
        if depth < 2 and desc.get("mediaType") in INDEX_TYPES:
            try:
                child = layout.blob_json(desc["digest"])
            except BundleError:
                continue
            for _, inner in _walk(layout, child, depth + 1):
                yield position, inner


def _platform_manifest(layout: Layout, index_digest: str, arch: str) -> Optional[Dict[str, Any]]:
    index = layout.blob_json(index_digest)
    want = GOARCH.get(arch, arch)
    for desc in index.get("manifests") or []:
        plat = desc.get("platform") or {}
        if plat.get("os", "linux") == "linux" and plat.get("architecture") == want:
            return desc
    return None


def expected_images(layout: Layout, images: Mapping[str, Mapping[str, str]], arch: str) -> List[Expected]:
    """For each manifest image, the bundle descriptor with its digest and the IDs to expect."""
    out: List[Expected] = []
    if not layout.oci:
        return _expected_docker_archive(layout, images)
    descriptors = list(_walk(layout, layout.index))
    for name, image in sorted(images.items()):
        digest = image["digest"]
        found = next(((pos, d) for pos, d in descriptors if d.get("digest") == digest), None)
        if found is None:
            raise BundleError(f"the bundle does not contain image {name} ({image['repository']}@{digest}) "
                              "that the signed manifest names",
                              fix="download the bundle for the same version as the manifest")
        position, desc = found
        candidates: List[str] = []
        manifest_digest = digest
        if desc.get("mediaType") in INDEX_TYPES:
            child = _platform_manifest(layout, digest, arch)
            if child is None:
                raise BundleError(f"image {name} in the bundle has no linux/{GOARCH.get(arch, arch)} variant",
                                  fix="download the bundle for this machine's architecture")
            manifest_digest = child["digest"]
        manifest = layout.blob_json(manifest_digest)
        config = (manifest.get("config") or {}).get("digest")
        if not config:
            raise BundleError(f"image {name} in the bundle has no config")
        candidates += [config, digest]
        if manifest_digest != digest:
            candidates.append(manifest_digest)
        annotations = desc.get("annotations") or {}
        out.append(Expected(name=name, digest=digest, candidates=candidates,
                            ref_name=annotations.get("org.opencontainers.image.ref.name"),
                            position=position))
    return out


def _expected_docker_archive(layout: Layout, images: Mapping[str, Mapping[str, str]]) -> List[Expected]:
    out = []
    for name, image in sorted(images.items()):
        repo = image["repository"]
        match = None
        for entry in layout.docker_manifest:
            tags = entry.get("RepoTags") or []
            if any(t.rsplit(":", 1)[0].endswith(repo) for t in tags):
                match = entry
                break
        if match is None:
            raise BundleError(f"the bundle has no image tagged {repo} (for {name})",
                              fix="download the offline bundle again from the portal")
        config = str(match.get("Config") or "")
        hexpart = re.search(r"([0-9a-f]{64})", config)
        if not hexpart:
            raise BundleError(f"the bundle's entry for {repo} names no config digest")
        out.append(Expected(name=name, digest=image["digest"], candidates=["sha256:" + hexpart.group(1)],
                            digest_checked=False))
    return out


# --------------------------------------------------------------------------- loading


def load(layout: Layout, expected: List[Expected], stable: Mapping[str, str], docker: Any,
         which: Callable = shutil.which, runner: Optional[Callable] = None, log: Callable = print) -> str:
    """Load the bundle into Docker; returns the method used (``docker load`` or ``skopeo``)."""
    error = docker.load(layout.path)
    if error is None:
        return "docker load"
    skopeo = which("skopeo")
    if skopeo is None or not layout.oci:
        raise BundleError(f"docker load {layout.path.name} failed: {error}",
                          fix="update Docker to 25 or newer (it loads OCI archives), or install skopeo, then re-run")
    log(f"docker load failed ({error.splitlines()[0] if error else 'no detail'}); trying skopeo")
    import subprocess

    run = runner or subprocess.run
    for item in expected:
        source = f"oci-archive:{layout.path}:" + (item.ref_name if item.ref_name else f"@{item.position}")
        result = run([skopeo, "copy", "--quiet", source, f"docker-daemon:{stable[item.name]}"],
                     capture_output=True, text=True)
        if getattr(result, "returncode", 1) != 0:
            raise BundleError(f"skopeo copy of {item.name} failed: "
                              f"{(getattr(result, 'stderr', '') or '').strip()[:300]}",
                              fix="update Docker to 25 or newer, then re-run")
    return "skopeo"
