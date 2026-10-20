#!/usr/bin/env python3
"""SeatBot 防回归门禁。

用途
====
把历史上真实发生过、且长期未被任何自动检查拦住的缺陷固化为可执行门禁：

* 真实凭证（手机号 / 口令 / 会话 cookie / 用户 uid）泄漏进 git 历史或工作区；
* 历史清洗的脱敏标记误伤代码常量；
* 生产库中的业务实例（账号 id / 座位号 / 馆舍名）被硬编码进生产代码；
* 代码中残留待办标记、日期、版本号等历史痕迹；
* 密钥文件与一次性脚本被误提交；
* 公共定义缺失文档注释；
* 模块级名字引用未定义；
* ``docs/docs.md`` 索引与 ``docs/`` 实际文件不一致。

运行方式
========
::

    python scripts/guard/guard.py                                  # 执行全部门禁
    python scripts/guard/guard.py --only docstring-coverage        # 只执行单项检查

比对用的数据库默认取仓库根下的生产库（只读）；CI 等无生产库的环境可用环境变量
``SEATBOT_GUARD_DB_PATH`` 指向隔离库，库不存在时相关比对项自动跳过。

任一检查失败时退出码为 1（CI 必挂）；因环境原因无法执行的检查标记为 SKIP 并说明原因。

约束
====
* 仅使用 Python 标准库，不依赖 pytest，可在 CI 最小镜像中直接运行；
* 只读打开生产库（``mode=ro``），不发起任何网络请求，不写入业务数据；
* 任何命中只输出计数与位置，不输出凭证明文。
"""

from __future__ import annotations

import argparse
import ast
import builtins
import fnmatch
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / "seatbot"
TESTS_DIR = REPO_ROOT / "tests"
DOCS_DIR = REPO_ROOT / "docs"
DOCS_INDEX_PATH = DOCS_DIR / "docs.md"

# 比对用的数据库路径：默认取仓库根下的生产库；CI 通过环境变量指向隔离空库。
PRODUCTION_DB_PATH = Path(os.environ.get("SEATBOT_GUARD_DB_PATH") or (REPO_ROOT / "seatbot.db"))

# 测试夹具使用的中性占位手机号前缀（形如 13800000000 / 13900000000）。
# 这些号码不对应任何真实用户，允许出现在测试与文档中；其余 11 位手机号形态一律视为违规。
PLACEHOLDER_PHONE_PREFIXES: tuple[str, ...] = (
    "1380000",
    "1390000",
    "1370000",
    "1360000",
    "1350000",
    "1880000",
    "1580000",
    "1860000",
    "1990000",
    "1500000",
)

# 11 位手机号形态；数字边界用于避免命中更长的数字串（时间戳、对象哈希等）。
PHONE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")

# 登录态 cookie 形态：键名后紧跟取值。
COOKIE_PATTERN = re.compile(r"(?:JSESSIONID|p_auth_token|DSSTASH_LOG|cx_p_token)\s*=\s*[^\s;\"'\r\n]{8,}")

# 历史清洗留下的脱敏标记；出现在代码或测试中说明清洗误伤了代码常量。
REDACTION_MARKER = "***REDACTED***"

# 历史痕迹标记：待办、临时方案、版本号。
TRACE_MARKER_PATTERN = re.compile(r"\b(?:TODO|FIXME|XXX|HACK)\b")
DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
VERSION_MARKER_PATTERN = re.compile(r"(?<!\w)v\d+\s*:")

# docstring 中「日期 + 用户确认」形态的豁免规则：
# 仅当日期出现在文档注释中，且其后 40 个字符内出现「用户确认」时视为用户确认标注，不计为历史痕迹。
USER_CONFIRMATION_MARKER = "用户确认"
USER_CONFIRMATION_LOOKAHEAD = 40

# 不得被 git 跟踪的密钥/运行期文件（按文件名匹配）。
SECRET_FILE_PATTERNS: tuple[str, ...] = ("config.yaml", "*.db", "*.db-wal", "*.db-shm", "*.log")

# ``scripts/`` 下不得入库的一次性脚本（按文件名匹配）。
ONETIME_SCRIPT_PATTERNS: tuple[str, ...] = (
    "oneoff_*.py",
    "probe_*.py",
    "run_*.cmd",
    "_diag_*.py",
    "test_*.py",
    "*.superseded",
)

# 历史清理前的遗留对象基线：这些对象仍含旧版真实用户 uid（当时的清洗只覆盖了手机号与口令层）。
# 豁免按对象 SHA 精确匹配，只覆盖下列对象本身，任何新的凭证泄漏仍会被拦截；
# 一旦这些对象从历史中消失，豁免自动失效且无需维护。
HISTORY_LEAK_EXEMPTIONS: dict[str, str] = {
    "2b57cdf02009da23e39c901587caa6a783c80ff3": "tests/test_client_direct.py",
    "53a86c22857b35ac3174c1fd3d2feb8eddedeeab": "tests/test_client_direct.py",
    "93c44f6f45e2cce63422b4b23600c1971a013964": "docs/superpowers/research/2026-07-08-chaoxing-seat-api.md",
    "f27271cac5712c8dff59b0e98303fb8abfc39724": "docs/superpowers/research/2026-07-08-chaoxing-seat-api.md",
}

