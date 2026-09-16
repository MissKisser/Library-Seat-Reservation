/* SeatBot app.js — Alpine 组件 + 全局函数。
 * 替代旧 theme.js / clock.js，统一交互行为。
 */
(function () {
  const THEME_KEY = 'seatbot-theme';
  const root = document.documentElement;

  /* ===== 主题：初始化 + 全局切换 ===== */
  function applyTheme(t) {
    root.setAttribute('data-theme', t);
  }
  // 尽早应用主题，避免闪烁（在 <head> 内联或 defer 前执行）
  const saved = (() => { try { return localStorage.getItem(THEME_KEY); } catch (e) { return null; } })();
  if (saved === 'light' || saved === 'dark') applyTheme(saved);
  // 未存偏好时保持 HTML 标签的默认 data-theme="light"

  window.themeInit = function () {
    return {
      isDark: root.getAttribute('data-theme') === 'dark',
      toggle() {
        this.isDark = !this.isDark;
        applyTheme(this.isDark ? 'dark' : 'light');
        try { localStorage.setItem(THEME_KEY, this.isDark ? 'dark' : 'light'); } catch (e) {}
      },
    };
  };

  /* ===== Toast 全局 ===== */
  window.__seatbotToasts = [];
  let toastId = 0;
  window.showToast = function ({ type = 'info', title = '', desc = '' }) {
    const id = ++toastId;
    window.__seatbotToasts.push({ id, type, title, desc });
    document.dispatchEvent(new CustomEvent('toast-new'));
    const ttl = type === 'error' ? 6000 : 4000;
    setTimeout(() => window.dismissToast(id), ttl);
  };
  window.dismissToast = function (id) {
    const i = window.__seatbotToasts.findIndex(t => t.id === id);
    if (i >= 0) window.__seatbotToasts.splice(i, 1);
    document.dispatchEvent(new CustomEvent('toast-new'));
  };

  /* Alpine 组件：toast 容器（挂在 base.html）*/
  window.toastContainer = function () {
    return {
      toasts: [],
      init() {
        const sync = () => { this.toasts = window.__seatbotToasts.slice(); };
        sync();
        document.addEventListener('toast-new', sync);
      },
      dismiss(id) { window.dismissToast(id); },
    };
  };

  /* ===== 确认弹层 ===== */
  let confirmResolver = null;
  window.confirmAction = function ({ title = '确认操作', desc = '', confirmText = '确认', danger = false } = {}) {
    return new Promise((resolve) => {
      confirmResolver = resolve;
      const modal = document.getElementById('confirm-modal');
      if (!modal) { resolve(window.confirm(title)); return; }
      modal.dispatchEvent(new CustomEvent('open', {
        detail: { title, desc, confirmText, danger }
      }));
    });
  };
  window._resolveConfirm = function (val) {
    if (confirmResolver) { confirmResolver(val); confirmResolver = null; }
  };
  window.confirmModal = function () {
    return {
      open: false, title: '', desc: '', confirmText: '确认', danger: false,
      init() {
        document.getElementById('confirm-modal').addEventListener('open', (e) => {
          Object.assign(this, e.detail);
          this.open = true;
        });
      },
      confirm() { this.open = false; window._resolveConfirm(true); },
      cancel()  { this.open = false; window._resolveConfirm(false); },
    };
  };

  /* ===== PRG Toast：读 URL flash param 显示 toast ===== */
  window.prgToast = function () {
    const map = {
      saved: { type: 'success', title: '保存成功' },
      deleted: { type: 'success', title: '已删除' },
      created: { type: 'success', title: '已创建' },
      updated: { type: 'success', title: '已更新' },
      cancelled: { type: 'success', title: '已取消' },
      signed: { type: 'success', title: '已签到' },
      left: { type: 'success', title: '已签退' },
      reserved: { type: 'success', title: '已续约' },
      imported: { type: 'success', title: '已导入预约' },
      error: { type: 'error', title: '操作失败' },
    };
    const params = new URLSearchParams(location.search);
    let shown = false;
    for (const [k, v] of params) {
      if (map[k]) {
        // error 携带具体原因时，把它作为 toast 描述
        const t = map[k];
        window.showToast(k === 'error' && v && v !== '1' ? { ...t, desc: v } : t);
        shown = true;
      }
    }
    if (shown && window.history.replaceState) {
      const url = location.pathname + location.hash;
      window.history.replaceState(null, '', url);
    }
  };

  /* ===== 顶栏时钟：本地秒级走字 + 每 30s 轮询 /api/status 校准偏移 ===== */
  window.topbarClock = function () {
    return {
      now: '--:--', nextAt: '--:--', nextAcc: '—',
      _offsetMs: null,
      init() {
        this.tick();
        setInterval(() => this.tick(), 30000);
        setInterval(() => this.tickLocal(), 1000);
      },
      tickLocal() {
        if (this._offsetMs !== null) {
          this.now = fmtHHMM(new Date(Date.now() + this._offsetMs));
        }
      },
      async tick() {
        try {
          const r = await fetch('/api/status', { cache: 'no-store' });
          if (!r.ok) return;
          const j = await r.json();
          if (j.now_ms) { this._offsetMs = j.now_ms - Date.now(); this.tickLocal(); }
          if (j.next_relay_at) {
            this.nextAt = fmtHHMM(new Date(j.next_relay_at));
            this.nextAcc = j.next_relay_account_id || '-';
          } else {
            this.nextAt = '--:--';
            this.nextAcc = '夜间静默';
          }
        } catch (e) { /* keep last */ }
      },
    };
  };
  function fmtHHMM(d) { return d.toTimeString().slice(0, 5); }

  /* ===== 账号验证按钮（单账号）状态机 ===== */
  // 可读失败原因映射
  const ERR_MAP = [
    [/(timeout|timed?\s*out)/i, '登录超时，请重试'],
    [/(password|passwd|密码|账号或密码|401|unauthorized)/i, '手机号或密码错误'],
    [/(risk|风控|vc3|auth cookies|cookie)/i, '登录被平台安全策略拦截，可稍后重试'],
    [/(network|connection|econnrefused|ENOTFOUND)/i, '网络连接失败'],
    [/(not found|account not found)/i, '账号不存在'],
  ];
  function humanizeError(err) {
    if (!err) return '未知错误';
    for (const [re, msg] of ERR_MAP) if (re.test(err)) return msg;
    return err;
  }

  window.verifyButton = function (accountId) {
    return {
      accountId,
      running: false,
      state: 'idle',      // idle | running | success | failed
      message: '',
      get label() {
        if (this.running) return '验证中…';
        if (this.state === 'success') return '已验证';
        return '验证登录';
      },
      async run() {
        if (this.running) return;
        this.running = true; this.state = 'running'; this.message = '';
        try {
          const r = await fetch(`/accounts/${this.accountId}/test-login`, { method: 'POST' });
          const j = await r.json().catch(() => ({}));
          if (r.ok && j.ok) {
            this.state = 'success';
            this.message = '✓ 登录成功';
            window.showToast({ type: 'success', title: '验证通过', desc: this.accountId });
          } else {
            this.state = 'failed';
            this.message = '✗ ' + humanizeError(j.error);
            window.showToast({ type: 'error', title: '验证失败', desc: `${this.accountId} · ${humanizeError(j.error)}` });
          }
        } catch (e) {
          this.state = 'failed';
          this.message = '✗ 网络错误';
          window.showToast({ type: 'error', title: '验证失败', desc: this.accountId + ' · 网络错误' });
        } finally {
          this.running = false;
        }
      },
    };
  };

  /* ===== 批量验证 ===== */
  window.verifyAll = function (accountIds) {
    return {
      ids: accountIds,
      running: false, done: 0, total: accountIds.length,
      failedIds: [],
      get label() {
        if (!this.running) return '全部验证';
        return `验证中 ${this.done}/${this.total}`;
      },
      async run() {
        if (this.running) return;
        this.running = true; this.done = 0; this.failedIds = [];
        for (const id of this.ids) {
          // 串行 fetch 每个账号；若该行有 verifyButton 组件则同步更新其 UI
          let ok = false;
          let errMsg = '';
          try {
            const r = await fetch(`/accounts/${id}/test-login`, { method: 'POST' });
            const j = await r.json().catch(() => ({}));
            ok = r.ok && j.ok;
            if (!ok) errMsg = humanizeError(j.error);
          } catch (e) { errMsg = '网络错误'; }
          if (!ok) this.failedIds.push(id);
          // 同步更新行内 verifyButton 状态（如果该行在 DOM 中）
          const cell = document.querySelector(`[data-verify-id="${id}"]`);
          const comp = cell && cell._x_dataStack && cell._x_dataStack[0];
          if (comp) {
            comp.state = ok ? 'success' : 'failed';
            comp.message = ok ? '✓ 登录成功' : '✗ ' + errMsg;
          }
          this.done++;
          await new Promise(res => setTimeout(res, 400)); // 串行间隔，避免 Playwright 并发
        }
        this.running = false;
        const okCount = this.total - this.failedIds.length;
        window.showToast({
          type: this.failedIds.length ? 'warn' : 'success',
          title: '批量验证完成',
          desc: `${okCount} 通过，${this.failedIds.length} 失败`,
        });
      },
    };
  };

  /* ===== 账号表单：座位×时段矩阵编辑器（模式由服务端全局设置驱动） ===== */
  /* opts = {seats, mode: 'uniform'|'weekly', initial: {seat: {mon..sun: ["HH:MM-HH:MM"]}},
   *         others: [{id, seatSlots}], open, close, maxHours}
   * 内部状态 perDay[wd][seat] = [{s,e}]；全局统一模式以周一为源、写回时铺满 7 天；
   * sync() 序列化为规范形态 {seat: {mon..sun: [...]}}，全天空的座位不出现在 JSON 里。 */
  window.matrixEditor = function (opts) {
    const WDS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];
    const WD_LABELS = { mon: '周一', tue: '周二', wed: '周三', thu: '周四', fri: '周五', sat: '周六', sun: '周日' };
    const toMin = s => { const [h, m] = s.split(':').map(Number); return h * 60 + m; };
    const toHM = v => String(Math.floor(v / 60)).padStart(2, '0') + ':' + String(v % 60).padStart(2, '0');
    const ticks = [];
    for (let t = toMin(opts.open); t <= toMin(opts.close); t += 30) ticks.push(toHM(t));

    const fullDayRanges = () => {
      const out = [];
      for (let t = toMin('08:00'); t < toMin('22:00'); t += 120)
        out.push({ s: toHM(t), e: toHM(Math.min(t + 120, toMin('22:00'))) });
      return out;
    };

    const perDay = {};
    WDS.forEach(wd => {
      perDay[wd] = {};
      opts.seats.forEach(seat => {
        let ranges = [];
        const v = (opts.initial || {})[seat];
        if (Array.isArray(v)) ranges = v;
        else if (v === 'full') ranges = fullDayRanges();
        else if (v && typeof v === 'object') {
          const dv = v[wd];
          if (Array.isArray(dv)) ranges = dv;
          else if (dv === 'full') ranges = fullDayRanges();
        }
        perDay[wd][seat] = ranges.map(r => {
          const dash = r.indexOf('-');
          return { s: r.slice(0, dash), e: r.slice(dash + 1) };
        });
      });
    });
    /* ★ perDay 必须挂进返回的 state 才有 Alpine 深层响应式；
     * 放闭包里会导致增删时段不触发重渲染。以下所有方法经 this.perDay 访问。 */
    return {
      seats: opts.seats,
      wds: WDS,
      wdLabels: WD_LABELS,
      activeDay: 'mon',
      mode: opts.mode === 'weekly' ? 'weekly' : 'uniform',
      perDay,
      ticks,
      maxHours: opts.maxHours,
      others: opts.others || [],

      get uniformMode() { return this.mode !== 'weekly'; },
      dayLabel(wd) { return WD_LABELS[wd]; },
      get rows() {
        const day = this.uniformMode ? 'mon' : this.activeDay;
        return this.seats.map(seat => ({ seat, slots: this.perDay[day][seat] }));
      },

      validateRow(row) {
        const msgs = [];
        const maxMin = this.maxHours * 60;
        const spans = row.slots.map(x => ({ s: toMin(x.s), e: toMin(x.e) }));
        spans.forEach((x, i) => {
          if (x.e <= x.s) msgs.push('结束需晚于开始');
          if (x.e - x.s > maxMin) msgs.push('单段超 ' + this.maxHours + 'h 上限');
          spans.forEach((y, j) => {
            if (j > i && x.s < y.e && y.s < x.e) msgs.push('时段重叠');
          });
        });
        return msgs;
      },
      allRows() {
        return WDS.flatMap(wd =>
          this.seats.map(seat => ({ seat, slots: this.perDay[wd][seat] })));
      },
      get hasErrors() { return this.allRows().some(r => this.validateRow(r).length); },

      addSlot(row) {
        const last = row.slots.length
          ? toMin(row.slots[row.slots.length - 1].e)
          : toMin(opts.open);
        const s = Math.max(toMin(opts.open), Math.min(last, toMin(opts.close) - 120));
        row.slots.push({ s: toHM(s), e: toHM(Math.min(s + 120, toMin(opts.close))) });
        if (this.uniformMode) this._replicate(row.seat);
      },
      removeSlot(row, i) {
        row.slots.splice(i, 1);
        if (this.uniformMode) this._replicate(row.seat);
      },
      _replicate(seat) {
        const src = this.perDay.mon[seat];
        WDS.slice(1).forEach(wd => { this.perDay[wd][seat] = src.map(x => ({ ...x })); });
      },
      copyDayToAll() {
        const src = this.activeDay;
        WDS.forEach(wd => {
          if (wd === src) return;
          this.seats.forEach(seat => { this.perDay[wd][seat] = this.perDay[src][seat].map(x => ({ ...x })); });
        });
      },
      clearDay() {
        this.seats.forEach(seat => { this.perDay[this.activeDay][seat] = []; });
      },

      /* 其他账号在当前查看天对某座位的占用时段（周天感知） */
      otherRangesFor(o, seat) {
        const v = o.seatSlots[seat];
        const day = this.uniformMode ? 'mon' : this.activeDay;
        if (Array.isArray(v)) return v;
        if (v && typeof v === 'object') return Array.isArray(v[day]) ? v[day] : [];
        return [];
      },

      /* 该座位在当前天的 30min 覆盖位图: mine/other/gap */
      coverage(seat) {
        const open = toMin(opts.open), close = toMin(opts.close), step = 30;
        const day = this.uniformMode ? 'mon' : this.activeDay;
        const mineSlots = this.perDay[day][seat];
        const bits = [];
        for (let t = open; t < close; t += step) {
          let mine = false;
          mineSlots.forEach(x => { if (toMin(x.s) <= t && t + step <= toMin(x.e)) mine = true; });
          let other = false;
          if (!mine) {
            this.others.forEach(o => this.otherRangesFor(o, seat).forEach(r => {
              const dash = r.indexOf('-');
              const s = r.slice(0, dash), e = r.slice(dash + 1);
              if (toMin(s) <= t && t + step <= toMin(e)) other = true;
            }));
          }
          bits.push(mine ? 'mine' : (other ? 'other' : 'gap'));
        }
        return bits;
      },
      gapsCount(seat) { return this.coverage(seat).filter(b => b === 'gap').length; },

      /* 把矩阵写回隐藏字段; 返回 false 表示有校验错误,调用方应阻止提交 */
      sync() {
        const seatSlots = {};
        this.seats.forEach(seat => {
          const perWd = {};
          let any = false;
          WDS.forEach(wd => {
            const list = (this.uniformMode ? this.perDay.mon[seat] : this.perDay[wd][seat])
              .map(x => x.s + '-' + x.e);
            perWd[wd] = list;
            if (list.length) any = true;
          });
          if (any) seatSlots[seat] = perWd;
        });
        const el = document.getElementById('f-seat-slots');
        if (el) el.value = JSON.stringify(seatSlots);
        return !this.hasErrors;
      },
    };
  };

  /* ===== 绑定页：星期 tab 切换（Jinja 渲染 7 个面板，Alpine 只控制显隐） ===== */
  window.dayTabs = function () {
    return {
      active: 'mon',
      wds: ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'],
      labels: { mon: '周一', tue: '周二', wed: '周三', thu: '周四', fri: '周五', sat: '周六', sun: '周日' },
    };
}

  /* ===== 绑定页：按策略重排（preview + apply） ===== */
  const WD_LABELS = { mon: '周一', tue: '周二', wed: '周三', thu: '周四', fri: '周五', sat: '周六', sun: '周日' };
  window.replanModal = function () {
    return {
      open: false,
      loading: false,
      error: '',
      strategy: 'safe',
      strategyLabel: '',
      summaryText: '',
      added: [],
      removed: [],
      unfillable: [],
      wdLabel(wd) { return WD_LABELS[wd] || wd; },
      show(plan) {
        this.strategy = plan.strategy;
        this.strategyLabel = ({ safe: '安全模式', minimal: '最简模式' })[plan.strategy] || plan.strategy;
        const s = plan.summary || {};
        this.summaryText = `新增 ${s.added_count || 0} / 移除 ${s.removed_count || 0}，影响 ${s.affected_count || 0} 个账号`;
        this.added = plan.added || [];
        this.removed = plan.removed || [];
        this.unfillable = plan.unfillable || [];
        this.open = true;
      },
      close() { this.open = false; this.error = ''; },
      async fetchPreview() {
        this.loading = true; this.error = '';
        try {
          const r = await fetch('/bindings/replan/preview', { method: 'POST' });
          if (!r.ok) throw new Error('HTTP ' + r.status);
          const j = await r.json();
          this.show(j);
        } catch (e) {
          this.error = '预览失败：' + (e && e.message || e);
          this.open = true;
        } finally {
          this.loading = false;
        }
      },
      async confirm() {
        try {
          const r = await fetch('/bindings/replan/apply', { method: 'POST' });
          if (!r.ok) throw new Error('HTTP ' + r.status);
          // fetch 已自动跟随 303：r.url 即带 ?msg= 的最终地址，赋回浏览器让 prgToast 弹提示
          location.assign(r.url);
        } catch (e) {
          this.error = '应用失败：' + (e && e.message || e);
        }
      },
      cancel() { this.close(); },
    };
  };
  /* ===== 守护账号页：查看日下拉（初值由服务端按 14:00 前今天 / 后明天预置） ===== */
  window.slotWeekPicker = function (initialWd, todayWd, tomorrowWd) {
    return {
      wd: initialWd,
      todayWd: todayWd,
      tomorrowWd: tomorrowWd,
      labels: { mon: '周一', tue: '周二', wed: '周三', thu: '周四', fri: '周五', sat: '周六', sun: '周日' },
      optionLabel(wd) {
        if (wd === this.todayWd) return '今天 · ' + this.labels[wd];
        if (wd === this.tomorrowWd) return '明天 · ' + this.labels[wd];
        return this.labels[wd];
      },
    };
  };

  /* ===== 守护账号页：时段单元格（座位 × 选中星期的药丸，当天无安排的座位整行隐藏） ===== */
  window.accountSlotCell = function (view) {
    return {
      view: view,
      activeSeats(wd) {
        return view.seats.filter((row) => {
          const v = (row.days || {})[wd];
          return v === 'full' || (Array.isArray(v) && v.length > 0);
        });
      },
    };
  };


  /* ===== 覆盖图: 数据驱动甘特 + 单元格信息卡 ===== */
  /* 首屏与 /api/dashboard-data 共用同一 JSON 形状, Alpine 响应式渲染,
   * 替代旧的 innerHTML 拼 HTML 字符串实现。30s 轮询节奏不变;
   * 信息卡打开期间跳过甘特重绘, 避免卡片被打断。 */
  const SUCCESS_STATUSES = ['active', 'signed', 'submitting', 'leaving', 'complete'];
  const CARD_STATUSES = ['active', 'signed', 'submitting', 'leaving', 'failed'];

  window.coverageGrid = function (initial) {
    return {
      today: initial.today,
      tomorrow: initial.tomorrow,
      recentLogs: initial.recent_logs || [],
      notifications: initial.notifications || [],
      bootSlots: initial.boot_slots || [],
      targetSeatCount: initial.target_seat_count || 0,
      accountCount: initial.account_count || 0,
      now: initial.now_hhmm || '',
      countdown: 12,
      busy: false,
      dismissBusy: null,
      dismissAllBusy: false,
      clockOffsetMs: null,
      timer: null,
      card: null,
      /* 首屏动画已注释停用: active 置 false + target 置满, 避免 tick/probe 被 boot 门控卡死 */
      boot: { active: false, display: 100, target: 100, stage: '就绪' },

      formatTs(ts) {
        return window.formatTs(ts);
      },
      init() {
        this.timer = setInterval(() => this.tick(), 1000);
        this._clockTimer = setInterval(() => this.tickClock(), 1000);
        // this._initBoot();
        this.refresh();
        this._onVis = () => { if (!document.hidden) this.probe(); };
        document.addEventListener('visibilitychange', this._onVis);
      },
      destroy() {
        clearInterval(this.timer);
        clearInterval(this._clockTimer);
        clearInterval(this._bootTimer);
        clearTimeout(this._bootGuard);
        document.removeEventListener('visibilitychange', this._onVis);
      },

      /* 本地秒级走字的"当前时间": 以最近一次服务器时间为基准做偏移校准,
       * 分钟级显示不再等待下一次数据刷新 */
      tickClock() {
        if (this.clockOffsetMs !== null) {
          this.now = fmtHHMM(new Date(Date.now() + this.clockOffsetMs));
        }
      },

      /* 首屏加载动画: 缓动数字向 target 爬升, 首次数据到达后放行到 100% 揭幕 */
      _initBoot() {
        this._bootTimer = setInterval(() => {
          const b = this.boot;
          if (b.display < b.target) {
            b.display = Math.min(b.target, b.display + (b.target - b.display) * 0.07 + 0.25);
            if (b.display >= 99.6 && b.target >= 100) this._finishBoot();
          }
        }, 60);
        setTimeout(() => {
          if (this.boot.target < 88) {
            this.boot.target = 88;
            this.boot.stage = '正在拉取今日与次日占用数据…';
          }
        }, 200);
        this._bootGuard = setTimeout(() => {
          if (this.boot.target < 100) {
            this.boot.target = 100;
            this.boot.stage = '网络较慢，数据稍后自动补齐';
          }
        }, 15000);
      },
      _finishBoot() {
        clearInterval(this._bootTimer);
        this.boot.display = 100;
        this.boot.stage = '就绪';
        setTimeout(() => { this.boot.active = false; }, 320);
      },
      tick() {
        if (this.busy || this.boot.active) return;
        this.countdown = Math.max(0, this.countdown - 1);
        if (this.countdown === 0) this.probe();
      },

      /* 版本探针: 纯本地轻请求; 版本没变就不拉覆盖数据、不打超星 */
      async probe() {
        if (this.busy || this.boot.active) return;
        this.countdown = 12;
        try {
          const r = await fetch('/api/version', { cache: 'no-store' });
          if (!r.ok) return;
          const j = await r.json();
          if (j.now_ms) { this.clockOffsetMs = j.now_ms - Date.now(); this.tickClock(); }
          if (this._knownV === undefined) { this._knownV = j.v; return; }
          if (j.v !== this._knownV) await this.refresh();
        } catch (e) { /* 探针失败静默, 下轮再试 */ }
      },

      cellStatus(c) {
        if (!c.accounts_info || !c.accounts_info.length) return 'empty';
        // 优先找无错误的成功任务；若全部成功任务都带 last_error，则视为失败
        for (const hit of c.accounts_info) {
          if (SUCCESS_STATUSES.includes(hit.status) && !hit.last_error) return hit.status;
        }
        // 其次若首个带错误则标红
        const hit = c.accounts_info[0];
        if (hit.last_error) return 'failed';
        return hit.status;
      },
      isSuccess(c) { return SUCCESS_STATUSES.includes(this.cellStatus(c)); },
      cellUserMark(c) { return !!c.user_reserved && !this.isSuccess(c); },
      cellOthersMark(c) { return !!c.others_occupied && !this.isSuccess(c); },
      cardHit(c) {
        const hit = (c.accounts_info && c.accounts_info[0]) || null;
        if (!hit || !hit.task_id) return null;
        // 带 last_error 的 active 视为可操作的失败态，便于续约
        const effective = hit.last_error && hit.status === 'active' ? 'failed' : hit.status;
        return CARD_STATUSES.includes(effective) ? {...hit, status: effective} : null;
      },

      openCard(day, seatNum, cell, evt) {
        const hit = this.cardHit(cell);
        if (!hit) return;
        const rect = evt.currentTarget.getBoundingClientRect();
        const width = 280, margin = 12;
        const left = Math.max(margin, Math.min(rect.left, window.innerWidth - width - margin));
        let top = rect.bottom + 8;
        if (top + 240 > window.innerHeight) top = Math.max(margin, rect.top - 248);
        this.card = { day, seatNum, cell, hit, left, top };
      },
      closeCard() { this.card = null; },

      updatedText(hit) {
        if (!hit || !hit.updated_at) return '';
        const d = new Date(hit.updated_at * 1000);
        return '更新于 ' + d.toTimeString().slice(0, 5);
      },

      /* 忽略一条看板通知: 只删本地记录, 成功后原地移除, 不打断甘特刷新节奏 */
      async dismissNotification(id) {
        if (this.dismissBusy) return;
        this.dismissBusy = id;
        try {
          const r = await fetch(`/api/notifications/${id}/dismiss`, { method: 'POST' });
          if (r.ok) this.notifications = this.notifications.filter(n => n.id !== id);
        } catch (e) { /* 失败静默, 下轮数据到达时自然恢复 */ }
        finally { this.dismissBusy = null; }
      },

      /* 全部忽略: 积压的旧告警逐条点体验极差, 一键清空 */
      async dismissAllNotifications() {
        if (this.dismissAllBusy) return;
        this.dismissAllBusy = true;
        try {
          const r = await fetch('/api/notifications/dismiss-all', { method: 'POST' });
          if (r.ok) this.notifications = [];
        } catch (e) { /* 失败静默, 下轮数据到达时自然恢复 */ }
        finally { this.dismissAllBusy = false; }
      },

      async refresh(fresh = false) {
        if (this.card) { this.countdown = 10; return; }
        this.busy = true;
        try {
          const r = await fetch('/api/dashboard-data' + (fresh ? '?fresh=1' : ''), { cache: 'no-store' });
          if (!r.ok) throw new Error('HTTP ' + r.status);
          const j = await r.json();
          this.today = j.today || this.today;
          this.tomorrow = j.tomorrow || this.tomorrow;
          this.recentLogs = j.recent_logs || [];
          this.notifications = j.notifications || [];
          this.now = j.now_hhmm || this.now;
          if (j.now_ms) this.clockOffsetMs = j.now_ms - Date.now();
          if (this.boot.target < 100) this.boot.stage = '核对座位覆盖…';
        } catch (e) {
          console.warn('dashboard refresh failed:', e);
        } finally {
          this.countdown = 12;
          this.busy = false;
          if (this.boot.target < 100) this.boot.target = 100;
          this._knownV = undefined;  // 拉取后重置基线, 由下轮探针重新对齐
        }
      },
    };
  };
  /* ===== 任务看板: 按状态分列 ===== */
  const BOARD_COLUMNS = [
    { key: 'todo', title: '待执行', statuses: ['pending', 'ready'], tone: 'chip-muted' },
    { key: 'running', title: '进行中', statuses: ['submitting', 'active', 'signed', 'leaving'], tone: 'chip-progress' },
    { key: 'failed', title: '失败', statuses: ['failed'], tone: 'chip-danger' },
    { key: 'done', title: '已完成', statuses: ['complete'], tone: 'chip-success' },
  ];

  window.tasksBoard = function (initial) {
    return {
      day: initial.day,
      tasks: initial.tasks || [],
      columns: BOARD_COLUMNS,
      loading: false,

      ofCol(col) {
        return this.tasks
          .filter(t => col.statuses.includes(t.status))
          .sort((a, b) => a.start.localeCompare(b.start) || a.seat_num.localeCompare(b.seat_num));
      },
      count(col) { return this.ofCol(col).length; },
      toneOf(t) {
        const col = this.columns.find(c => c.statuses.includes(t.status));
        return col ? col.tone : 'chip-muted';
      },
      async refresh() {
        this.loading = true;
        try {
          const r = await fetch('/api/tasks?day=' + this.day, { cache: 'no-store' });
          if (r.ok) { const j = await r.json(); this.tasks = j.tasks || []; }
        } catch (e) {
          console.warn('tasks refresh failed:', e);
        } finally { this.loading = false; }
      },
    };
  };

  /* ===== 自动托管页：标签切换 + 刷新节流 ===== */
  window.hostingBoard = function (initial) {
    return {
      tab: initial.initialTab || 'current',
      page: initial.page || 1,
      hasPrev: !!initial.hasPrev,
      hasNext: !!initial.hasNext,
      refreshing: false,
    };
  };
  window.formatTs = function (ts) {
    if (!ts) return '';
    const d = new Date(ts);
    if (isNaN(d.getTime())) return '';
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  };

  /* ===== 日志页：按日期分页与前端级别即时过滤 ===== */
  window.logsFilter = function (initialLevel = '', currentDay = '', prevDay = '', nextDay = '', todayStr = '') {
    return {
      level: initialLevel,
      currentDay: currentDay,
      prevDay: prevDay,
      nextDay: nextDay,
      todayStr: todayStr,
      setLevel(lv) {
        this.level = lv;
        const url = new URL(window.location.href);
        if (lv) url.searchParams.set('level', lv);
        else url.searchParams.delete('level');
        window.history.replaceState({}, '', url.toString());
      },
      goToDay(day) {
        if (!day) return;
        const url = new URL(window.location.href);
        url.searchParams.set('day', day);
        if (this.level) url.searchParams.set('level', this.level);
        else url.searchParams.delete('level');
        window.location.href = url.toString();
      }
    };
  };

  /* ===== 移动端侧栏开关 ===== */
  window.mobileNav = function () {
    return {
      open: false,
      toggle() { this.open = !this.open; },
      close() { this.open = false; },
      init() {
        // Escape 关闭抽屉（仅在打开时监听，避免与确认弹层/输入框冲突）
        document.addEventListener('keydown', (e) => {
          if (e.key === 'Escape' && this.open) {
            e.preventDefault();
            this.close();
          }
        });
        // 监听抽屉状态变化，body 滚动锁 + aria
        this.$watch('open', (v) => {
          if (v) {
            document.body.style.overflow = 'hidden';
            this.$root.setAttribute('data-mobile-nav', 'open');
          } else {
            document.body.style.overflow = '';
            this.$root.setAttribute('data-mobile-nav', 'closed');
          }
        });
      },
    };
  };

  /* ===== 系统设置页：检测表单变更，底部保存按钮滚出视口时才浮出悬浮保存条 ===== */
  window.settingsDirty = function () {
    return {
      dirty: false,
      saveVisible: true,
      init() {
        const form = this.$root.querySelector('form[action="/settings"]');
        if (!form) return;
        // FormData 无自定义 toString，需用 URLSearchParams 序列化才能得到稳定可比的字符串
        const snapshot = () => new URLSearchParams(new FormData(form)).toString();
        const baseline = snapshot();
        form.addEventListener('input', () => { this.dirty = snapshot() !== baseline; });
        form.addEventListener('change', () => { this.dirty = snapshot() !== baseline; });
        form.addEventListener('submit', () => { this.dirty = false; });
        const anchor = document.getElementById('save-anchor');
        if (anchor && 'IntersectionObserver' in window) {
          new IntersectionObserver((entries) => {
            this.saveVisible = entries[entries.length - 1].isIntersecting;
          }, { threshold: 0.1 }).observe(anchor);
        }
      },
    };
  };

  /* 页面加载后跑 PRG toast + 覆盖条横滚提示自动隐藏 */
  document.addEventListener('DOMContentLoaded', () => {
    if (window.prgToast) window.prgToast();
    /* 覆盖图横滚条：.gantt-strip 是滚动元素，渐隐与提示挂在外层 wrap */
    document.querySelectorAll('.gantt-strip').forEach((strip) => {
      const wrap = strip.closest('.gantt-strip-wrap');
      const hint = wrap ? wrap.querySelector('.gantt-strip-hint') : null;
      const inner = strip.querySelector('.gantt-strip-inner');
      if (!wrap || !inner) return;
      let scrolledAway = false;
      /* 双向判定：内容宽度随 Alpine 渲染变化，铺满则收提示与渐隐，变宽则恢复 */
      const settle = () => {
        if (strip.scrollWidth <= strip.clientWidth + 1) {
          wrap.classList.add('is-full');
          if (hint) hint.style.display = 'none';
        } else {
          wrap.classList.remove('is-full');
          if (hint && !scrolledAway) {
            hint.style.display = '';
            hint.style.opacity = '';
          }
        }
      };
      const onScroll = () => {
        if (strip.scrollLeft > 4 && hint) {
          scrolledAway = true;
          hint.style.transition = 'opacity .3s';
          hint.style.opacity = '0';
          strip.removeEventListener('scroll', onScroll);
        }
      };
      strip.addEventListener('scroll', onScroll, { passive: true });
      requestAnimationFrame(settle);
      /* Alpine 渲染行数据后宽度才会到位，监听内容尺寸变化后复检 */
      if ('ResizeObserver' in window) {
        new ResizeObserver(settle).observe(inner);
      } else {
        setTimeout(settle, 1500);
        setTimeout(settle, 4000);
      }
    });
  });
})();
