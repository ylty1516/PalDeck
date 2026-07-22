# PalDeck 完整重构实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 在现有项目上交付可打包的 Windows Steam 版 Palworld Mod 管理器，实现经过磁盘验证的安装/启停、Nexus 只读目录、三套主题、自定义背景、动态樱花和完整测试。

**架构：** 保留 pywebview + Flask 桌面壳，将文件系统职责拆为存储、游戏检测、安全解包、清单、安装适配器、外观和 Nexus 服务。前端使用无构建 ES Modules，共享一个 API 客户端与动作注册表；所有写操作通过事务和安装清单验证后才更新 UI。

**技术栈：** Python 3.10+、Flask 3、pywebview 5、Pillow、pytest、原生 HTML/CSS/JavaScript、PyInstaller。

---

## 文件结构

### 新建

- `backend/storage.py`：原子 JSON、应用数据路径与便携配置存储。
- `backend/domain.py`：Mod 类型、文件记录、清单、冲突和状态数据类型。
- `backend/archive_utils.py`：ZIP 安全检查、解包限制和 Mod 内容识别。
- `backend/manifest_store.py`：每 Mod 一份清单的读写、校验和迁移。
- `backend/mod_service.py`：导入、冲突决策、事务、启停、删除和重扫编排。
- `backend/appearance.py`：主题和背景设置、图片验证与复制。
- `backend/process_utils.py`：Palworld 进程检测、写权限检查、管理员重启。
- `frontend/api.js`：统一 API 请求、错误码和超时。
- `frontend/effects.js`：按钮涟漪、樱花 Canvas 与减少动态效果。
- `frontend/render.js`：安全 DOM 渲染、格式化和通用状态组件。
- `tests/conftest.py`：临时应用数据和伪 Palworld 目录夹具。
- `tests/test_storage.py`
- `tests/test_game_detector.py`
- `tests/test_archive_utils.py`
- `tests/test_manifest_store.py`
- `tests/test_mod_service_pak.py`
- `tests/test_mod_service_ue4ss.py`
- `tests/test_appearance.py`
- `tests/test_nexus_api.py`
- `tests/test_api.py`
- `tests/test_frontend_contract.py`
- `requirements-dev.txt`
- `scripts/prepare_default_background.py`
- `scripts/build_portable.ps1`
- `assets/default-background.webp`

### 修改

- `backend/game_detector.py`：严格 Steam 清单检测与无副作用状态查询。
- `backend/mod_manager.py`：缩减为兼容门面，转发到 `ModService`。
- `backend/nexus_api.py`：类型化匿名 GraphQL、短缓存和显式降级。
- `backend/app.py`：应用工厂、会话令牌、结构化错误和新 API。
- `launcher.py`：便携目录、令牌、随机端口和管理员重启入口。
- `frontend/index.html`：四页应用壳、对话框、外观设置和真实按钮。
- `frontend/styles.css`：三主题、玻璃拟态、背景、响应式与交互状态。
- `frontend/app.js`：状态机、动作注册、各页面业务交互。
- `requirements.txt`：加入 Pillow。
- `build_exe.bat`：调用 F 盘内的可重复构建脚本，不复制到桌面。
- `README.md`：支持范围、使用、构建、测试和限制。

### 保留并接入

- `backend/ue4ss_installer.py`：继续提供 UE4SS 状态与安装，写入前接入进程/权限检查。
- `backend/self_updater.py`：保留更新功能，但适配新的错误契约。
- `backend/mod_config.py` 与 `bundled_mods/`：不在新版主界面暴露无关按钮；不影响已有数据兼容。

---

### 任务 1：测试基线与原子便携存储

**文件：**
- 创建：`requirements-dev.txt`
- 创建：`backend/storage.py`
- 创建：`tests/conftest.py`
- 创建：`tests/test_storage.py`
- 修改：`requirements.txt`

- [ ] **步骤 1：声明运行和开发依赖**

`requirements.txt` 精确包含：

```text
flask>=3.0.0,<4
pywebview>=5.0,<7
Pillow>=10.0,<13
```

`requirements-dev.txt` 精确包含：

```text
-r requirements.txt
pytest>=8.0,<10
```

- [ ] **步骤 2：编写失败的原子存储测试**

```python
# tests/test_storage.py
import json
from backend.storage import JsonStore


def test_json_store_round_trip_is_atomic(tmp_path):
    path = tmp_path / "data" / "config.json"
    store = JsonStore(path)
    store.write({"theme": "aurora-glass"})
    assert store.read({}) == {"theme": "aurora-glass"}
    assert not path.with_suffix(".json.tmp").exists()
    assert json.loads(path.read_text(encoding="utf-8"))["theme"] == "aurora-glass"


def test_json_store_returns_copy_of_default_for_corrupt_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{broken", encoding="utf-8")
    default = {"mods": []}
    value = JsonStore(path).read(default)
    value["mods"].append("x")
    assert default == {"mods": []}
```

- [ ] **步骤 3：运行测试并确认失败**

运行：`py -3 -m pytest tests/test_storage.py -q`

预期：FAIL，`ModuleNotFoundError: No module named 'backend.storage'`。

- [ ] **步骤 4：实现最小原子存储**

```python
# backend/storage.py
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any


class JsonStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def read(self, default: Any) -> Any:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return copy.deepcopy(default)

    def write(self, value: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.path)
```

