"""Closed runtime profiles; no CLI-supplied image, source or root override."""
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class RuntimeProfile:
    binary: str
    image: str
    platforms: object
    host_root: str
    pending: bool = False


AUTHORING = RuntimeProfile(
    "37404c9f4364e456b23e77a57c9f01a65a250666",
    "ghcr.io/datum-labs/apps@sha256:8eb09c545ba3f1e8f1a7eddb9f5667ae914786cf4da1d63a21cd38ef7652e610",
    MappingProxyType({
        "arm64": ("sha256:bcb1f61d50892016c3701210d6a3bbf76fb6ee015e5a55280fcbd19c2bc555b7", "sha256:44e712c7c713ac7a3078112cc191ea732799c77c129d519bfc64baf78a8bb4a6"),
        "amd64": ("sha256:65890f10a73fd056458b16d7098256ae7385fea6acbff69b59628dc3d0c7f3f6", "sha256:2c3919924423e1c2cd7fe037aceed55c1618955c7cdd02975180e81b44f36699")}),
    "config/overlays/test-infra/host")
PENDING = RuntimeProfile(
    "d3b84cfb90b1720f951c25e407adc7363980368b",
    "ghcr.io/datum-labs/apps@sha256:7246924c7e551403e79098d1c88816ac6608853fef691589bb2cfa0b6400cce6",
    MappingProxyType({
        "arm64": ("sha256:49820334195f972c5985eaf04e28e49c3c70cbdf2a5aed4705298ec4cf7fd8a9", "sha256:8ab5692300a7176a7538cf55dd924f845036e8ea07ea9f59abfbe4e93453626f"),
        "amd64": ("sha256:afd13738ae5f8c9f006bb2bf72f5703d747135dbda2a0f01ae2505f434db4a6c", "sha256:f5ceec405e52356b9a218da465f64db6d3dcf2e25bdc2ede28d863ed70e7b7be")}),
    "config/experimental/pending-app-host", True)


def selected(pending: bool) -> RuntimeProfile:
    return PENDING if pending else AUTHORING
