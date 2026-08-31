"""实况核对：用超星 getusedtimes 的真实占用核验本地任务。

判定与选号函数为纯函数，离线可测；网络与写库由 Scheduler.reconcile_sweep
承担。本模块只写告警与带标记的 last_error，不改任务状态。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from seatbot.models import Account

#: 任务 last_error 中实况核对标记前缀；恢复时仅清自己写的标记
RECONCILE_ERROR_PREFIX = "实况核对: "


def _to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def classify_task(
    start: str, end: str, server_windows: list[tuple[str, str]],
) -> tuple[float, bool]:
    """任务窗口 [start, end) 被服务端占用区间覆盖的比例与是否失守。

    入参均为 "HH:MM" 字符串；server_windows 为 getusedtimes 返回的占用区间。
    covered_ratio == 1.0 判一致，其余（含部分覆盖）均判失守——部分失守
    意味着时段后半段他人可抢，同样需要人工介入。
    返回 (covered_ratio, mismatch)。
    """
    s, e = _to_min(start), _to_min(end)
    span = e - s
    if span <= 0:
        return 1.0, False
    covered = 0
    for ws, we in server_windows:
        lo, hi = max(s, _to_min(ws)), min(e, _to_min(we))
        if hi > lo:
            covered += hi - lo
    ratio = min(1.0, covered / span)
    return ratio, ratio < 1.0


def pick_read_account(
    accounts: list["Account"],
    cookie_recency: dict[str, int] | None = None,
) -> "Account | None":
    """选只读查询账号：优先会话最新的账号，回退第一个有凭据的账号。

    cookie_recency: account_id → account_cookies.updated_at，由调用方
    动态查库传入；不依赖任何具体账号，账号增删后自动重选，已删账号
    的残留记录被自然忽略。并列时取 accounts 列表序（list_accounts
    为 ORDER BY id，稳定）。
    """
    cred = [a for a in accounts if a.phone and a.password]
    if not cred:
        return None
    rec = cookie_recency or {}
    return max(cred, key=lambda a: rec.get(a.id, 0))