`tests/conftest.py` 提供 `fake_game_root`：创建 `Palworld.exe`、`Pal/Binaries/Win64/Palworld-Win64-Shipping.exe` 与 `Pal/Content/Paks`。

- [ ] **步骤 5：验证并提交**

运行：`py -3 -m pytest tests/test_storage.py -q`

预期：`2 passed`。

```bash
git add requirements.txt requirements-dev.txt backend/storage.py tests/conftest.py tests/test_storage.py
git commit -m "test: 建立原子便携存储基线"
```

---

### 任务 2：严格 Steam 游戏检测与无副作用路径状态

**文件：**
- 修改：`backend/game_detector.py`
- 创建：`tests/test_game_detector.py`

- [ ] **步骤 1：编写 Steam 清单和严格验证测试**

```python
# tests/test_game_detector.py
from pathlib import Path
from backend.game_detector import find_palworld_installs, validate_game_path


def test_detects_install_from_library_manifest(tmp_path, fake_game_root):
    steam = tmp_path / "Steam"
    library = tmp_path / "Library"
    (steam / "steamapps").mkdir(parents=True)
    (library / "steamapps").mkdir(parents=True)
    (steam / "steamapps/libraryfolders.vdf").write_text(
        f'"libraryfolders" {{ "0" {{ "path" "{library.as_posix()}" }} }}', encoding="utf-8"
    )
    (library / "steamapps/appmanifest_1623730.acf").write_text(
        '"AppState" { "appid" "1623730" "installdir" "Palworld" }', encoding="utf-8"
    )
    target = library / "steamapps/common/Palworld"
    target.parent.mkdir(parents=True)
    fake_game_root.rename(target)
    found = find_palworld_installs(steam_roots=[steam])
    assert [x["path"] for x in found] == [str(target)]


def test_status_query_does_not_create_mod_folders(fake_game_root):
    info = validate_game_path(fake_game_root, create=False)
    assert info["valid"] is True
    assert not (fake_game_root / "Pal/Content/Paks/~mods").exists()


def test_paks_folder_alone_is_not_valid(tmp_path):
    (tmp_path / "Pal/Content/Paks").mkdir(parents=True)
    assert validate_game_path(tmp_path, create=False)["valid"] is False
```

- [ ] **步骤 2：运行目标测试并确认现有实现失败**

运行：`py -3 -m pytest tests/test_game_detector.py -q`

预期：至少一个 FAIL，原因是没有 `steam_roots`/`create` 参数或验证过宽。

- [ ] **步骤 3：实现检测接口**

固定公共接口为 `find_palworld_installs(*, steam_roots=None)`、`validate_game_path(path, *, create=False)`、`ensure_mod_folders(game_root)` 和 `resolve_ue4ss_mods_dir(game_root)`。

实现 VDF/ACF 键值解析、所有库遍历、`installdir` 解析、去重和严格可执行文件验证。`validate_game_path(create=False)` 不得调用 `ensure_mod_folders`，返回值分别保持 `list[dict[str, object]]`、`dict[str, object]`、`dict[str, object]` 和 `Path`。

- [ ] **步骤 4：验证并提交**

运行：`py -3 -m pytest tests/test_game_detector.py tests/test_storage.py -q`

预期：全部 PASS。

```bash
git add backend/game_detector.py tests/test_game_detector.py
git commit -m "feat: 严格检测 Steam 版 Palworld"
```

---

### 任务 3：领域模型与 ZIP 安全解包

**文件：**
- 创建：`backend/domain.py`
- 创建：`backend/archive_utils.py`
- 创建：`tests/test_archive_utils.py`

- [ ] **步骤 1：编写危险 ZIP 和类型识别测试**

```python
# tests/test_archive_utils.py
import io
import zipfile
import pytest
from backend.archive_utils import inspect_and_extract
from backend.domain import ArchivePolicy, ModKind


def make_zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_rejects_parent_traversal(tmp_path):
    archive = tmp_path / "bad.zip"
    archive.write_bytes(make_zip({"../escape.pak": b"x"}))
    with pytest.raises(ValueError, match="不安全路径"):
        inspect_and_extract(archive, tmp_path / "out", ArchivePolicy())


def test_rejects_expansion_over_limit(tmp_path):
    archive = tmp_path / "large.zip"
    archive.write_bytes(make_zip({"mod.pak": b"x" * 33}))
    with pytest.raises(ValueError, match="展开大小"):
        inspect_and_extract(archive, tmp_path / "out", ArchivePolicy(max_total_bytes=32))


def test_detects_logicmods_and_groups_pak_sidecars(tmp_path):
    archive = tmp_path / "logic.zip"
    archive.write_bytes(make_zip({
        "Pal/Content/Paks/LogicMods/Foo.pak": b"p",
        "Pal/Content/Paks/LogicMods/Foo.utoc": b"u",
        "Pal/Content/Paks/LogicMods/Foo.ucas": b"c",
    }))
    result = inspect_and_extract(archive, tmp_path / "out", ArchivePolicy())
    assert result.kind is ModKind.LOGIC
    assert {p.suffix for p in result.groups[0]} == {".pak", ".utoc", ".ucas"}
```

- [ ] **步骤 2：运行并确认失败**

运行：`py -3 -m pytest tests/test_archive_utils.py -q`

预期：FAIL，缺少 `backend.archive_utils` 或 `backend.domain`。

- [ ] **步骤 3：实现固定数据类型和安全策略**

