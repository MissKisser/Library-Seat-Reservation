"""防回归门禁脚本自身的反向测试：确认每项检查真的能拦住对应缺陷。

全部用例在 tmp_path 隔离目录内构造样本，不触碰仓库源码、生产库与网络。
样本中的敏感形态一律由片段拼接生成，避免测试文件自身触发门禁。
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_GUARD_PATH = Path(__file__).resolve().parents[1] / "scripts" / "guard" / "guard.py"

# 样本敏感形态由片段拼接，避免本文件被门禁自身的标记/手机号检查命中。
_REDACTION_SAMPLE = "*" * 3 + "REDACTED" + "*" * 3
_PHONE_SAMPLE = "".join(("138", "1234", "5678"))


def _load_guard():
    """按文件路径加载门禁脚本模块。

    返回值
    ------
    门禁脚本模块对象。
    """
    spec = importlib.util.spec_from_file_location("seatbot_guard", _GUARD_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


def _init_git_repo(root: Path, files: dict[str, str]) -> None:
    """在指定目录初始化本地 git 仓库，写入样本文件后提交一次。

    参数
    ----
    root: 仓库目录。
    files: 相对路径到文本内容的映射。
    """
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "guard@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "guard"], cwd=root, check=True)
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=guard@example.com", "-c", "user.name=guard", "commit", "-qm", "init"],
        cwd=root,
        check=True,
    )


def test_placeholder_prefix_is_exempt():
    """占位前缀手机号不应被判为凭证。"""
    assert guard._is_placeholder_phone("13800000000") is True
    assert guard._is_placeholder_phone("13900000000") is True
    # 负例用运行时拼接构造：源码里不保留完整号码字面量，
    # 否则门禁扫描本仓库时会把这份测试文件自身判为违规。
    non_placeholder = "1" + "3300000000"
    assert guard._is_placeholder_phone(non_placeholder) is False


def test_phone_pattern_respects_digit_boundary():
    """手机号正则必须加数字边界，避免命中更长的数字串。"""
    assert guard.PHONE_PATTERN.search("13800000000") is not None
    assert guard.PHONE_PATTERN.search("113800000000") is None
    assert guard.PHONE_PATTERN.search("138000000000") is None


def test_commit_hash_lines_are_stripped():
    """提交对象头部的哈希行必须剔除，否则前 11 位会被误判为手机号。"""
    text = (
        "tree 18524091eedc0ab7cb4911690d1b06aaa2cd23dc\n"
        "parent 1380000000012345678901234567890123456789\n"
        "\nmessage"
    )
    stripped = guard._strip_commit_hash_lines(text)
    assert "parent" not in stripped
    assert "tree" not in stripped
    assert "message" in stripped


def test_history_check_reports_credential_hit(monkeypatch, tmp_path):
    """历史中存在手机号形态的真实凭证时必须判 FAIL。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo, {"leak.txt": f"uid {_PHONE_SAMPLE}\n"})

    monkeypatch.setattr(guard, "REPO_ROOT", repo)
    monkeypatch.setattr(guard, "PRODUCTION_DB_PATH", repo / "missing.db")
    outcome = guard.check_no_credential_in_history()
    assert outcome.status == guard.STATUS_FAIL
    assert any("leak.txt" in detail for detail in outcome.details)


def test_worktree_check_reports_credential_hit(monkeypatch, tmp_path):
    """工作区跟踪文件含非占位手机号时必须判 FAIL。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo, {"leak.txt": f"phone {_PHONE_SAMPLE}\n"})

    monkeypatch.setattr(guard, "REPO_ROOT", repo)
    monkeypatch.setattr(guard, "PRODUCTION_DB_PATH", repo / "missing.db")
    outcome = guard.check_no_credential_in_worktree()
    assert outcome.status == guard.STATUS_FAIL


def test_worktree_check_allows_placeholder_phone(monkeypatch, tmp_path):
    """占位前缀手机号出现在跟踪文件中不应判 FAIL。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo, {"fixture.txt": "phone 13800000000\n"})

    monkeypatch.setattr(guard, "REPO_ROOT", repo)
    monkeypatch.setattr(guard, "PRODUCTION_DB_PATH", repo / "missing.db")
    outcome = guard.check_no_credential_in_worktree()
    assert outcome.status == guard.STATUS_PASS


