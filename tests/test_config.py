from pathlib import Path
import textwrap

import pytest

from seatbot.config import load_config, ConfigError


def test_load_minimal_config(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(textwrap.dedent("""
        library:
          room_id: 11692
          room_name: "2号楼图书馆-3F"
          time_unit_minutes: 30
          open_time: "08:00"
          close_time: "22:00"
          max_reserve_hours: 2.0
        accounts:
          - id: zhangsan
            phone: "13800000001"
            password: "secret"
            seat_num: "084"
            slots: full
        runtime:
          stagger_seconds: [0, 3]
          relogin_on_401: true
          random_ua: true
          log_dir: ./logs
          db_path: ./seatbot.db
          web_host: 0.0.0.0
          web_port: 8080
    """).strip())

    cfg = load_config(cfg_file)
    assert cfg.library.room_id == 11692
    assert cfg.accounts[0].id == "zhangsan"
    assert cfg.accounts[0].slots == "full"
    assert cfg.runtime.web_port == 8080


def test_load_missing_library_raises(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("accounts: []\nruntime: {}\n")
    with pytest.raises(ConfigError):
        load_config(cfg_file)


def test_account_id_must_be_unique(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(textwrap.dedent("""
        library:
          room_id: 1
          room_name: "x"
          time_unit_minutes: 30
          open_time: "08:00"
          close_time: "22:00"
          max_reserve_hours: 2.0
        accounts:
          - {id: a, phone: "1", password: "p", seat_num: "001", slots: full}
          - {id: a, phone: "2", password: "p", seat_num: "002", slots: full}
        runtime: {stagger_seconds: [0,0], relogin_on_401: true, random_ua: true,
                  log_dir: "./logs", db_path: "./x.db", web_host: "0.0.0.0", web_port: 1}
    """).strip())
    with pytest.raises(ConfigError, match="unique"):
        load_config(cfg_file)