```python
# backend/domain.py
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

class ModKind(StrEnum):
    PAK = "pak"
    LOGIC = "logicpak"
    UE4SS = "ue4ss"

@dataclass(frozen=True)
class ArchivePolicy:
    max_files: int = 5000
    max_single_bytes: int = 2 * 1024**3
    max_total_bytes: int = 8 * 1024**3

@dataclass
class InspectedMod:
    kind: ModKind
    display_name: str
    content_root: Path
    groups: list[list[Path]] = field(default_factory=list)
```

`archive_utils.py` 必须逐条检查规范化路径、Windows 盘符、ZIP external attributes 中的符号链接、文件数和声明展开大小；使用流式复制并再次统计实际字节数。类型优先级为 UE4SS 标准结构、明确 LogicMods 路径、普通 PAK；普通文件夹不得被猜成可安装 Mod。

- [ ] **步骤 4：验证并提交**

运行：`py -3 -m pytest tests/test_archive_utils.py -q`

预期：`3 passed`。

```bash
git add backend/domain.py backend/archive_utils.py tests/test_archive_utils.py
git commit -m "feat: 添加安全解包与 Mod 类型识别"
```

---

### 任务 4：逐 Mod 清单、哈希与迁移

**文件：**
- 创建：`backend/manifest_store.py`
- 创建：`tests/test_manifest_store.py`
- 修改：`backend/mod_manager.py`

- [ ] **步骤 1：编写清单持久化与完整性测试**

```python
# tests/test_manifest_store.py
from backend.domain import ModKind
from backend.manifest_store import ManifestStore


def test_manifest_detects_external_change(tmp_path):
    owned = tmp_path / "game/~mods/Foo.pak"
    owned.parent.mkdir(parents=True)
    owned.write_bytes(b"original")
    store = ManifestStore(tmp_path / "data/manifests")
    manifest = store.create("Foo", ModKind.PAK, owned.parent, [owned], "Foo.zip")
    assert store.audit(manifest).status == "enabled"
    owned.write_bytes(b"changed")
    assert store.audit(manifest).status == "modified"


def test_manifest_detects_missing_file(tmp_path):
    owned = tmp_path / "Foo.pak"
    owned.write_bytes(b"x")
    store = ManifestStore(tmp_path / "manifests")
    manifest = store.create("Foo", ModKind.PAK, tmp_path, [owned], "Foo.pak")
    owned.unlink()
    assert store.audit(manifest).status == "missing"
```

- [ ] **步骤 2：运行并确认失败**

运行：`py -3 -m pytest tests/test_manifest_store.py -q`

预期：FAIL，缺少 `ManifestStore`。

- [ ] **步骤 3：实现清单接口**

固定接口为 `ManifestStore(root)`、`create(name, kind, install_root, paths, source_name, nexus_id=None)`、`get(mod_id)`、`list()`、`save(manifest)`、`delete(mod_id)`、`audit(manifest)` 和 `migrate_legacy_registry(registry_path)`。

相对路径必须相对于 `install_root`，每个文件保存 SHA-256 和大小。迁移旧 `mods_registry.json` 时只导入仍位于已知 Mod 根目录的条目，不能信任任意绝对路径。`get` 返回单个清单或抛出 `KeyError`，`list` 返回按安装时间排序的清单，`audit` 返回含 `enabled|disabled|modified|missing|conflict` 状态的数据对象。

- [ ] **步骤 4：将 `mod_manager.py` 改为兼容门面**

保留现有路由调用需要的函数名，但内部通过单例/工厂取得新服务；删除旧的静默随机改名和仅写布尔状态逻辑。此步骤只接线，不在门面重复文件操作。

- [ ] **步骤 5：验证并提交**

运行：`py -3 -m pytest tests/test_manifest_store.py tests/test_storage.py -q`

预期：全部 PASS。

```bash
git add backend/domain.py backend/manifest_store.py backend/mod_manager.py tests/test_manifest_store.py
git commit -m "feat: 使用逐 Mod 安装清单追踪文件"
```

---

### 任务 5：PAK 与 LogicMods 事务安装和真实启停

**文件：**
- 创建：`backend/process_utils.py`
- 创建：`backend/mod_service.py`
- 创建：`tests/test_mod_service_pak.py`
- 修改：`tests/conftest.py`

- [ ] **步骤 1：编写安装、整组禁用和回滚测试**

先在 `tests/conftest.py` 增加可复用夹具：

```python
import zipfile
import pytest
from backend.mod_service import ModService

@pytest.fixture
def pak_zip(tmp_path):
    path = tmp_path / "Foo.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Foo.pak", b"pak")
        zf.writestr("Foo.utoc", b"utoc")
        zf.writestr("Foo.ucas", b"ucas")
    return path

@pytest.fixture
def service(tmp_path, fake_game_root):
    return ModService(
        game_root=fake_game_root,
        data_dir=tmp_path / "data",
        game_running=lambda: False,
    )
```

```python
# tests/test_mod_service_pak.py
import pytest
from backend.domain import ModKind
from backend.mod_service import ModService


def test_pak_group_moves_out_and_back_atomically(service: ModService, fake_game_root, pak_zip):
    installed = service.install(pak_zip, decision="cancel")
    assert installed.kind is ModKind.PAK
    assert {p.suffix for p in installed.live_paths()} == {".pak", ".utoc", ".ucas"}
    disabled = service.set_enabled(installed.id, False)
    assert disabled.status == "disabled"
    assert all(not p.exists() for p in installed.live_paths())
    assert len(list((service.disabled_root / installed.id).iterdir())) == 3
    enabled = service.set_enabled(installed.id, True)
    assert enabled.status == "enabled"
    assert all(p.exists() for p in enabled.live_paths())


def test_install_rolls_back_when_manifest_write_fails(service, pak_zip, monkeypatch):
    monkeypatch.setattr(service.manifests, "save", lambda _: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        service.install(pak_zip, decision="cancel")
    assert not list(service.paths["tilde_mods"].glob("Foo.*"))
```

