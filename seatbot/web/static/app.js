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

  /* ===== 顶栏时钟：轮询 /api/status ===== */
  window.topbarClock = function () {
    return {
      now: '--:--', nextAt: '--:--', nextAcc: '—',
      init() { this.tick(); setInterval(() => this.tick(), 30000); },
      async tick() {
        try {
          const r = await fetch('/api/status', { cache: 'no-store' });
          if (!r.ok) return;
          const j = await r.json();
          this.now = fmtHHMM(new Date(j.now));
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
    [/(risk|风控|vc3|auth cookies|cookie)/i, '登录被风控拦截，可能需手动登录'],
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

  /* ===== 账号表单：full/custom 切换 ===== */
  window.slotsToggle = function (initial = 'full') {
    return {
      mode: initial,
      isCustom() { return this.mode === 'custom'; },
    };
  };

  /* ===== 座位平面图: 只读渲染 ===== */
  /* layout 为 config.library.seats_layout (行数组, null=过道);
   * 未配置时按座位编号排序单行兜底。 */
  window.seatFloor = function (roomId, layout) {
    return {
      roomId,
      layout,
      sections: [],                 // [{seat, byNum: {座位号: seatObj}}]
      error: null, loading: true, fetching: false,

      init() {
        this.load();
        this.timer = setInterval(() => this.load(), 30000);
      },
      destroy() { clearInterval(this.timer); },
      async load() {
        /* 上一轮未完成 (登录重试最长 ~30s) 时跳过本轮, 防止请求堆积 */
        if (this.fetching) return;
        this.fetching = true;
        try {
          const r = await fetch(`/api/seats/${this.roomId}`, { cache: 'no-store' });
          const j = await r.json().catch(() => ({}));
          if (j.error) { this.error = j.error; this.sections = []; }
          else if (j.by_target) {
            this.error = null;
            this.sections = Object.entries(j.by_target).map(([seat, seats]) => {
              const byNum = {};
              (seats || []).forEach(s => { byNum[s.seat_num] = s; });
              return { seat, byNum, rows: null };
            });
            this.sections.forEach(sec => { sec.rows = this.rowsFor(sec); });
          } else {
            this.error = '无数据';
            this.sections = [];
          }
        } catch (e) {
          this.error = '获取座位失败';
        } finally {
          this.loading = false;
          this.fetching = false;
        }
      },

      rowsFor(sec) {
        if (this.layout && this.layout.length) {
          return this.layout.map(row => row.map(
            n => (n === null ? null : (sec.byNum[n] || { seat_num: n, missing: true }))
          ));
        }
        const nums = Object.keys(sec.byNum).sort();
        return [nums.map(n => sec.byNum[n])];
      },
      seatClasses(s) {
        if (!s || s.missing) return 'bg-app-muted/60 border-edge-subtle text-ink-muted';
        if (s.error) return 'bg-danger-soft border-danger/30 text-danger';
        if (s.is_target) return 'bg-target-soft border-2 border-target text-target font-bold';
        if (s.occupied === true) return 'bg-danger/70 border-danger/70 text-white';
        return 'bg-app-muted border-edge-subtle text-ink-secondary';
      },
      seatTitle(s) {
        if (!s) return '过道';
        if (s.missing) return s.seat_num + '（不在查询范围）';
        if (s.error) return s.seat_num + ' · ' + s.error;
        if (s.occupied === true) return s.seat_num + ' · 被 ' + (s.occupier_uid || '?') + ' 占用';
        return s.seat_num + ' · 空闲';
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
      targetSeatCount: initial.target_seat_count || 0,
      accountCount: initial.account_count || 0,
      now: initial.now_hhmm || '',
      countdown: 30,
      busy: false,
      timer: null,
      card: null,

      init() {
        this.timer = setInterval(() => this.tick(), 1000);
      },
      destroy() { clearInterval(this.timer); },
      tick() {
        if (this.busy) return;
        this.countdown = Math.max(0, this.countdown - 1);
        if (this.countdown === 0) this.refresh();
      },
      formatCountdown() { return '下次刷新 ' + this.countdown + 's'; },

      cellStatus(c) {
        const hit = c.accounts_info && c.accounts_info[0];
        return hit ? hit.status : 'empty';
      },
      isSuccess(c) { return SUCCESS_STATUSES.includes(this.cellStatus(c)); },
      cellUserMark(c) { return !!c.user_reserved && !this.isSuccess(c); },
      cellOthersMark(c) { return !!c.others_occupied && !this.isSuccess(c); },
      cardHit(c) {
        const hit = (c.accounts_info && c.accounts_info[0]) || null;
        return hit && hit.task_id && CARD_STATUSES.includes(hit.status) ? hit : null;
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

      async refresh() {
        if (this.card) { this.countdown = 10; return; }
        this.busy = true;
        try {
          const r = await fetch('/api/dashboard-data', { cache: 'no-store' });
          if (!r.ok) throw new Error('HTTP ' + r.status);
          const j = await r.json();
          this.today = j.today || this.today;
          this.tomorrow = j.tomorrow || this.tomorrow;
          this.recentLogs = j.recent_logs || [];
          this.now = j.now_hhmm || this.now;
        } catch (e) {
          console.warn('dashboard refresh failed:', e);
        } finally {
          this.countdown = 30;
          this.busy = false;
        }
      },
    };
  };
  /* ===== 移动端侧栏开关 ===== */
  window.mobileNav = function () {
    return { open: false, toggle() { this.open = !this.open; } };
  };

  /* 页面加载后跑 PRG toast */
  document.addEventListener('DOMContentLoaded', () => {
    if (window.prgToast) window.prgToast();
  });
})();
