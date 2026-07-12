"""Source-bundled open-source acknowledgements and trusted external links."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any


def _entry(**values: Any) -> MappingProxyType:
    return MappingProxyType(values)


CATALOG = (
    _entry(
        id="okaetsu", name="Okaetsu/RE-UE4SS",
        purpose="感谢 Okaetsu 维护 Palworld 专用构建，为游戏提供可用的 UE4SS 发行包。",
        author="Okaetsu", license="MIT", version="experimental-palworld 固定来源",
        source_url="https://github.com/Okaetsu/RE-UE4SS", core=True,
        direct_dependency=False,
        license_text="MIT License；完整许可文本随内置 UE4SS 资源分发。",
    ),
    _entry(
        id="ue4ss", name="UE4SS-RE/RE-UE4SS",
        purpose="提供 Unreal Engine Lua 脚本加载、反射与模组运行能力；感谢上游贡献者。",
        author="UE4SS-RE contributors；Copyright (c) 2022 Narknon",
        copyright="Copyright (c) 2022 Narknon", license="MIT",
        version="Palworld 内置构建的上游",
        source_url="https://github.com/UE4SS-RE/RE-UE4SS", core=True,
        direct_dependency=False,
        license_text="MIT License；Copyright (c) 2022 Narknon。完整许可文本位于 third_party/ue4ss-palworld/LICENSE。",
    ),
    _entry(
        id="flask", name="Flask", purpose="提供仅限本机回环访问的管理 API 与静态界面服务。",
        author="Pallets", license="BSD-3-Clause", version="3.1.3",
        source_url="https://github.com/pallets/flask", core=True, direct_dependency=True,
        license_text="BSD 3-Clause License。",
    ),
    _entry(
        id="pywebview", name="pywebview", purpose="在桌面窗口中承载本机 Web 界面。",
        author="Roman Sirokov and contributors", license="BSD-3-Clause", version="6.2.1",
        source_url="https://github.com/r0x0r/pywebview", core=True, direct_dependency=True,
        license_text="BSD 3-Clause License。",
    ),
    _entry(
        id="pillow", name="Pillow", purpose="校验、转换和保存自定义背景图片。",
        author="Pillow contributors", license="MIT-CMU", version="12.3.0",
        source_url="https://github.com/python-pillow/Pillow", core=True, direct_dependency=True,
        license_text="MIT-CMU（CMU License）；依据 Pillow 12.3.0 wheel METADATA 的 License-Expression。",
    ),
    _entry(
        id="pyinstaller", name="PyInstaller", purpose="将应用及其运行资源构建为 Windows 便携程序。",
        author="PyInstaller Development Team", license="GPL-2.0-or-later with Bootloader Exception", version="6.21.0",
        source_url="https://github.com/pyinstaller/pyinstaller", core=True, direct_dependency=True,
        license_text="GNU GPL 2.0 or later；生成程序适用 PyInstaller Bootloader Exception。",
    ),
    _entry(
        id="palworld-modding-docs", name="Palworld Modding Docs",
        purpose="Palworld 模组结构与开发资料参考（资料，不是代码依赖）。",
        author="Palworld Modding Community", license="CC BY-NC-SA 4.0", version="在线资料固定来源",
        source_url="https://pwmodding.wiki/", core=True, direct_dependency=False,
        license_text="Creative Commons Attribution-NonCommercial-ShareAlike 4.0。",
    ),
)

TRUSTED_LINKS = MappingProxyType({item["id"]: item["source_url"] for item in CATALOG})


def catalog_payload() -> list[dict[str, Any]]:
    """Return detached JSON-ready catalog values without network access."""
    return [dict(item) for item in CATALOG]