另加测试：游戏进程运行时抛出 `GameRunningError`；同名不同哈希返回 `ModConflictError`；`replace` 备份旧受管文件后安装，`keep_both` 使用明确的用户可见名称。

- [ ] **步骤 2：运行并确认失败**

运行：`py -3 -m pytest tests/test_mod_service_pak.py -q`

预期：FAIL，缺少 `ModService`。

- [ ] **步骤 3：实现事务边界**

构造器固定为 `ModService(game_root, data_dir, game_running=is_palworld_running)`；公共接口为 `install(source, *, preferred_kind=None, display_name=None, nexus_id=None, decision="cancel")`、`set_enabled(mod_id, enabled)`、`delete(mod_id, *, force_modified=False)`、`rescan()` 和 `list_mods()`。

安装暂存位于 `data/staging/<transaction-id>`。事务记录每个已移动路径和配置备份；异常时按逆序恢复。PAK/LogicMods 禁用必须移动清单内完整文件组到 `disabled/<mod-id>`，成功后再保存状态。所有返回值都是清单审计后的序列化对象，不返回未经验证的请求值。

- [ ] **步骤 4：实现进程和权限检查**

`process_utils.py` 使用 Windows Toolhelp snapshot 或 `tasklist /FO CSV` 检测 `Palworld.exe`、`Palworld-Win64-Shipping.exe`。提供 `is_palworld_running() -> bool`、`is_directory_writable(path) -> bool` 和 `restart_as_admin(argv) -> None`。`restart_as_admin` 仅在 Windows 调用带 `runas` verb 的 `ShellExecuteW`，调用失败时抛出包含 Win32 错误码的异常。

非 Windows 测试环境返回可注入结果，不在导入模块时执行系统调用。

- [ ] **步骤 5：验证并提交**

运行：`py -3 -m pytest tests/test_mod_service_pak.py -q`

预期：全部 PASS。

```bash
git add backend/process_utils.py backend/mod_service.py tests/test_mod_service_pak.py
git commit -m "feat: 事务化管理 PAK 与 LogicMods"
```

---

### 任务 6：UE4SS 保留式配置与真实开关

**文件：**
- 修改：`backend/mod_service.py`
- 修改：`backend/game_detector.py`
- 创建：`tests/test_mod_service_ue4ss.py`

- [ ] **步骤 1：编写两种布局和 `enabled.txt` 测试**

```python
# tests/test_mod_service_ue4ss.py
import zipfile
import pytest
from backend.game_detector import resolve_ue4ss_mods_dir

@pytest.fixture
def ue4ss_zip(tmp_path):
    path = tmp_path / "LuaExample.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("LuaExample/enabled.txt", b"")
        zf.writestr("LuaExample/Scripts/main.lua", b"print('ok')")
    return path


def test_ue4ss_disable_preserves_comments_and_other_entries(service, ue4ss_zip):
    mods_txt = service.paths["ue4ss_mods"] / "mods.txt"
    mods_txt.parent.mkdir(parents=True, exist_ok=True)
    mods_txt.write_text("; keep this\nOtherMod : 1\n", encoding="utf-8")
    mod = service.install(ue4ss_zip, decision="cancel")
    assert not (mod.install_root / mod.folder_name / "enabled.txt").exists()
    service.set_enabled(mod.id, False)
    text = mods_txt.read_text(encoding="utf-8")
    assert "; keep this" in text
    assert "OtherMod : 1" in text
    assert f"{mod.folder_name} : 0" in text


def test_detects_nested_ue4ss_layout(fake_game_root):
    nested = fake_game_root / "Pal/Binaries/Win64/ue4ss/Mods"
    nested.mkdir(parents=True)
    assert resolve_ue4ss_mods_dir(fake_game_root) == nested
```

- [ ] **步骤 2：运行并确认失败**

运行：`py -3 -m pytest tests/test_mod_service_ue4ss.py -q`

预期：至少一个 FAIL，现有逻辑会整体重写 `mods.txt` 或保留 `enabled.txt`。

- [ ] **步骤 3：实现保留式 `mods.txt` 编辑器**

逐行解析 `name : 0|1`，只替换目标 Mod 行；保留注释、空行、未知行和顺序。目标不存在时在文件末尾添加。安装时将 `enabled.txt` 移至 `disabled/<mod-id>/metadata/enabled.txt` 并记录原始存在状态，统一由 `mods.txt` 控制。

- [ ] **步骤 4：接入 UE4SS 安装器安全前置检查**

`ue4ss_installer.py` 的在线和本地安装入口在写 Win64 前调用游戏进程和写权限检查；下载仍只使用官方 GitHub Release。

- [ ] **步骤 5：验证并提交**

运行：`py -3 -m pytest tests/test_mod_service_ue4ss.py tests/test_mod_service_pak.py -q`

预期：全部 PASS。

```bash
git add backend/game_detector.py backend/mod_service.py backend/ue4ss_installer.py tests/test_mod_service_ue4ss.py
git commit -m "feat: 正确管理 UE4SS 模组开关"
```