# 凭证取值的最小长度：短于此长度的 cookie 值属于馆舍/组织标识（deptId 之类），不是会话秘密。
MIN_SECRET_LENGTH = 8

STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_SKIP = "SKIP"


@dataclass(frozen=True)
class CheckOutcome:
    """单项门禁的执行结果。

    属性
    ----
    name: 检查名，与 ``--only`` 取值一致。
    status: ``PASS`` / ``FAIL`` / ``SKIP`` 之一。
    summary: 一行摘要，说明判定依据与计数。
    details: 命中位置明细（file:line 或对象 SHA），不含任何凭证明文。
    """

    name: str
    status: str
    summary: str
    details: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProductionProfile:
    """生产库中禁止出现在代码与历史里的敏感值与业务实例名单。

    属性
    ----
    available: 生产库是否可读；不可读时相关检查整体跳过。
    credential_values: 真实凭证取值（手机号 / 口令 / 会话 cookie / 用户 uid）。
    account_ids: 真实账号 id 名单。
    seat_numbers: 真实座位号名单。
    library_labels: 真实馆舍名名单。
    """

    available: bool
    credential_values: tuple[str, ...] = ()
    account_ids: tuple[str, ...] = ()
    seat_numbers: tuple[str, ...] = ()
    library_labels: tuple[str, ...] = ()


@dataclass
class SecretMatcher:
    """按形态区分的敏感值匹配器。

    纯数字取值按数字边界匹配，避免命中时间戳、对象哈希等更长数字串；
    非纯数字取值按子串匹配（会话 cookie 等高熵串不会与日常文本混淆）。
    """

    values: tuple[str, ...] = ()

    def find(self, text: str) -> list[str]:
        """返回文本中命中的敏感取值列表。

        参数
        ----
        text: 待扫描文本。

        返回值
        ------
        命中的敏感取值列表（按敏感值登记顺序去重）。
        """
        hits: list[str] = []
        for value in self.values:
            if value.isdigit():
                pattern = rf"(?<!\d){re.escape(value)}(?!\d)"
                if re.search(pattern, text):
                    hits.append(value)
            elif value in text:
                hits.append(value)
        return hits


