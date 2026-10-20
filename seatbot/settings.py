"""系统设置：已在页面保存过的以页面设置为准，未设置的沿用配置文件。

有效值以页面设置为准，配置文件仅提供初始值；所有校验与默认值收敛于此，供
config / store / scheduler / web 共用，避免散落硬编码枚举。
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# ---- 允许值 ----

SUBMIT_STRATEGIES = (
    "direct_first",        # 直连优先 → 失败落回页面改写（含锚点）
    "direct_only",         # 仅直连，失败即 FAILED
    "page_rewrite_first",  # 页面改写优先 → 直连兜底
    "page_rewrite_only",   # 仅页面改写（等价 direct_submit_enabled=false）
)

SUBMIT_STRATEGY_LABELS: dict[str, str] = {
    "direct_first": "直连优先（推荐）",
    "direct_only": "仅直连",
    "page_rewrite_first": "模拟点击优先",
    "page_rewrite_only": "仅模拟点击",
}

# 详细对比文案供配置页 tooltip / 帮助区复用
SUBMIT_STRATEGY_HELP: dict[str, dict[str, str]] = {
    "direct_first": {
        "label": SUBMIT_STRATEGY_LABELS["direct_first"],
        "badge": "推荐",
        "desc": "优先走直连 API，失败自动落回模拟点击（含锚点重试）。",
    },
    "direct_only": {
        "label": SUBMIT_STRATEGY_LABELS["direct_only"],
        "badge": "",
        "desc": "只走直连 API，失败直接标记失败；适合排障时隔离页面通道。",
    },
    "page_rewrite_first": {
        "label": SUBMIT_STRATEGY_LABELS["page_rewrite_first"],
        "badge": "",
        "desc": "优先走模拟点击，失败再试直连；仅在直连被风控时考虑。",
    },
    "page_rewrite_only": {
        "label": SUBMIT_STRATEGY_LABELS["page_rewrite_only"],
        "badge": "等价布尔开关",
        "desc": "只走模拟点击（等价 direct_submit_enabled=false），直连完全禁用。",
    },
}

# 通道对比表（配置页“两者区别”区块）
SUBMIT_CHANNEL_COMPARE: list[dict[str, str]] = [
    {
        "dim": "原理",
        "direct": "httpx GET 座位页读 #submit_enc 种子 → 本地九字段排序拼 md5 重算 enc → 同会话 POST /submit",
        "page": "Chromium 真实打开座位页 → 点可选格子让页面 JS 构造表单 → route 拦截改写 day/时段并重算 enc 后放行",
    },
    {
        "dim": "依赖",
        "direct": "纯 httpx，无 Playwright",
        "page": "需 Playwright + Chromium，含 networkidle / 点击验证",
    },
    {
        "dim": "是否受“使用中无格子”影响",
        "direct": "免疫（种子在 HTML 中，使用中面板下照样可读）",
        "page": "必挂（账号有进行中使用时任意座位页 li=0）",
    },
    {
        "dim": "速度",
        "direct": "快（<1s，1 次 GET + 1 次 POST）",
        "page": "慢（3–8s，含页面等待与点击）",
    },
    {
        "dim": "失败特征",
        "direct": "seed not found / 服务端“已被预约/参数错误”",
        "page": "no selectable cell / begin-click failed / 无改写响应",
    },
]

RELAY_LEAD_OPTIONS: list[int] = [60, 180, 300, 600]
TICK_INTERVAL_OPTIONS: list[int] = [15, 30, 60]
#: 时长类设置（单段上限 / 每日限额）的合法区间，单位小时。
#: 配置层（seatbot.config.LibraryConfig）与设置层共用本区间，避免两处口径漂移。
HOURS_RANGE: tuple[float, float] = (0.5, 24)
# 守护时段模式：uniform=全局统一（每天相同时段，界面保持单套）；
# weekly=按天自定义（界面显示周一至周日分别勾选）。仅是界面与录入约定，
# 数据层恒为 7 键 dict，执行链路恒按天取值。
SCHEDULE_MODES: tuple[str, ...] = ("uniform", "weekly")

SCHEDULE_MODE_LABELS: dict[str, str] = {
    "uniform": "全局统一",
    "weekly": "按天自定义",
}


# 时间分配策略：两档都以「座位独占优先」为核心——座位与账号一一对应，
# 每座优先由一个账号整座承接，账号之间互不交叉；主账号当日额度装不下时，
# 溢出段仍留在该座位内交给其他账号。
#   safe    = 溢出摊薄：溢出段交给当日已用最少的账号
#   minimal = 最少账号：溢出段优先复用已参与该座的账号
# 仅影响矩阵构建（自动绑定 + 显式重排），执行链路（14:00 提交 / 签到 / 签退）
# 不受影响。
ALLOCATION_STRATEGIES: tuple[str, ...] = ("safe", "minimal")
ALLOCATION_STRATEGY_LABELS: dict[str, str] = {
    "safe": "独占优先·摊薄",
    "minimal": "独占优先·省账号",
}
ALLOCATION_STRATEGY_HELP: dict[str, dict[str, str]] = {
    "safe": {
        "label": "独占优先（摊薄）",
        "badge": "默认",
        "desc": "座位与账号一一对应，每座由一个账号整座承接；主账号额度不足时，"
                "溢出段交给当日已用最少的账号分担（仍在同一座位）。",
    },
    "minimal": {
        "label": "独占优先（省账号）",
        "badge": "省账号",
        "desc": "座位与账号一一对应，每座由一个账号整座承接；主账号额度不足时，"
                "优先复用已参与该座的账号，尽量少动用账号。",
    },
}

DEFAULTS: dict[str, object] = {
    "submit_strategy": "direct_first",
    "relay_lead_seconds": 300,
    "stagger_seconds": [0, 3],
    "tick_interval_seconds": 30,
    "anchor_retry_enabled": True,
    "anchor_scan_limit": 12,
    # 馆舍限额（可在页面调整，未调整时沿用馆舍配置）
    "max_reserve_hours": 2.0,
    "daily_reserve_hours_limit": 14.0,
    "notify_webhook": "",
    "reconcile_interval_seconds": 300,
    "schedule_mode": "uniform",
    "allocation_strategy": "safe",
}


def normalize_schedule_mode(v: object) -> str:
    """校验守护时段模式取值。"""
    s = str(v).strip().lower() if v is not None else ""
    if s in SCHEDULE_MODES:
        return s
    raise ValueError(f"schedule_mode 须为 {', '.join(SCHEDULE_MODES)}，得 {v!r}")


def normalize_allocation_strategy(v: object) -> str:
    """校验时间分配策略取值。"""
    s = str(v).strip().lower() if v is not None else ""
    if s in ALLOCATION_STRATEGIES:
        return s
    raise ValueError(f"allocation_strategy 须为 {', '.join(ALLOCATION_STRATEGIES)}，得 {v!r}")



def normalize_submit_strategy(v: object) -> str:
    """接受四种字符串 + 旧 bool（True→direct_first, False→page_rewrite_only）。"""
    if isinstance(v, bool):
        return "direct_first" if v else "page_rewrite_only"
    s = str(v).strip() if v is not None else ""
    if s in SUBMIT_STRATEGIES:
        return s
    raise ValueError(f"submit_strategy 须为 {', '.join(SUBMIT_STRATEGIES)}，得 {v!r}")


def normalize_hours(v: object) -> float:
    """时长类设置（单段上限 / 每日限额）归一化：必须是允许区间内的小时数。

    入参:
        v: 任意可转 float 的取值（数字或字符串）。

    返回值:
        归一化后的 float。

    异常:
        ValueError: 不是数字，或超出 HOURS_RANGE 允许的区间。
    """
    fv = float(str(v).strip())
    lo, hi = HOURS_RANGE
    if not (lo <= fv <= hi):
        raise ValueError(f"须为 {lo}–{hi}")
    return fv


def normalize_stagger(v: object) -> list[int]:
    """接受 '0,3' / [0,3] / JSON 字符串，返回 [lo, hi]。"""
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return [0, 3]
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                v = parsed
            else:
                v = [int(x.strip()) for x in s.split(",") if x.strip() != ""]
        except Exception:
            v = [int(x.strip()) for x in s.split(",") if x.strip() != ""]
    if not isinstance(v, list) or len(v) != 2:
        raise ValueError("stagger_seconds 须为两个整数，如 0,3")
    lo, hi = int(v[0]), int(v[1])
    if not (0 <= lo <= hi <= 30):
        raise ValueError("stagger_seconds 需满足 0 ≤ lo ≤ hi ≤ 30")
    return [lo, hi]


def coerce_for_storage(key: str, value: object) -> str:
    """统一保存为文本格式；list/dict 转 JSON，其余转 str。"""
    if key == "stagger_seconds" and isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def parse_stored(key: str, raw: str | None, fallback: object) -> object:
    """把 DB 字符串还原为类型化值；解析失败回 fallback。"""
    if raw is None:
        return fallback
    try:
        if key == "submit_strategy":
            return normalize_submit_strategy(raw)
        if key == "relay_lead_seconds":
            v = int(str(raw).strip())
            if v not in RELAY_LEAD_OPTIONS and not (30 <= v <= 900):
                raise ValueError
            return v
        if key == "stagger_seconds":
            return normalize_stagger(raw)
        if key == "tick_interval_seconds":
            v = int(str(raw).strip())
            if v not in TICK_INTERVAL_OPTIONS and not (5 <= v <= 300):
                raise ValueError
            return v
        if key in ("anchor_retry_enabled",):
            s = str(raw).strip().lower()
            return s in ("1", "true", "yes", "on")
        if key == "reconcile_interval_seconds":
            v = int(str(raw).strip())
            if not (60 <= v <= 86400):
                raise ValueError
            return v
        if key == "anchor_scan_limit":
            v = int(str(raw).strip())
            if not (4 <= v <= 20):
                raise ValueError
            return v
        if key in ("max_reserve_hours", "daily_reserve_hours_limit"):
            return normalize_hours(raw)
        if key == "notify_webhook":
            return str(raw).strip()
        if key == "schedule_mode":
            return normalize_schedule_mode(raw)
        if key == "allocation_strategy":
            return normalize_allocation_strategy(raw)
    except Exception as e:
        logger.debug("设置项 %s 的库内值无法解析，回退到默认值: %s", key, e)
        return fallback
    return raw


def validate_all(
    patch: dict[str, object], *, current: dict[str, object] | None = None,
) -> dict[str, str]:
    """校验待写入的键值对，返回 {key: error}，空即通过。

    入参 patch 为本次提交的键值对；current 为库内现值（可选），用于把
    单段上限与每日限额做交叉校验时补齐 patch 未包含的那一侧。
    """
    errors: dict[str, str] = {}
    for k, v in patch.items():
        if k not in DEFAULTS:
            errors[k] = "未知配置项"
            continue
        try:
            if k == "submit_strategy":
                normalize_submit_strategy(v)
            elif k == "relay_lead_seconds":
                iv = int(str(v).strip()) if isinstance(v, str) else int(v)  # type: ignore[arg-type]
                if iv not in RELAY_LEAD_OPTIONS and not (30 <= iv <= 900):
                    raise ValueError(f"须为 {RELAY_LEAD_OPTIONS} 或 30–900 内整数")
            elif k == "stagger_seconds":
                normalize_stagger(v)
            elif k == "tick_interval_seconds":
                iv = int(str(v).strip()) if isinstance(v, str) else int(v)  # type: ignore[arg-type]
                if iv not in TICK_INTERVAL_OPTIONS and not (5 <= iv <= 300):
                    raise ValueError(f"须为 {TICK_INTERVAL_OPTIONS} 或 5–300 内整数")
            elif k == "anchor_retry_enabled":
                if str(v).lower() not in ("true", "false", "1", "0", "yes", "no", "on", "off", True, False):  # type: ignore[comparison-overlap]
                    # 宽松：任何可转 bool 的都放行
                    pass
            elif k == "anchor_scan_limit":
                iv = int(str(v).strip()) if isinstance(v, str) else int(v)  # type: ignore[arg-type]
                if not (4 <= iv <= 20):
                    raise ValueError("须为 4–20 整数")
            elif k == "reconcile_interval_seconds":
                iv = int(str(v).strip()) if isinstance(v, str) else int(v)  # type: ignore[arg-type]
                if not (60 <= iv <= 86400):
                    raise ValueError("须为 60–86400 内整数")
            elif k in ("max_reserve_hours", "daily_reserve_hours_limit"):
                normalize_hours(v)
            elif k == "notify_webhook":
                s = str(v).strip()
                if s and not (s.startswith("http://") or s.startswith("https://")):
                    raise ValueError("须为 http(s) URL 或留空")
            elif k == "schedule_mode":
                normalize_schedule_mode(v)
            elif k == "allocation_strategy":
                normalize_allocation_strategy(v)
        except Exception as e:
            errors[k] = str(e) if str(e) else "格式错误"
    # 交叉校验：单段上限不应超过日限额
    # 解析顺序：patch > current（库内现值）> DEFAULTS。
    # 未提供 current 时，仅当两个键同时出现在 patch 中才判定，避免用默认值误判。
    _mh_keys = ("max_reserve_hours", "daily_reserve_hours_limit")
    if any(k in patch for k in _mh_keys):
        if "max_reserve_hours" in patch or current is not None:
            try:
                def _resolve(key: str) -> float:
                    raw: object = patch.get(key)
                    if raw is None and current is not None:
                        raw = current.get(key)
                    if raw is None:
                        raw = DEFAULTS[key]
                    return float(str(raw).strip())

                if (_resolve("max_reserve_hours") > _resolve("daily_reserve_hours_limit")
                        and "max_reserve_hours" not in errors):
                    errors["max_reserve_hours"] = "单段上限不应超过每日限额"
            except Exception as e:
                logger.debug("单段上限与每日限额的交叉校验跳过: %s", e)
    return errors


def effective(settings_rows: dict[str, str], cfg) -> dict[str, object]:
    """合并有效配置：已在页面保存过的以页面设置为准，未设置的沿用配置文件。

    cfg: seatbot.config.Config 实例；取 library/runtime 的同名字段作初始值。
    """
    out: dict[str, object] = {}
    # 先用默认值兜底
    out.update(DEFAULTS)
    # 再用配置文件覆盖
    try:
        out["max_reserve_hours"] = float(cfg.library.max_reserve_hours)
        out["daily_reserve_hours_limit"] = float(cfg.library.daily_reserve_hours_limit)
    except Exception as e:
        logger.debug("读取配置文件中的馆舍限额失败，沿用默认值: %s", e)
    try:
        out["stagger_seconds"] = list(cfg.runtime.stagger_seconds)
    except Exception as e:
        logger.debug("读取配置文件中的错峰秒数失败，沿用默认值: %s", e)
    for k in ("submit_strategy", "relay_lead_seconds", "tick_interval_seconds",
              "anchor_retry_enabled", "anchor_scan_limit", "notify_webhook",
              "reconcile_interval_seconds"):
        try:
            v = getattr(cfg.runtime, k, None)
            if v is not None:
                out[k] = v
        except Exception as e:
            logger.debug("读取配置文件中的运行参数 %s 失败，沿用默认值: %s", k, e)
    # 布尔开关兼容：direct_submit_enabled=false → page_rewrite_only
    try:
        legacy = getattr(cfg.runtime, "direct_submit_enabled", None)
        if legacy is not None and "submit_strategy" not in settings_rows:
            # 仅当页面未显式设置时，配置文件的布尔开关才生效
            if legacy is False and out.get("submit_strategy") == "direct_first":
                out["submit_strategy"] = "page_rewrite_only"
    except Exception as e:
        logger.debug("读取布尔开关 direct_submit_enabled 失败，沿用 submit_strategy: %s", e)
    # 最后以页面已保存的设置为准
    for k, raw in settings_rows.items():
        if k not in DEFAULTS:
            continue
        out[k] = parse_stored(k, raw, out[k])
    # 兜底：submit_strategy 归一化
    try:
        out["submit_strategy"] = normalize_submit_strategy(out.get("submit_strategy"))
    except Exception as e:
        logger.debug("submit_strategy 归一化失败，回退到默认值: %s", e)
        out["submit_strategy"] = DEFAULTS["submit_strategy"]
    return out