---

### 任务 7：应用工厂、安全本机 API 与结构化冲突

**文件：**
- 修改：`backend/app.py`
- 修改：`launcher.py`
- 修改：`backend/mod_manager.py`
- 创建：`tests/test_api.py`
- 修改：`tests/conftest.py`

- [ ] **步骤 1：编写 API 令牌、状态和冲突测试**

在 `tests/conftest.py` 增加：

```python
import io
import pytest
from backend.app import create_app

@pytest.fixture
def app(tmp_path, fake_game_root, monkeypatch):
    monkeypatch.setenv("PALMOD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PALMOD_GAME_PATH", str(fake_game_root))
    return create_app(
        root=tmp_path,
        data_dir=tmp_path / "data",
        session_token="test-token",
        testing=True,
    )

@pytest.fixture
def auth_client(app):
    client = app.test_client()
    client.get("/?token=test-token", follow_redirects=True)
    return client

@pytest.fixture
def conflict_zip(pak_zip):
    return (io.BytesIO(pak_zip.read_bytes()), pak_zip.name)
```

```python
# tests/test_api.py

def test_api_rejects_missing_session_cookie(app):
    client = app.test_client()
    response = client.get("/api/mods")
    assert response.status_code == 403
    assert response.json["error_code"] == "invalid_session"


def test_index_sets_session_cookie(app):
    client = app.test_client()
    response = client.get("/?token=test-token")
    assert response.status_code == 200
    assert "paldeck_session=" in response.headers["Set-Cookie"]


def test_import_conflict_returns_machine_readable_409(auth_client, conflict_zip):
    response = auth_client.post("/api/mods/import", data={"file": conflict_zip})
    assert response.status_code == 409
    assert response.json["error_code"] == "mod_conflict"
    assert set(response.json["details"]["choices"]) == {"replace", "keep_both", "cancel"}
```

- [ ] **步骤 2：运行并确认失败**

运行：`py -3 -m pytest tests/test_api.py -q`

预期：FAIL，现有全局应用没有会话验证或结构化错误码。

- [ ] **步骤 3：实现应用工厂**

固定应用工厂签名为 `create_app(*, root=None, data_dir=None, session_token=None, testing=False) -> Flask`。

`/` 仅在查询 token 使用 `secrets.compare_digest` 成功后设置 HttpOnly、SameSite=Strict 会话 Cookie，并重定向到无 token URL。`/api/*` 除健康检查外验证 Cookie。所有异常映射为 `{ok:false,error,error_code,details}`。测试模式仍要求显式提供测试令牌，不能暗中绕过会话校验。

- [ ] **步骤 4：迁移路由到服务层**

路由只做输入转换：

- `GET /api/mods`
- `POST /api/mods/import`
- `POST /api/mods/<id>/toggle`
- `DELETE /api/mods/<id>?force_modified=true`
- `POST /api/mods/resync`
- `GET/POST /api/game/*`
- `POST /api/system/restart-admin`

冲突使用 HTTP 409，游戏运行使用 423，无权限使用 403，输入错误使用 400。

- [ ] **步骤 5：验证并提交**

运行：`py -3 -m pytest tests/test_api.py tests/test_mod_service_pak.py tests/test_mod_service_ue4ss.py -q`

预期：全部 PASS。

```bash
git add backend/app.py backend/mod_manager.py launcher.py tests/test_api.py
git commit -m "feat: 加固桌面本机 API 契约"
```

---

### 任务 8：默认背景处理与外观服务

**文件：**
- 创建：`scripts/prepare_default_background.py`
- 创建：`assets/default-background.webp`
- 创建：`backend/appearance.py`
- 创建：`tests/test_appearance.py`
- 修改：`backend/app.py`

- [ ] **步骤 1：生成不依赖 C 盘的默认背景资产**

脚本固定接受输入和输出参数：

```python
# scripts/prepare_default_background.py
from __future__ import annotations

import argparse
from pathlib import Path
from PIL import Image


def prepare(source: Path, target: Path) -> None:
    with Image.open(source) as image:
        image = image.convert("RGB")
        crop_top = max(64, round(image.height * 0.055))
        image = image.crop((0, crop_top, image.width, image.height))
        target.parent.mkdir(parents=True, exist_ok=True)
        image.save(target, "WEBP", quality=90, method=6)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    prepare(args.source, args.target)


if __name__ == "__main__":
    main()
```

运行：

```powershell
py -3 scripts/prepare_default_background.py `
  "C:\Users\yyx\Pictures\Screenshots\屏幕截图 2026-07-11 165046.png" `
  "assets\default-background.webp"
```

验证：`assets/default-background.webp` 存在，且图片顶部不再包含监控文字。

- [ ] **步骤 2：编写图片格式伪装和设置持久化测试**

```python
# tests/test_appearance.py
from PIL import Image
import pytest
from backend.appearance import AppearanceService


def test_rejects_extension_disguised_as_image(tmp_path):
    fake = tmp_path / "wallpaper.png"
    fake.write_text("not an image", encoding="utf-8")
    with pytest.raises(ValueError, match="有效图片"):
        AppearanceService(tmp_path / "data", tmp_path / "default.webp").set_background(fake)


def test_copies_background_and_never_deletes_source(tmp_path):
    source = tmp_path / "user.png"
    Image.new("RGB", (1600, 900), "blue").save(source)
    service = AppearanceService(tmp_path / "data", tmp_path / "default.webp")
    saved = service.set_background(source)
    assert source.exists()
    assert saved.exists()
    assert saved.parent == tmp_path / "data/backgrounds"
```

