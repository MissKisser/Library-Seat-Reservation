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
  // 否则保持 HTML 标签的默认 data-theme="dark"

  window.themeInit = function () {
    return {
      isDark: root.getAttribute('data-theme') !== 'light',
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
      if (map[k]) { window.showToast(map[k]); shown = true; }
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
          // 通过事件触发对应行的 verifyButton；若行不存在则直接 fetch
          const btn = document.querySelector(`[data-verify-id="${id}"]`);
          if (btn && btn._x_dataStack) {
            // 触发该行 Alpine 组件的 run
            btn.dispatchEvent(new CustomEvent('verify-run'));
          } else {
            try {
              const r = await fetch(`/accounts/${id}/test-login`, { method: 'POST' });
              const j = await r.json().catch(() => ({}));
              if (!(r.ok && j.ok)) this.failedIds.push(id);
            } catch (e) { this.failedIds.push(id); }
          }
          this.done++;
          await new Promise(res => setTimeout(res, 400)); // 串行间隔，避免 Playwright 并发
        }
        this.running = false;
        const ok = this.total - this.failedIds.length;
        window.showToast({
          type: this.failedIds.length ? 'warn' : 'success',
          title: '批量验证完成',
          desc: `${ok} 通过，${this.failedIds.length} 失败`,
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

  /* ===== 座位图：轮询渲染 ===== */
  window.seatMap = function (roomId) {
    return {
      roomId, sections: [], error: null, loading: true,
      init() {
        this.load();
        this.timer = setInterval(() => this.load(), 30000);
      },
      destroy() { clearInterval(this.timer); },
      async load() {
        try {
          const r = await fetch(`/api/seats/${this.roomId}`, { cache: 'no-store' });
          const j = await r.json().catch(() => ({}));
          if (j.error) { this.error = j.error; this.sections = []; }
          else if (j.by_target) {
            this.error = null;
            this.sections = Object.entries(j.by_target).map(([seat, seats]) => ({
              seat, seats: seats || [],
            }));
          } else {
            this.error = '无数据';
            this.sections = [];
          }
        } catch (e) {
          this.error = '获取座位失败';
        } finally {
          this.loading = false;
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
