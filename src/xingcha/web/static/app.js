// 后台的全部 JS。**必须是外部文件。**
//
// CSP 是 `script-src 'self'`（无 nonce、无 unsafe-inline），所以内联 <script> 与
// 内联事件处理器（onclick= / onsubmit=）都会被浏览器拒绝执行。
//
// 这不是理论问题：v0.4 之前这段代码写在 base.html 的内联 <script> 里，两处危险操作
// 用的是 onsubmit="return confirm(...)"。结果**复制按钮完全无反应、危险操作的二次
// 确认根本不弹**——而密钥页同时写着「这是唯一一次看到明文」。
//
// 测试守住了 CSP 头（断言没有 unsafe-inline），但没人拿页面去对那个头。
// tests/test_admin.py 里现在有一条反向断言：模板里不许出现内联脚本或 on* 属性。

(() => {
  'use strict';

  // --- 复制到剪贴板：一次性密钥展示处用 ---
  document.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-copy]');
    if (!btn) return;
    const restore = btn.textContent;
    try {
      await navigator.clipboard.writeText(btn.dataset.copy);
      btn.textContent = '已复制';
      setTimeout(() => { btn.textContent = restore; }, 1500);
    } catch {
      // http 下 navigator.clipboard 不可用（安全上下文限制）。回落到选中文本，
      // 让用户至少能一次 Ctrl+C 拿走——比一个没反应的按钮好。
      const target = document.getElementById(btn.dataset.copyTarget || '');
      if (target) {
        const range = document.createRange();
        range.selectNodeContents(target);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        btn.textContent = '已选中，按 Ctrl+C';
      } else {
        btn.textContent = '复制失败，请手动选中';
      }
      setTimeout(() => { btn.textContent = restore; }, 3000);
    }
  });

  // --- 换进 <dialog> 里的内容自动弹出 ---
  //
  // 一条通用规则，而不是给每个 id 写一条：**htmx 的 target 落在哪个 <dialog> 里，
  // 就弹哪个**。加一处新的"结果弹窗"只需要在模板里包一层 dialog，不用回来改 JS。
  //
  // 用 <dialog> 而不是自己糊遮罩：焦点陷阱、Esc 关闭、::backdrop 都是白拿的，
  // 而这三样自己实现都容易漏——尤其是 Esc 和焦点，键盘用户会直接被困住。
  document.addEventListener('htmx:afterSwap', (e) => {
    const dlg = e.target && e.target.closest && e.target.closest('dialog');
    if (!dlg) return;
    // 一次只能有一个 modal：先关掉别的（比如提交前那个密码弹窗），再弹这个。
    for (const other of document.querySelectorAll('dialog[open]')) {
      if (other !== dlg) other.close();
    }
    if (!dlg.open) dlg.showModal();
  });

  // --- 弹窗的开与关 ---
  //
  // 开：``data-open-dialog="#选择器"``，按钮是 type=button（不是 submit），
  //     所以点它不提交表单——表单等弹窗里那个 submit 才走。
  // 关：``data-close-dialog``，放在弹窗里的取消/知道了上。
  document.addEventListener('click', (e) => {
    const closer = e.target.closest('[data-close-dialog]');
    if (closer) {
      e.preventDefault();
      const dlg = closer.closest('dialog');
      if (dlg) dlg.close();
      return;
    }

    const opener = e.target.closest('[data-open-dialog]');
    if (!opener) return;
    e.preventDefault();

    // 先校验**弹窗之外**的字段，缺东西就别弹窗，把原生提示打在那个输入上。
    //
    // 不能用 form.reportValidity()：它会把弹窗里那个 required 的密码框也算进去，
    // 而关闭的 <dialog> 里的字段不可聚焦——浏览器既报不出提示、又判定校验失败，
    // 于是弹窗永远打不开，点按钮**完全没反应**。踩过。
    const form = opener.closest('form');
    if (form) {
      for (const el of form.querySelectorAll('input, select, textarea')) {
        if (el.closest('dialog')) continue;
        if (!el.checkValidity()) {
          el.reportValidity();
          return;
        }
      }
    }
    const dlg = document.querySelector(opener.dataset.openDialog);
    if (dlg && !dlg.open) dlg.showModal();
  });

  // --- 危险操作二次确认 ---
  //
  // 用 data-confirm 而不是 onsubmit=：后者是内联处理器，被 CSP 挡掉之后表单会**静默
  // 直接提交**——比没有确认更糟，因为界面看起来像是有保护。
  //
  // **必须注册在防重复提交之前**：同一事件的监听器按注册顺序执行，用户点"取消"时
  // 这里 preventDefault，下面那个看到 defaultPrevented 就不会去禁用按钮。
  // 顺序反了的话，取消一次之后按钮就永久禁用了。
  document.addEventListener('submit', (e) => {
    const form = e.target.closest('form[data-confirm]');
    if (!form) return;
    if (!window.confirm(form.dataset.confirm)) {
      e.preventDefault();
    }
  });

  // --- 防重复提交 ---
  //
  // 每个改状态的表单都可能被点两次：第一次点完页面还在原样（要等一次往返），
  // 而"没反应"的自然反应就是再点一下。后果不是抽象的：签两把密钥、加两个同名
  // 供应商、切两次上游（第二次会用上一次的旧值再写一遍）。
  //
  // 做法是**提交后立刻禁用提交控件**，而不是拦第二次 submit 事件——禁用是用户
  // 看得见的反馈，"点了没反应"本身就是重复点击的原因。
  //
  // 有三处必须小心：
  //   1. 带 data-confirm 的表单在用户点"取消"之后不能被禁用（那次没提交）；
  //   2. htmx 的表单不走原生 submit，由下面的 htmx 事件单独处理；
  //   3. 浏览器"后退"回到页面时会用缓存的 DOM（bfcache），按钮还是禁用的 ——
  //      所以 pageshow 里要恢复，否则用户后退回来发现表单点不了。
  const lock = (form) => {
    for (const el of form.querySelectorAll('button[type=submit], input[type=submit]')) {
      if (el.disabled) continue;
      el.disabled = true;
      el.dataset.lockedByUs = '1';
      if (el.tagName === 'BUTTON' && !el.querySelector('.htmx-indicator')) {
        el.dataset.labelBefore = el.textContent;
        el.textContent = '处理中…';
      }
    }
  };
  const unlockAll = () => {
    for (const el of document.querySelectorAll('[data-locked-by-us]')) {
      el.disabled = false;
      delete el.dataset.lockedByUs;
      if (el.dataset.labelBefore !== undefined) {
        el.textContent = el.dataset.labelBefore;
        delete el.dataset.labelBefore;
      }
    }
  };

  document.addEventListener('submit', (e) => {
    // 走到这里说明确认那一关已经过了（下面那个监听器在 preventDefault 时会阻断）
    if (e.defaultPrevented) return;
    const form = e.target.closest('form');
    if (form && !form.hasAttribute('hx-post')) lock(form);
  });

  // htmx 的表单不触发原生 submit，用它自己的事件。
  document.addEventListener('htmx:beforeRequest', (e) => {
    const form = e.target.closest && e.target.closest('form');
    if (form) lock(form);
  });
  document.addEventListener('htmx:afterRequest', unlockAll);
  // bfcache：后退回来时 DOM 是缓存的，按钮还禁着
  window.addEventListener('pageshow', unlockAll);

})();