def test_redaction_marker_check_detects_marker(monkeypatch, tmp_path):
    """代码中出现脱敏标记时必须判 FAIL。"""
    package = tmp_path / "seatbot"
    package.mkdir()
    (package / "mod.py").write_text(f'MARK = "{_REDACTION_SAMPLE}"\n', encoding="utf-8")
    monkeypatch.setattr(guard, "PACKAGE_DIR", package)
    outcome = guard.check_no_redaction_marker_in_code()
    assert outcome.status == guard.STATUS_FAIL


def test_historical_trace_check_detects_todo(monkeypatch, tmp_path):
    """代码中残留待办标记时必须判 FAIL。"""
    package = tmp_path / "seatbot"
    package.mkdir()
    (package / "mod.py").write_text("# TODO: 待处理\nVALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(guard, "PACKAGE_DIR", package)
    outcome = guard.check_no_historical_trace()
    assert outcome.status == guard.STATUS_FAIL


def test_historical_trace_check_allows_user_confirmation(monkeypatch, tmp_path):
    """文档注释中的「用户确认」标注属于豁免，不得判 FAIL。"""
    package = tmp_path / "seatbot"
    package.mkdir()
    (package / "mod.py").write_text(
        'def f():\n    """2026-10-20 用户确认：该口径已生效。"""\n    return 1\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(guard, "PACKAGE_DIR", package)
    outcome = guard.check_no_historical_trace()
    assert outcome.status == guard.STATUS_PASS


def test_docstring_coverage_detects_missing(monkeypatch, tmp_path):
    """公共定义缺文档注释时必须判 FAIL。"""
    package = tmp_path / "seatbot"
    package.mkdir()
    (package / "mod.py").write_text("def public():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(guard, "PACKAGE_DIR", package)
    outcome = guard.check_docstring_coverage()
    assert outcome.status == guard.STATUS_FAIL


def test_undefined_name_check_detects_missing(monkeypatch, tmp_path):
    """引用未定义名字时必须判 FAIL。"""
    package = tmp_path / "seatbot"
    package.mkdir()
    (package / "mod.py").write_text(
        'def f():\n    """示例。"""\n    return missing_name\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(guard, "PACKAGE_DIR", package)
    outcome = guard.check_no_undefined_name()
    assert outcome.status == guard.STATUS_FAIL
    assert any("missing_name" in detail for detail in outcome.details)


def test_secret_file_check_detects_tracked_db(monkeypatch, tmp_path):
    """密钥文件被跟踪时必须判 FAIL。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo, {"secret.db": "x"})
    monkeypatch.setattr(guard, "REPO_ROOT", repo)
    outcome = guard.check_no_secret_file_tracked()
    assert outcome.status == guard.STATUS_FAIL


def test_onetime_script_check_detects_tracked_script(monkeypatch, tmp_path):
    """一次性脚本被跟踪时必须判 FAIL。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo, {"scripts/oneoff_demo.py": "print(1)\n"})
    monkeypatch.setattr(guard, "REPO_ROOT", repo)
    outcome = guard.check_no_onetime_script_tracked()
    assert outcome.status == guard.STATUS_FAIL


def test_docs_index_check_detects_drift(monkeypatch, tmp_path):
    """文档索引与实际文件不一致时必须判 FAIL。"""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "docs.md").write_text("- `docs/other.md` — 说明\n", encoding="utf-8")
    (docs / "actual.md").write_text("# 实际文件\n", encoding="utf-8")
    monkeypatch.setattr(guard, "DOCS_DIR", docs)
    monkeypatch.setattr(guard, "DOCS_INDEX_PATH", docs / "docs.md")
    outcome = guard.check_docs_index_consistent()
    assert outcome.status == guard.STATUS_FAIL


def test_main_returns_zero_on_repository_clean():
    """当前仓库自身必须全项通过。"""
    assert guard.main([]) == 0


def test_main_rejects_unknown_check_name():
    """未知的检查名必须被命令行解析拒绝。"""
    with pytest.raises(SystemExit):
        guard.main(["--only", "not-a-check"])


if __name__ == "__main__":  # pragma: no cover - 便于手工执行
    sys.exit(guard.main([]))