- [ ] **步骤 3：运行并确认失败**

运行：`py -3 -m pytest tests/test_appearance.py -q`

预期：FAIL，缺少 `AppearanceService`。

- [ ] **步骤 4：实现外观服务和 API**

主题仅允许 `aurora-glass`、`ivory-sakura`、`starlit-night`。背景只允许 Pillow 实际识别的 PNG/JPEG/WEBP，最大 25 MiB、最大 12000×12000。保存使用 UUID 文件名。

API：

- `GET /api/appearance`
- `POST /api/appearance`
- `POST /api/appearance/background`
- `DELETE /api/appearance/background`
- `GET /api/appearance/background/current`

设置字段范围：遮罩 `0..0.85`，模糊 `0..24`，位置为九宫格枚举，樱花为 `off|low|medium|high`。

- [ ] **步骤 5：验证并提交**

运行：`py -3 -m pytest tests/test_appearance.py tests/test_api.py -q`

预期：全部 PASS。

```bash
git add scripts/prepare_default_background.py assets/default-background.webp backend/appearance.py backend/app.py tests/test_appearance.py
git commit -m "feat: 添加可持久化主题与背景服务"
```

---

### 任务 9：三主题应用壳、背景和交互特效

**文件：**
- 修改：`frontend/index.html`
- 修改：`frontend/styles.css`
- 创建：`frontend/api.js`
- 创建：`frontend/effects.js`
- 创建：`frontend/render.js`
- 修改：`frontend/app.js`
- 创建：`tests/test_frontend_contract.py`

- [ ] **步骤 1：先写可见控件—动作契约测试**

```python
# tests/test_frontend_contract.py
from html.parser import HTMLParser
from pathlib import Path
import re

class ActionParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.actions = set()
    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag in {"button", "input", "select", "label"} and values.get("data-action"):
            self.actions.add(values["data-action"])


def test_every_visible_action_has_handler():
    parser = ActionParser()
    parser.feed(Path("frontend/index.html").read_text(encoding="utf-8"))
    source = Path("frontend/app.js").read_text(encoding="utf-8")
    match = re.search(r"export const ACTION_HANDLERS = Object\.freeze\(\{(.*?)\}\);", source, re.S)
    assert match, "ACTION_HANDLERS registry is required"
    registered = set(re.findall(r"\n\s*([a-zA-Z0-9_-]+)\s*:", match.group(1)))
    assert parser.actions == registered
```

- [ ] **步骤 2：运行并确认失败**

运行：`py -3 -m pytest tests/test_frontend_contract.py -q`

预期：FAIL，旧 HTML 没有统一 `data-action` 契约。

- [ ] **步骤 3：重建单一应用壳**

`index.html` 只保留四页：我的模组、导入安装、N 网热门、设置与外观。所有交互控件使用唯一 `data-action`。加入冲突对话框、删除确认对话框、Toast、全局忙碌层、背景层和 `<canvas id="petalCanvas">`。

不加载 Google Fonts；使用本机 `Segoe UI Variable`、`Microsoft YaHei UI` 和系统字体。

- [ ] **步骤 4：实现主题和背景 CSS**

`:root[data-theme="aurora-glass"]`、`:root[data-theme="ivory-sakura"]`、`:root[data-theme="starlit-night"]` 分别定义主题变量。必须实现焦点可见、按钮 hover/active/loading/disabled、开关回滚动画、玻璃面板、Nexus 图片占位和 960×640 最小窗口布局。

背景使用 `--background-url`、`--background-mask`、`--background-blur`、`--background-position`，内容层保持可读对比度。

- [ ] **步骤 5：实现按钮与樱花效果**

`effects.js` 导出：

```javascript
export function installRipple(root = document) {}
export function createPetalEffect(canvas, { density, reducedMotion }) {}
export function updatePetalEffect(controller, settings) {}
```

Canvas 使用一个 RAF 循环；`visibilitychange` 时暂停；`prefers-reduced-motion: reduce` 时默认不启动。高密度粒子上限 80。

- [ ] **步骤 6：实现前端动作注册表和外观保存**

`app.js` 必须包含：

```javascript
export const ACTION_HANDLERS = Object.freeze({
  refreshMods: handleRefreshMods,
  openModsFolder: handleOpenModsFolder,
  chooseModFile: handleChooseModFile,
  importMod: handleImportMod,
  searchNexus: handleSearchNexus,
  refreshNexus: handleRefreshNexus,
  autoDetectGame: handleAutoDetectGame,
  saveGamePath: handleSaveGamePath,
  repairFolders: handleRepairFolders,
  chooseBackground: handleChooseBackground,
  resetBackground: handleResetBackground,
  saveAppearance: handleSaveAppearance,
  installUe4ss: handleInstallUe4ss,
  checkUpdate: handleCheckUpdate,
  restartAdmin: handleRestartAdmin,
});
```

动态 Mod/Nexus 卡片操作通过受控事件委托单独处理。背景上传成功后才切换 CSS URL；失败保持原背景。

- [ ] **步骤 7：验证并提交**

运行：

```powershell
py -3 -m pytest tests/test_frontend_contract.py tests/test_appearance.py -q
node --check frontend/api.js
node --check frontend/effects.js
node --check frontend/render.js
node --check frontend/app.js
```

