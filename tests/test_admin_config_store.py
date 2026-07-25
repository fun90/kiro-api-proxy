"""config_store 可写配置层单元测试。"""

from __future__ import annotations

import json
from pathlib import Path

from kiro_api_proxy.admin.config_store import ConfigStore


def test_defaults_fall_back_to_env(tmp_path: Path):
    """未写入任何覆盖时，读取回退到 .env 派生的默认值。"""
    store = ConfigStore(tmp_path / "config.json")
    cfg = store.get()
    assert cfg.api_host == "127.0.0.1"
    assert cfg.api_port == 3458


def test_update_persists_and_overrides_env(tmp_path: Path):
    """写入的字段覆盖默认值，并原子落盘。"""
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    cfg = store.update(api_port=9000, api_key="secret-key")
    assert cfg.api_port == 9000
    assert cfg.api_key == "secret-key"

    # 落盘内容只含显式写入的键。
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved == {"api_port": 9000, "api_key": "secret-key"}


def test_reload_reads_existing_overrides(tmp_path: Path):
    """新实例应从已有 config.json 读回覆盖值。"""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"api_port": 8888}), encoding="utf-8")
    store = ConfigStore(path)
    assert store.get().api_port == 8888


def test_update_none_restores_default(tmp_path: Path):
    """传入 None 删除覆盖，读取恢复为默认值。"""
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    store.update(api_port=9000)
    assert store.get().api_port == 9000
    store.update(api_port=None)
    assert store.get().api_port == 3458
    # 落盘不再保留该键。
    assert "api_port" not in json.loads(path.read_text(encoding="utf-8"))


def test_ignores_unknown_keys_on_load(tmp_path: Path):
    """加载时忽略未知字段，不污染有效配置。"""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"api_port": 7000, "bogus": "x"}), encoding="utf-8"
    )
    store = ConfigStore(path)
    cfg = store.get()
    assert cfg.api_port == 7000


def test_corrupt_file_falls_back(tmp_path: Path):
    """损坏的 JSON 不应抛出，回退到默认值。"""
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    store = ConfigStore(path)
    assert store.get().api_port == 3458