def _read_text(path: Path) -> str | None:
    """读取文本文件内容；文件缺失、二进制或解码失败时返回 None。

    参数
    ----
    path: 文件路径。

    返回值
    ------
    文件文本内容，或 None。
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _iter_files(root: Path) -> Iterator[Path]:
    """遍历目录下的全部文件，跳过缓存目录。

    参数
    ----
    root: 目录路径。

    返回值
    ------
    文件路径迭代器。
    """
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts:
            continue
        yield path


def _run_git(*args: str) -> str:
    """执行只读 git 命令并返回标准输出。

    参数
    ----
    args: git 子命令参数。

    返回值
    ------
    标准输出文本。
    """
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 执行失败: {result.stderr.strip()}")
    return result.stdout


def _load_production_profile() -> ProductionProfile:
    """只读读取生产库中的凭证取值与业务实例名单。

    返回值
    ------
    生产库画像；生产库不存在或不可读时 ``available`` 为 False。
    """
    if not PRODUCTION_DB_PATH.is_file():
        return ProductionProfile(available=False)

    credentials: list[str] = []
    account_ids: list[str] = []
    seat_numbers: list[str] = []
    library_labels: list[str] = []

    try:
        connection = sqlite3.connect(f"file:{PRODUCTION_DB_PATH}?mode=ro", uri=True)
    except sqlite3.Error:
        return ProductionProfile(available=False)

    try:
        rows = connection.execute("select id, phone, password from accounts").fetchall()
        for account_id, phone, password in rows:
            if account_id:
                account_ids.append(str(account_id))
            if phone and len(str(phone)) >= 4:
                credentials.append(str(phone))
            if password and len(str(password)) >= 4:
                credentials.append(str(password))

        for (cookie_json,) in connection.execute("select cookies_json from account_cookies"):
            try:
                cookies = json.loads(cookie_json)
            except (TypeError, ValueError):
                continue
            if not isinstance(cookies, dict):
                continue
            for value in cookies.values():
                if isinstance(value, str) and len(value) >= MIN_SECRET_LENGTH:
                    credentials.append(value)

        for seat_num, label in connection.execute("select seat_num, label from target_seats"):
            if seat_num:
                seat_numbers.append(str(seat_num))
            if label:
                library_labels.append(str(label))
    except sqlite3.Error:
        return ProductionProfile(available=False)
    finally:
        connection.close()

    return ProductionProfile(
        available=True,
        credential_values=tuple(dict.fromkeys(credentials)),
        account_ids=tuple(dict.fromkeys(account_ids)),
        seat_numbers=tuple(dict.fromkeys(seat_numbers)),
        library_labels=tuple(dict.fromkeys(library_labels)),
    )


def _reachable_objects() -> dict[str, str]:
    """返回 git 历史中全部可达对象及其路径。

    返回值
    ------
    对象 SHA 到路径的映射（提交对象映射为空字符串）。
    """
    mapping: dict[str, str] = {}
    for line in _run_git("rev-list", "--objects", "--all").splitlines():
        parts = line.split(" ", 1)
        if not parts[0]:
            continue
        mapping[parts[0]] = parts[1] if len(parts) > 1 else ""
    return mapping


def _iter_git_objects() -> Iterator[tuple[str, str, bytes]]:
    """按 ``git cat-file --batch`` 协议流式产出全部对象。

    返回值
    ------
    (对象 SHA, 对象类型, 对象内容) 三元组迭代器。
    """
    process = subprocess.Popen(
        ["git", "cat-file", "--batch-all-objects", "--batch"],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
    )
    assert process.stdout is not None
    while True:
        header = process.stdout.readline()
        if not header:
            break
        parts = header.decode("utf-8", "replace").split()
        if len(parts) < 3:
            continue
        size = int(parts[2])
        body = process.stdout.read(size)
        process.stdout.read(1)
        yield parts[0], parts[1], body
    process.wait()


def _strip_commit_hash_lines(text: str) -> str:
    """移除提交对象头部的 tree / parent 哈希行。

    提交对象头部的 40 位十六进制哈希，其前 11 位可能恰好是手机号形态，
    属于已知假阳性，必须在扫描前剔除。

    参数
    ----
    text: 提交对象文本。

    返回值
    ------
    剔除哈希行后的文本。
    """
    kept: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith(("tree ", "parent ")) and re.fullmatch(r"[0-9a-f]{40}", stripped.split(" ", 1)[1]):
            continue
        kept.append(line)
    return "\n".join(kept)


def _is_placeholder_phone(value: str) -> bool:
    """判断手机号形态的取值是否属于占位前缀白名单。

    参数
    ----
    value: 手机号形态的取值。

    返回值
    ------
    属于占位前缀时为 True。
    """
    return value.startswith(PLACEHOLDER_PHONE_PREFIXES)


def check_no_credential_in_history() -> CheckOutcome:
    """扫描 git 历史全部可达对象，检出真实凭证、手机号形态与 cookie 形态泄漏。

    返回值
    ------
    门禁结果；命中真实凭证、手机号形态或 cookie 形态时判 FAIL。
    """
    name = "no-credential-in-history"
    try:
        objects = _reachable_objects()
    except (RuntimeError, OSError) as error:
        return CheckOutcome(name, STATUS_SKIP, f"非 git 仓库或无法读取历史：{error}")

    if not objects:
        return CheckOutcome(name, STATUS_SKIP, "git 历史为空，无法扫描")

    profile = _load_production_profile()
    matcher = SecretMatcher(profile.credential_values) if profile.available else SecretMatcher()

    credential_hits: list[str] = []
    exempted_hits: list[str] = []
    phone_hits: list[str] = []
    placeholder_hits = 0
    cookie_hits: list[str] = []
    binary_objects = 0
    scanned = 0

    for sha, obj_type, body in _iter_git_objects():
        if obj_type not in ("blob", "commit"):
            continue
        if sha not in objects:
            continue
        if b"\x00" in body:
            binary_objects += 1
            continue
        scanned += 1
        text = body.decode("utf-8", "replace")
        if obj_type == "commit":
            text = _strip_commit_hash_lines(text)

        matched = matcher.find(text)
        if matched:
            location = f"{objects.get(sha) or 'commit'}@{sha[:8]}"
            if sha in HISTORY_LEAK_EXEMPTIONS:
                exempted_hits.append(location)
            else:
                credential_hits.append(f"{location}（{len(matched)} 处）")

        for match in PHONE_PATTERN.finditer(text):
            if _is_placeholder_phone(match.group(0)):
                placeholder_hits += 1
            else:
                phone_hits.append(f"{objects.get(sha) or 'commit'}@{sha[:8]}")

        if COOKIE_PATTERN.search(text):
            cookie_hits.append(f"{objects.get(sha) or 'commit'}@{sha[:8]}")

    summary = (
        f"扫描 {scanned} 个可达对象（跳过 {binary_objects} 个二进制对象）；"
        f"真实凭证命中 {len(credential_hits)}；手机号形态命中 {len(phone_hits)}"
        f"（占位前缀豁免 {placeholder_hits}）；cookie 形态命中 {len(cookie_hits)}；"
        f"已登记历史豁免 {len(exempted_hits)}"
    )
    details = tuple(credential_hits + phone_hits + cookie_hits)
    if credential_hits or phone_hits or cookie_hits:
        return CheckOutcome(name, STATUS_FAIL, summary, details)
    if not profile.available:
        summary += "；生产库不可用，真实凭证比对已跳过"
    return CheckOutcome(name, STATUS_PASS, summary, tuple(exempted_hits))


def check_no_credential_in_worktree() -> CheckOutcome:
    """扫描 git 跟踪文件，检出真实凭证值与非占位手机号形态。

    返回值
    ------
    门禁结果；跟踪文件中出现真实凭证或非占位手机号时判 FAIL。
    """
    name = "no-credential-in-worktree"
    try:
        tracked = [line for line in _run_git("ls-files").splitlines() if line.strip()]
    except (RuntimeError, OSError) as error:
        return CheckOutcome(name, STATUS_SKIP, f"非 git 仓库或无法读取跟踪文件：{error}")

    profile = _load_production_profile()
    matcher = SecretMatcher(profile.credential_values) if profile.available else SecretMatcher()

    credential_hits: list[str] = []
    phone_hits: list[str] = []
    placeholder_hits = 0
    cookie_hits: list[str] = []
    scanned = 0

    for relative in tracked:
        path = REPO_ROOT / relative
        if not path.is_file():
            continue
        text = _read_text(path)
        if text is None:
            continue
        scanned += 1
        for match in PHONE_PATTERN.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            if _is_placeholder_phone(match.group(0)):
                placeholder_hits += 1
            else:
                phone_hits.append(f"{relative}:{line}")
        if matcher.find(text):
            credential_hits.append(relative)
        if COOKIE_PATTERN.search(text):
            cookie_hits.append(relative)

    summary = (
        f"扫描 {scanned} 个跟踪文件；真实凭证命中文件 {len(credential_hits)}；"
        f"手机号形态命中 {len(phone_hits)}（占位前缀豁免 {placeholder_hits}）；"
        f"cookie 形态命中文件 {len(cookie_hits)}"
    )
    if credential_hits or phone_hits or cookie_hits:
        return CheckOutcome(
            name,
            STATUS_FAIL,
            summary,
            tuple(credential_hits + phone_hits + cookie_hits),
        )
    if not profile.available:
        summary += "；生产库不可用，真实凭证比对已跳过"
    return CheckOutcome(name, STATUS_PASS, summary)


def check_no_redaction_marker_in_code() -> CheckOutcome:
    """检查代码与测试中是否残留历史清洗的脱敏标记。

    返回值
    ------
    门禁结果；``seatbot/`` 或 ``tests/`` 中出现脱敏标记时判 FAIL。
    """
    name = "no-redaction-marker-in-code"
    hits: list[str] = []
    scanned = 0
    for root in (PACKAGE_DIR, TESTS_DIR):
        for path in _iter_files(root):
            text = _read_text(path)
            if text is None:
                continue
            scanned += 1
            for index, line in enumerate(text.split("\n"), 1):
                if REDACTION_MARKER in line:
                    relative = path.relative_to(REPO_ROOT).as_posix()
                    hits.append(f"{relative}:{index}")
    summary = f"扫描 {scanned} 个代码与测试文件；脱敏标记命中 {len(hits)}"
    if hits:
        return CheckOutcome(name, STATUS_FAIL, summary, tuple(hits))
    return CheckOutcome(name, STATUS_PASS, summary)


def check_no_hardcoded_instance_in_source() -> CheckOutcome:
    """检查生产代码中是否硬编码了生产库的真实账号 id、座位号与馆舍名。

    返回值
    ------
    门禁结果；命中真实业务实例时判 FAIL，生产库不可用时判 SKIP。
    """
    name = "no-hardcoded-instance-in-source"
    profile = _load_production_profile()
    if not profile.available:
        return CheckOutcome(name, STATUS_SKIP, "生产库不可用，无法获取真实业务实例名单")

    instances = profile.account_ids + profile.seat_numbers + profile.library_labels
    patterns = {
        value: re.compile(rf"(?<!\w){re.escape(value)}(?!\w)")
        for value in instances
        if len(value) >= 2
    }

    hits: list[str] = []
    scanned = 0
    for path in _iter_files(PACKAGE_DIR):
        text = _read_text(path)
        if text is None:
            continue
        scanned += 1
        relative = path.relative_to(REPO_ROOT).as_posix()
        for index, line in enumerate(text.split("\n"), 1):
            for value, pattern in patterns.items():
                if pattern.search(line):
                    hits.append(f"{relative}:{index}（实例名长度 {len(value)}）")
                    break

    summary = f"扫描 {scanned} 个生产代码文件；真实实例硬编码命中 {len(hits)}"
    if hits:
        return CheckOutcome(name, STATUS_FAIL, summary, tuple(hits))
    return CheckOutcome(name, STATUS_PASS, summary)


def _docstring_confirmed_lines(path: Path) -> set[int]:
    """返回带「用户确认」标注的日期所在行号集合。

    解析文档注释，凡日期形态之后 40 个字符内出现「用户确认」的，视为用户确认标注并豁免；
    其余日期一律计入历史痕迹。

    参数
    ----
    path: Python 源文件路径。

    返回值
    ------
    豁免行号集合。
    """
    exempt: set[int] = set()
    text = _read_text(path)
    if text is None:
        return exempt
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return exempt
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        docstring_node = ast.get_docstring(node, clean=False)
        if not docstring_node:
            continue
        raw = node.body[0]
        if not isinstance(raw, ast.Expr) or not isinstance(raw.value, ast.Constant):
            continue
        value = raw.value.value
        if not isinstance(value, str):
            continue
        for match in DATE_PATTERN.finditer(value):
            tail = value[match.end() : match.end() + USER_CONFIRMATION_LOOKAHEAD]
            if USER_CONFIRMATION_MARKER in tail:
                exempt.add(raw.value.lineno + value.count("\n", 0, match.start()))
    return exempt


def check_no_historical_trace() -> CheckOutcome:
    """检查生产代码中是否残留待办标记、日期与版本号等历史痕迹。

    返回值
    ------
    门禁结果；命中历史痕迹时判 FAIL。
    """
    name = "no-historical-trace"
    hits: list[str] = []
    scanned = 0
    for path in _iter_files(PACKAGE_DIR):
        text = _read_text(path)
        if text is None:
            continue
        scanned += 1
        relative = path.relative_to(REPO_ROOT).as_posix()
        exempt_lines = _docstring_confirmed_lines(path) if path.suffix == ".py" else set()
        for index, line in enumerate(text.split("\n"), 1):
            if TRACE_MARKER_PATTERN.search(line):
                hits.append(f"{relative}:{index}（待办标记）")
                continue
            if VERSION_MARKER_PATTERN.search(line):
                hits.append(f"{relative}:{index}（版本标记）")
                continue
            if DATE_PATTERN.search(line) and index not in exempt_lines:
                hits.append(f"{relative}:{index}（日期形态）")
    summary = f"扫描 {scanned} 个生产代码文件；历史痕迹命中 {len(hits)}"
    if hits:
        return CheckOutcome(name, STATUS_FAIL, summary, tuple(hits))
    return CheckOutcome(name, STATUS_PASS, summary)


def check_no_secret_file_tracked() -> CheckOutcome:
    """检查密钥文件与运行期数据文件是否被 git 跟踪。

    返回值
    ------
    门禁结果；命中被跟踪的密钥文件时判 FAIL。
    """
    name = "no-secret-file-tracked"
    try:
        tracked = [line for line in _run_git("ls-files").splitlines() if line.strip()]
    except (RuntimeError, OSError) as error:
        return CheckOutcome(name, STATUS_SKIP, f"非 git 仓库或无法读取跟踪文件：{error}")

    hits = [
        relative
        for relative in tracked
        if any(fnmatch.fnmatch(Path(relative).name, pattern) for pattern in SECRET_FILE_PATTERNS)
    ]
    summary = f"扫描 {len(tracked)} 个跟踪文件；密钥/运行期文件命中 {len(hits)}"
    if hits:
        return CheckOutcome(name, STATUS_FAIL, summary, tuple(hits))
    return CheckOutcome(name, STATUS_PASS, summary)


def check_no_onetime_script_tracked() -> CheckOutcome:
    """检查 ``scripts/`` 下的一次性脚本是否被 git 跟踪。

    返回值
    ------
    门禁结果；命中被跟踪的一次性脚本时判 FAIL。
    """
    name = "no-onetime-script-tracked"
    try:
        tracked = [line for line in _run_git("ls-files").splitlines() if line.strip()]
    except (RuntimeError, OSError) as error:
        return CheckOutcome(name, STATUS_SKIP, f"非 git 仓库或无法读取跟踪文件：{error}")

    hits = [
        relative
        for relative in tracked
        if relative.startswith("scripts/")
        and any(fnmatch.fnmatch(Path(relative).name, pattern) for pattern in ONETIME_SCRIPT_PATTERNS)
    ]
    summary = f"扫描 {len(tracked)} 个跟踪文件；一次性脚本命中 {len(hits)}"
    if hits:
        return CheckOutcome(name, STATUS_FAIL, summary, tuple(hits))
    return CheckOutcome(name, STATUS_PASS, summary)


def check_docstring_coverage() -> CheckOutcome:
    """统计生产代码中公共定义的文档注释覆盖率，要求达到 100%。

    返回值
    ------
    门禁结果；覆盖率不足 100% 时判 FAIL。
    """
    name = "docstring-coverage"
    total = 0
    documented = 0
    missing: list[str] = []
    for path in _iter_files(PACKAGE_DIR):
        if path.suffix != ".py":
            continue
        text = _read_text(path)
        if text is None:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        relative = path.relative_to(REPO_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if node.name.startswith("_"):
                continue
            total += 1
            if ast.get_docstring(node):
                documented += 1
            else:
                missing.append(f"{relative}:{node.lineno}（{node.name}）")
    summary = f"公共定义 {total} 个，已写文档注释 {documented} 个"
    if total == 0:
        return CheckOutcome(name, STATUS_SKIP, "未找到可统计的公共定义")
    if documented < total:
        summary += f"，覆盖率 {documented / total:.1%}（要求 100%）"
        return CheckOutcome(name, STATUS_FAIL, summary, tuple(missing))
    summary += "，覆盖率 100%"
    return CheckOutcome(name, STATUS_PASS, summary)


def _target_names(node: ast.AST) -> set[str]:
    """返回赋值目标绑定的名字。

    参数
    ----
    node: 赋值目标节点。

    返回值
    ------
    绑定的名字集合。
    """
    names: set[str] = set()
    if isinstance(node, ast.Name):
        names.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for element in node.elts:
            names |= _target_names(element)
    elif isinstance(node, ast.Starred):
        names |= _target_names(node.value)
    return names


def _match_bindings(pattern: ast.pattern) -> set[str]:
    """返回 match 语句模式绑定的名字。

    参数
    ----
    pattern: match 模式节点。

    返回值
    ------
    绑定的名字集合。
    """
    names: set[str] = set()
    if isinstance(pattern, ast.MatchAs):
        if pattern.pattern is not None:
            names |= _match_bindings(pattern.pattern)
        if pattern.name:
            names.add(pattern.name)
    elif isinstance(pattern, ast.MatchStar):
        if pattern.name:
            names.add(pattern.name)
    elif isinstance(pattern, ast.MatchMapping):
        for sub in pattern.patterns:
            names |= _match_bindings(sub)
        if pattern.rest:
            names.add(pattern.rest)
    elif isinstance(pattern, (ast.MatchSequence, ast.MatchOr)):
        for sub in pattern.patterns:
            names |= _match_bindings(sub)
    elif isinstance(pattern, ast.MatchClass):
        for sub in pattern.patterns:
            names |= _match_bindings(sub)
        for sub in pattern.kwd_patterns:
            names |= _match_bindings(sub)
    return names


def _statement_bindings(statement: ast.stmt) -> set[str]:
    """返回单条语句及其内嵌语句块绑定的名字。

    参数
    ----
    statement: 语句节点。

    返回值
    ------
    绑定的名字集合。
    """
    names: set[str] = set()
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {statement.name}
    if isinstance(statement, ast.Import):
        for alias in statement.names:
            names.add(alias.asname or alias.name.split(".")[0])
        return names
    if isinstance(statement, ast.ImportFrom):
        for alias in statement.names:
            names.add("*" if alias.name == "*" else (alias.asname or alias.name))
        return names
    if isinstance(statement, ast.Assign):
        for target in statement.targets:
            names |= _target_names(target)
        return names
    if isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
        names |= _target_names(statement.target)
        return names
    if isinstance(statement, (ast.For, ast.AsyncFor)):
        names |= _target_names(statement.target)
        names |= _collect_bindings(statement.body)
        names |= _collect_bindings(statement.orelse)
        return names
    if isinstance(statement, (ast.With, ast.AsyncWith)):
        for item in statement.items:
            if item.optional_vars is not None:
                names |= _target_names(item.optional_vars)
        names |= _collect_bindings(statement.body)
        return names
    if isinstance(statement, ast.Try):
        names |= _collect_bindings(statement.body)
        for handler in statement.handlers:
            if handler.name:
                names.add(handler.name)
            names |= _collect_bindings(handler.body)
        names |= _collect_bindings(statement.orelse)
        names |= _collect_bindings(statement.finalbody)
        return names
    if isinstance(statement, (ast.If, ast.While)):
        names |= _collect_bindings(statement.body)
        names |= _collect_bindings(statement.orelse)
        return names
    if isinstance(statement, ast.Match):
        for case in statement.cases:
            names |= _match_bindings(case.pattern)
            names |= _collect_bindings(case.body)
        return names
    if isinstance(statement, (ast.Global, ast.Nonlocal)):
        names |= set(statement.names)
        return names
    return names


def _collect_bindings(body: Sequence[ast.stmt]) -> set[str]:
    """收集语句块（含条件分支与循环体）内绑定的名字。

    参数
    ----
    body: 语句序列。

    返回值
    ------
    绑定的名字集合。
    """
    names: set[str] = set()
    for statement in body:
        names |= _statement_bindings(statement)
    return names


def _walk_scope(body: Sequence[ast.stmt]) -> Iterator[ast.AST]:
    """遍历语句块内的节点，不进入嵌套函数与类的定义体。

    参数
    ----
    body: 语句序列。

    返回值
    ------
    节点迭代器。
    """
    stack: list[ast.AST] = list(body)
    while stack:
        current = stack.pop()
        yield current
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for child in ast.iter_child_nodes(current):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            stack.append(child)


def _nested_definitions(body: Sequence[ast.stmt]) -> list[ast.stmt]:
    """返回语句块内直接嵌套的函数与类定义（含条件分支内）。

    参数
    ----
    body: 语句序列。

    返回值
    ------
    函数与类定义节点列表。
    """
    found: list[ast.stmt] = []
    stack: list[ast.AST] = list(body)
    while stack:
        current = stack.pop()
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.append(current)
            continue
        for child in ast.iter_child_nodes(current):
            if isinstance(child, ast.stmt):
                stack.append(child)
    return found


def _function_scope_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """返回函数体内的局部名字集合。

    覆盖语句级绑定、参数、推导式目标、lambda 参数与海象表达式赋值。

    参数
    ----
    node: 函数定义节点。

    返回值
    ------
    局部名字集合。
    """
    names = _collect_bindings(node.body)
    arguments = node.args
    for argument in list(arguments.posonlyargs) + list(arguments.args) + list(arguments.kwonlyargs):
        names.add(argument.arg)
    if arguments.vararg:
        names.add(arguments.vararg.arg)
    if arguments.kwarg:
        names.add(arguments.kwarg.arg)
    for statement in node.body:
        if isinstance(statement, (ast.Global, ast.Nonlocal)):
            names |= set(statement.names)
    for child in _walk_scope(node.body):
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
            names.add(child.id)
        elif isinstance(child, ast.comprehension):
            names |= _target_names(child.target)
        elif isinstance(child, ast.Lambda):
            lambda_arguments = child.args
            for argument in list(lambda_arguments.posonlyargs) + list(lambda_arguments.args):
                names.add(argument.arg)
            if lambda_arguments.vararg:
                names.add(lambda_arguments.vararg.arg)
            if lambda_arguments.kwarg:
                names.add(lambda_arguments.kwarg.arg)
    return names


def _analyze_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    module_names: set[str],
    outer_names: set[str],
    issues: list[str],
    relative: str,
) -> None:
    """检查函数体内的名字引用是否都有定义。

    参数
    ----
    node: 函数定义节点。
    module_names: 模块级名字集合。
    outer_names: 外层作用域可见的名字集合（闭包）。
    issues: 未定义名字的收集列表，原地追加。
    relative: 源文件相对路径，用于位置标注。
    """
    local_names = _function_scope_names(node)
    available = local_names | module_names | outer_names
    for child in _walk_scope(node.body):
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            if child.id not in available:
                issues.append(f"{relative}:{child.lineno}（未定义名字 {child.id}）")
    for nested in _nested_definitions(node.body):
        if isinstance(nested, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _analyze_function(nested, module_names, outer_names | local_names, issues, relative)
        else:
            _analyze_class(nested, module_names, outer_names | local_names, issues, relative)


def _analyze_class(
    node: ast.ClassDef,
    module_names: set[str],
    outer_names: set[str],
    issues: list[str],
    relative: str,
) -> None:
    """检查类体内的名字引用与嵌套定义。

    方法体不共享类作用域（与 Python 实际语义一致），只有定义在类体内的嵌套函数才能看到类作用域。

    参数
    ----
    node: 类定义节点。
    module_names: 模块级名字集合。
    outer_names: 外层作用域可见的名字集合。
    issues: 未定义名字的收集列表，原地追加。
    relative: 源文件相对路径，用于位置标注。
    """
    class_names = _collect_bindings(node.body) | module_names | outer_names
    for statement in node.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _analyze_function(statement, module_names, outer_names, issues, relative)
            continue
        if isinstance(statement, ast.ClassDef):
            _analyze_class(statement, module_names, outer_names, issues, relative)
            continue
        for child in ast.walk(statement):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                if child.id not in class_names:
                    issues.append(f"{relative}:{child.lineno}（未定义名字 {child.id}）")


def check_no_undefined_name() -> CheckOutcome:
    """检查生产代码中模块级名字引用是否都有定义。

    以作用域近似实现：模块级绑定 + 内置名 + 闭包可见名构成可用集合；
    动态特性（``import *``、运行期注入的名字）无法静态判定，对相关模块整体跳过并说明。

    返回值
    ------
    门禁结果；存在未定义名字时判 FAIL。
    """
    name = "no-undefined-name"
    issues: list[str] = []
    skipped: list[str] = []
    scanned = 0

    for path in _iter_files(PACKAGE_DIR):
        if path.suffix != ".py":
            continue
        text = _read_text(path)
        if text is None:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError as error:
            skipped.append(f"{path.relative_to(REPO_ROOT).as_posix()}（语法错误：{error.msg}）")
            continue
        scanned += 1
        relative = path.relative_to(REPO_ROOT).as_posix()
        module_names = _collect_bindings(tree.body)
        module_names |= set(dir(builtins))
        module_names |= {
            "__name__",
            "__file__",
            "__doc__",
            "__package__",
            "__spec__",
            "__loader__",
            "__builtins__",
        }
        if "*" in module_names:
            skipped.append(f"{relative}（存在 import *，无法静态判定名字解析）")
            continue
        for statement in tree.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _analyze_function(statement, module_names, set(), issues, relative)
            elif isinstance(statement, ast.ClassDef):
                _analyze_class(statement, module_names, set(), issues, relative)
            else:
                for child in _walk_scope([statement]):
                    if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                        if child.id not in module_names:
                            issues.append(f"{relative}:{child.lineno}（未定义名字 {child.id}）")

    summary = f"扫描 {scanned} 个模块；未定义名字命中 {len(issues)}；跳过 {len(skipped)}"
    if issues:
        return CheckOutcome(name, STATUS_FAIL, summary, tuple(issues))
    if skipped:
        summary += "（" + "；".join(skipped) + "）"
    return CheckOutcome(name, STATUS_PASS, summary, tuple(skipped))


def _docs_index_tokens(index_text: str) -> list[str]:
    """解析文档索引中出现的 markdown 文件引用。

    参数
    ----
    index_text: 索引文件文本。

    返回值
    ------
    引用片段列表（可能带目录前缀）。
    """
    return re.findall(r"[^\s`（）()、,，;；:：]+\.md", index_text)


def check_docs_index_consistent() -> CheckOutcome:
    """双向对账 ``docs/docs.md`` 索引与 ``docs/`` 实际文件。

    索引中登记但实际不存在的文件判 FAIL；实际存在但索引未登记的文档同样判 FAIL。
    登记判定同时接受完整路径与纯文件名两种写法（索引允许在描述中以文件名提及文档）。

    返回值
    ------
    门禁结果；对账不一致时判 FAIL。
    """
    name = "docs-index-consistent"
    if not DOCS_INDEX_PATH.is_file():
        return CheckOutcome(name, STATUS_SKIP, f"文档索引不存在：{DOCS_INDEX_PATH.relative_to(REPO_ROOT).as_posix()}")

    index_text = _read_text(DOCS_INDEX_PATH) or ""
    tokens = _docs_index_tokens(index_text)

    mentioned_paths: set[str] = set()
    mentioned_names: set[str] = set()
    missing: list[str] = []
    for token in tokens:
        normalized = token.strip("./").replace("\\", "/")
        if "/" in normalized:
            mentioned_paths.add(normalized)
            candidates = [REPO_ROOT / normalized, DOCS_DIR / normalized]
            if not any(candidate.is_file() for candidate in candidates):
                missing.append(normalized)
        else:
            mentioned_names.add(normalized)

    actual: set[str] = set()
    for path in _iter_files(DOCS_DIR):
        if path.suffix != ".md":
            continue
        relative = path.relative_to(REPO_ROOT).as_posix()
        if relative == DOCS_INDEX_PATH.relative_to(REPO_ROOT).as_posix():
            continue
        actual.add(relative)

    unregistered = sorted(
        relative
        for relative in actual
        if relative not in mentioned_paths and Path(relative).name not in mentioned_names
    )

    summary = f"索引登记 {len(tokens)} 条；实际文档 {len(actual)} 篇；登记但缺失 {len(missing)}；存在但未登记 {len(unregistered)}"
    if missing or unregistered:
        return CheckOutcome(name, STATUS_FAIL, summary, tuple(missing + unregistered))
    return CheckOutcome(name, STATUS_PASS, summary)


CHECKS: tuple[tuple[str, Callable[[], CheckOutcome]], ...] = (
    ("no-credential-in-history", check_no_credential_in_history),
    ("no-credential-in-worktree", check_no_credential_in_worktree),
    ("no-redaction-marker-in-code", check_no_redaction_marker_in_code),
    ("no-hardcoded-instance-in-source", check_no_hardcoded_instance_in_source),
    ("no-historical-trace", check_no_historical_trace),
    ("no-secret-file-tracked", check_no_secret_file_tracked),
    ("no-onetime-script-tracked", check_no_onetime_script_tracked),
    ("docstring-coverage", check_docstring_coverage),
    ("no-undefined-name", check_no_undefined_name),
    ("docs-index-consistent", check_docs_index_consistent),
)


def main(argv: Sequence[str] | None = None) -> int:
    """执行门禁检查并输出结果。

    参数
    ----
    argv: 命令行参数；None 时取 ``sys.argv[1:]``。

    返回值
    ------
    退出码：全部通过为 0，存在失败项为 1。
    """
    parser = argparse.ArgumentParser(description="SeatBot 防回归门禁")
    parser.add_argument(
        "--only",
        action="append",
        dest="only",
        choices=[check_name for check_name, _ in CHECKS],
        help="只执行指定检查（可重复给出）",
    )
    args = parser.parse_args(argv)

    selected = CHECKS
    if args.only:
        selected = tuple(item for item in CHECKS if item[0] in set(args.only))

    outcomes: list[CheckOutcome] = []
    for check_name, check_func in selected:
        try:
            outcome = check_func()
        except Exception as error:  # noqa: BLE001 - 门禁自身异常按失败处理并给出原因
            outcome = CheckOutcome(check_name, STATUS_FAIL, f"检查执行异常：{type(error).__name__}: {error}")
        outcomes.append(outcome)
        print(f"{outcome.status}: {outcome.name} — {outcome.summary}")
        for detail in outcome.details:
            print(f"    · {detail}")

    failed = sum(1 for outcome in outcomes if outcome.status == STATUS_FAIL)
    skipped = sum(1 for outcome in outcomes if outcome.status == STATUS_SKIP)
    passed = len(outcomes) - failed - skipped
    print(f"SUMMARY: {len(outcomes)} 项检查 / {passed} PASS / {failed} FAIL / {skipped} SKIP")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