预期：pytest 全部 PASS，四个 `node --check` 均退出码 0。

```bash
git add frontend/index.html frontend/styles.css frontend/api.js frontend/effects.js frontend/render.js frontend/app.js tests/test_frontend_contract.py
git commit -m "feat: 重做三主题桌面界面与樱花效果"
```

---

### 任务 10：本地 Mod 页面、导入冲突和失败回滚 UI

**文件：**
- 修改：`frontend/app.js`
- 修改：`frontend/render.js`
- 修改：`frontend/styles.css`
- 修改：`tests/test_frontend_contract.py`

- [ ] **步骤 1：扩展前端契约测试**

测试动态委托动作集合必须精确包含：

```python
expected = {
    "toggle-mod", "open-mod-folder", "delete-mod",
    "resolve-conflict", "force-delete-mod", "copy-nexus-id", "open-nexus",
}
assert expected <= set(re.findall(r'case "([a-z-]+)"', source))
```

- [ ] **步骤 2：运行并确认失败**

运行：`py -3 -m pytest tests/test_frontend_contract.py -q`

预期：FAIL，尚未包含完整动态动作。

- [ ] **步骤 3：实现本地列表真实状态呈现**

每张卡显示 `enabled|disabled|modified|missing|conflict`，只有 enabled/disabled 可直接切换。修改/缺失状态显示修复提示。切换时先锁定控件；API 成功后用返回清单渲染，失败时恢复旧值和旧卡片。

- [ ] **步骤 4：实现导入和冲突流程**

上传时显示文件名、识别类型和阶段进度。HTTP 409 打开三选一对话框：覆盖、保留两份、取消；选择后使用同一上传暂存令牌重试，避免用户再次选文件。若后端令牌过期，明确要求重新选择。

- [ ] **步骤 5：实现修改文件删除确认**

普通删除二次确认；后端返回 `modified_files` 冲突时展示文件列表，再由用户选择强制删除或取消。不得默认强制。

- [ ] **步骤 6：验证并提交**

运行：

```powershell
py -3 -m pytest tests/test_frontend_contract.py tests/test_api.py -q
node --check frontend/app.js
node --check frontend/render.js
```

预期：全部 PASS。

```bash
git add frontend/app.js frontend/render.js frontend/styles.css tests/test_frontend_contract.py
git commit -m "feat: 接通 Mod 导入启停与冲突交互"
```

---

### 任务 11：Nexus 匿名只读目录、短缓存和前端卡片

**文件：**
- 修改：`backend/nexus_api.py`
- 修改：`backend/app.py`
- 修改：`frontend/app.js`
- 修改：`frontend/render.js`
- 创建：`tests/test_nexus_api.py`
- 修改：`tests/test_api.py`

- [ ] **步骤 1：编写实时、字段缺失和缓存降级测试**

```python
# tests/test_nexus_api.py
from collections import deque
from datetime import datetime, timezone
import pytest
from backend.nexus_api import NexusCatalog


class FakeTransport:
    def __init__(self):
        self.responses = deque()

    def queue(self, value):
        self.responses.append(value)

    def queue_success_for_mod(self, mod_id):
        self.queue({"data": {"mods": {"nodes": [{"modId": mod_id, "name": "Cached"}]}}})

    def queue_timeout(self):
        self.queue(TimeoutError("timed out"))

    def __call__(self, query, variables):
        value = self.responses.popleft()
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def fake_transport():
    return FakeTransport()


def test_normalizes_public_mod_fields(tmp_path, fake_transport):
    fake_transport.queue({"data": {"mods": {"nodes": [{
        "modId": 336, "name": "Example", "summary": "Intro",
        "pictureUrl": "https://staticdelivery.nexusmods.com/x.jpg",
        "downloads": 123, "endorsements": 7,
    }]}}})
    result = NexusCatalog(tmp_path, transport=fake_transport).popular("downloads")
    assert result["source"] == "live"
    assert result["items"][0]["nexus_id"] == 336
    assert result["items"][0]["url"].endswith("/mods/336")


def test_live_failure_returns_timestamped_stale_cache(tmp_path, fake_transport):
    catalog = NexusCatalog(tmp_path, transport=fake_transport)
    fake_transport.queue_success_for_mod(336)
    catalog.popular("downloads")
    fake_transport.queue_timeout()
    result = catalog.popular("downloads", force=True)
    assert result["source"] == "cache"
    assert result["stale"] is True
    datetime.fromisoformat(result["fetched_at"]).astimezone(timezone.utc)
```

再加：可选字段缺失不会崩溃；恶意 `javascript:` 图片 URL 被清空；GraphQL `errors` 不被吞成“未找到”；纯数字查询调用单 Mod 查询。

- [ ] **步骤 2：运行并确认失败**

运行：`py -3 -m pytest tests/test_nexus_api.py -q`

预期：FAIL，现有 API 返回裸列表且无缓存状态。

- [ ] **步骤 3：实现 `NexusCatalog`**

固定响应：

```python
{
  "items": list[dict],
  "source": "live" | "cache",
  "stale": bool,
  "fetched_at": str,
  "warning": str | None,
}
```

热门排序仅允许 `downloads|endorsements|latest`；关键词和尾号查询使用 GraphQL 变量，不将用户输入插入查询文本。元数据缓存 TTL 600 秒；`force=True` 始终尝试网络。只允许 `https` 图片和详情 URL。

- [ ] **步骤 4：接入 API 和 UI**

