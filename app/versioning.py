"""API 版本解析。

版本可以通过两种方式指定：

1. 路径前缀：``/v1/orders``、``/v2/orders``；
2. 请求头：``X-API-Version: v2``（无路径前缀时生效）。

两者都没有时回落为配置的默认版本（``v1``，标记为 deprecated，便于观察
旧调用方式的存量流量）。请求了不存在的版本（含形如 ``/v3/...`` 的未知前缀
或头里写 ``v9``）抛 :class:`VersionNotSupportedError`，与普通 404 路由缺失
明确区分开。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.config import GovernanceConfig
from app.errors import VersionNotSupportedError

_VERSION_PREFIX_RE = re.compile(r"^/(v\d+)(?=/|$)")
_VERSION_NAME_RE = re.compile(r"^v\d+$")


@dataclass
class VersionInfo:
    version: str
    source: str  # "path" | "header" | "default"
    deprecated: bool


def _split_version_prefix(path: str) -> tuple[str | None, str]:
    """从路径中切出 (版本前缀, 剩余路径)；没有前缀则为 (None, 原路径)。"""
    match = _VERSION_PREFIX_RE.match(path)
    if match is None:
        return None, path
    prefix = match.group(1)
    rest = path[match.end():]
    if not rest.startswith("/"):
        rest = "/" + rest
    return prefix, rest


def resolve_version(path: str, header: str | None, cfg: GovernanceConfig) -> VersionInfo:
    """解析目标版本。路径前缀优先，其次 ``X-API-Version`` 头，最后默认版本。"""
    prefix, _ = _split_version_prefix(path)
    if prefix is not None:
        if prefix not in cfg.supported_versions:
            raise VersionNotSupportedError(
                details={
                    "requested_version": prefix,
                    "source": "path",
                    "supported_versions": list(cfg.supported_versions),
                    "latest_version": cfg.latest_version,
                }
            )
        return VersionInfo(prefix, "path", prefix in cfg.deprecated_versions)

    if header is not None:
        header = header.strip()
        if not _VERSION_NAME_RE.match(header):
            raise VersionNotSupportedError(
                message=f"malformed API version header: {header!r}",
                details={
                    "requested_version": header,
                    "source": "header",
                    "expected_format": "v<number>",
                    "supported_versions": list(cfg.supported_versions),
                },
            )
        if header not in cfg.supported_versions:
            raise VersionNotSupportedError(
                details={
                    "requested_version": header,
                    "source": "header",
                    "supported_versions": list(cfg.supported_versions),
                    "latest_version": cfg.latest_version,
                }
            )
        return VersionInfo(header, "header", header in cfg.deprecated_versions)

    return VersionInfo(
        cfg.default_version, "default", cfg.default_version in cfg.deprecated_versions
    )


def strip_version_prefix(path: str) -> str:
    """把 ``/v2/orders`` 还原成 ``/orders``，让路由表无需按版本重复注册。"""
    _, rest = _split_version_prefix(path)
    return rest
