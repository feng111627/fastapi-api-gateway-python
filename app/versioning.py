"""对外接口版本判定。

- v1：已过时但仍可用，响应带 Deprecation/Sunset 头，可被调用方观察到。
- v2：当前版本。
- 其他版本：明确报 unsupported_version 错误，与限流、幂等在同一管线上生效。
"""

SUPPORTED_VERSIONS: tuple[str, ...] = ("v1", "v2")
DEPRECATED_VERSIONS: tuple[str, ...] = ("v1",)
CURRENT_VERSION: str = "v2"
V1_SUNSET_DATE: str = "Sat, 01 May 2027 00:00:00 GMT"


def is_supported(version: str) -> bool:
    """版本是否存在。"""
    return version in SUPPORTED_VERSIONS


def is_deprecated(version: str) -> bool:
    """版本是否已过时但仍可用。"""
    return version in DEPRECATED_VERSIONS
