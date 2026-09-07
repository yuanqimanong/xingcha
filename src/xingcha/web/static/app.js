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

  // --- 危险操作二次确认 ---
  //
  // 用 data-confirm 而不是 onsubmit=：后者是内联处理器，被 CSP 挡掉之后表单会**静默
  // 直接提交**——比没有确认更糟，因为界面看起来像是有保护。
  document.addEventListener('submit', (e) => {
    const form = e.target.closest('form[data-confirm]');
    if (!form) return;
    if (!window.confirm(form.dataset.confirm)) {
      e.preventDefault();
    }
  });
})();