API 路由返回上述结构。UI 明确显示“实时数据”或“缓存数据 · 时间”，提供总下载热门、推荐热门、最新、搜索和刷新。图片失败替换为本地渐变占位，不添加下载按钮。

“打开 N 网”通过 pywebview/系统默认浏览器打开经过验证的 `https://www.nexusmods.com/palworld/mods/<id>`；复制尾号使用 Clipboard API，失败时显示可手动复制文本。

- [ ] **步骤 5：验证并提交**

运行：

```powershell
py -3 -m pytest tests/test_nexus_api.py tests/test_api.py tests/test_frontend_contract.py -q
node --check frontend/app.js
```

预期：全部 PASS。

```bash
git add backend/nexus_api.py backend/app.py frontend/app.js frontend/render.js tests/test_nexus_api.py tests/test_api.py
git commit -m "feat: 完成 Nexus 匿名只读热门目录"
```

---

### 任务 12：打包、完整验证、桌面副本和 GitHub 分支

**文件：**
- 创建：`scripts/build_portable.ps1`
- 修改：`build_exe.bat`
- 修改：`README.md`
- 修改：`.gitignore`
- 创建：`docs/verification/2026-07-11-paldeck-release.md`

- [ ] **步骤 1：编写可重复便携构建脚本**

`scripts/build_portable.ps1`：

1. 将工作目录固定到仓库根。
2. 创建项目内 `.venv-build`，禁止使用 C 盘临时目录：设置 `TMP`、`TEMP`、`PYTHONPYCACHEPREFIX` 到项目内 `.build-tmp`。
3. 安装 `requirements.txt`、PyInstaller 和 Pillow。
4. 运行完整测试。
5. PyInstaller 打包 `launcher.py`，加入 `frontend`、`assets`、`bundled_mods`。
6. 创建 `dist/PalDeck-portable/PalDeck.exe` 与 `README.txt`。
7. 压缩为 `dist/PalDeck-v<version>-windows-portable.zip`。
8. 输出 SHA-256。

`build_exe.bat` 只调用：

```bat
@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build_portable.ps1"
```

- [ ] **步骤 2：更新 README**

README 明确：Windows Steam 客户端范围、三类 Mod、Nexus 只读、三套主题、背景设置、运行游戏时禁止写操作、Steam Workshop/Xbox/服务器不支持、测试和构建命令。

- [ ] **步骤 3：运行完整自动化验证**

```powershell
py -3 -m pytest -q
node --check frontend/api.js
node --check frontend/effects.js
node --check frontend/render.js
node --check frontend/app.js
git diff --check
```

预期：pytest 全部 PASS；所有语法检查退出码 0；`git diff --check` 无输出。

- [ ] **步骤 4：运行真实 Nexus 契约检查**

在不记录响应正文和用户信息的前提下运行一次热门查询：

```powershell
py -3 -c "from backend.nexus_api import NexusCatalog; from backend.mod_manager import DATA_DIR; r=NexusCatalog(DATA_DIR/'cache'/'nexus').popular('downloads', force=True); print(r['source'], len(r['items']), r['items'][0]['nexus_id'] if r['items'] else 'empty')"
```

预期：`live <正整数> <有效尾号>`。若 Nexus 当时不可达，不能声称实时功能验证通过；记录错误并在网络恢复后重试。

- [ ] **步骤 5：构建和桌面冒烟测试**

运行：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_portable.ps1`

预期：便携 ZIP 和 SHA-256 均生成在 F 盘 `dist`。解压到 F 盘临时目录，启动 `PalDeck.exe`，验证窗口打开、健康检查、三主题切换、默认背景、樱花开关和无游戏路径提示。关闭后确认进程退出。

- [ ] **步骤 6：记录验证证据**

`docs/verification/2026-07-11-paldeck-release.md` 写入：测试命令、退出码、测试数量、Nexus 实时响应摘要、EXE 启动结果、ZIP 路径、大小、SHA-256、已知限制。不得写“应该通过”或未执行命令。

- [ ] **步骤 7：请求代码审查并修复阻塞项**

对完整 diff 进行正确性、安全、测试和 UI 四个角度审查。只修复证据充分且属于规格范围的问题；修复后重新运行步骤 3 至步骤 5。

- [ ] **步骤 8：提交发布候选**

```bash
git add .gitignore README.md build_exe.bat scripts/build_portable.ps1 docs/verification/2026-07-11-paldeck-release.md
git commit -m "build: 交付 PalDeck Windows 便携版"
git status --short
git log --oneline origin/main..HEAD
```

预期：工作树干净，提交历史只位于 `feature/paldeck-redesign`。

- [ ] **步骤 9：复制唯一 C 盘交付副本**

```powershell
Copy-Item -Force "F:\Grok_Workspace\01_Projects\palworld-mod-manager\dist\PalDeck-v*-windows-portable.zip" "C:\Users\yyx\Desktop\"
```

验证桌面 ZIP 的 SHA-256 与 F 盘产物一致。不得把源码、虚拟环境或构建中间文件复制到 C 盘。

- [ ] **步骤 10：推送新分支，不修改远端 main**

```bash
git fetch origin
git merge-base --is-ancestor origin/main HEAD
git push -u origin feature/paldeck-redesign
git ls-remote --heads origin feature/paldeck-redesign
git ls-remote --heads origin main
```

预期：远端存在 `refs/heads/feature/paldeck-redesign`；`origin/main` 未被本任务直接推送或强制更新。
