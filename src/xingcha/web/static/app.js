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

  // --- summary 里的按钮不要顺手把卡片折起来 ---
  //
  // <summary> 内任何位置的点击都会切换 details，包括点在按钮或链接上。「输出保证」
  // 那张卡的标题栏里有「检查字段命名」——不拦的话点一次 lint 就把整张卡折叠了，
  // 而结果正好渲染在被折起来的那半边，看起来像"点了没反应"。
  //
  // **只挡 button[type=button]。** 更宽的选择器（a / input / label / submit）会
  // 连带取消它们自己的默认动作——preventDefault 取消的是整个激活行为，不是只取消
  // 折叠。拿 submit 按钮举例：卡片不折了，表单也不提交了。type=button 没有默认
  // 动作可丢，是唯一一类挡了没有副作用的。
  document.addEventListener('click', (e) => {
    const hit = e.target.closest('button[type=button]');
    if (hit && hit.closest('summary')) e.preventDefault();
  });

  // --- 危险操作二次确认 ---
  //
  // 用 data-confirm 而不是 onsubmit=：后者是内联处理器，被 CSP 挡掉之后表单会**静默
  // 直接提交**——比没有确认更糟，因为界面看起来像是有保护。
  //
  // 用站内的 <dialog> 而不是 window.confirm()：原生框长得不像这个后台、文案不能
  // 排版、在自动化里被静默当成"取消"，而且 Chrome 允许用户勾"不再显示"——**勾掉
  // 之后所有危险操作就再没有确认了，而界面上看不出区别**。
  //
  // 原生 confirm 是同步的，这个不是，所以流程变成三步：拦下提交 → 弹窗 → 用户点
  // 确定后**用原来的提交者重新提交一次**。重新提交会再触发一次 submit，靠 armed
  // 这个集合放行——不放行的话就是一个永远弹窗的死循环。
  //
  // **必须注册在防重复提交之前**：同一事件的监听器按注册顺序执行，用户点"取消"时
  // 这里 preventDefault，下面那个看到 defaultPrevented 就不会去禁用按钮。
  // 顺序反了的话，取消一次之后按钮就永久禁用了。
  const dlg = document.getElementById('confirm-dialog');
  const armed = new WeakSet();

  document.addEventListener('submit', (e) => {
    const form = e.target.closest('form');
    if (!form) return;

    // **两个位置都认：表单上的，和被点那个按钮上的。**
    //
    // 只认 form[data-confirm] 的时候，写在 <button data-confirm> 上的那些会被
    // 静默忽略——没有报错、没有确认框，按钮直接生效。上游切换页就是这么漏的，
    // 而那是全站最危险的一个动作（它能让所有 Agent 一起开始报错）。
    const text =
      (e.submitter && e.submitter.dataset && e.submitter.dataset.confirm) ||
      form.dataset.confirm;
    if (!text) return;

    // 已经确认过的那一次，放行并把标记清掉（下次再点还要再确认一遍）。
    if (armed.has(form)) {
      armed.delete(form);
      return;
    }

    e.preventDefault();

    // 没有弹窗节点（老模板、或 JS 先于 DOM 跑）就退回原生的，别把功能锁死。
    if (!dlg) {
      if (window.confirm(text)) {
        armed.add(form);
        form.requestSubmit(e.submitter || undefined);
      }
      return;
    }

    // **记住提交者**。表单里有多个 submit 时，提交的 name=value 来自被点的那个，
    // requestSubmit() 不带它就会丢掉——症状是"点了另一个按钮的效果"。
    const submitter = e.submitter;
    dlg.querySelector('#confirm-text').textContent = text;
    dlg.dataset.pending = '1';

    const done = (go) => {
      dlg.removeEventListener('close', onClose);
      delete dlg.dataset.pending;
      dlg.close();
      if (!go) return;
      armed.add(form);
      form.requestSubmit(submitter || undefined);
    };
    const onClose = () => done(false);          // Esc 或点 backdrop 关掉 = 取消
    dlg.addEventListener('close', onClose, { once: true });

    dlg.querySelector('[data-confirm-ok]').onclick = () => done(true);
    dlg.querySelector('[data-confirm-cancel]').onclick = () => done(false);

    dlg.showModal();
    // 焦点落在"取消"上：危险操作的默认答案是"不做"，一个回车不该把东西删掉。
    dlg.querySelector('[data-confirm-cancel]').focus();
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
    // [data-lock] 让非 submit 的按钮也进锁。「试运行」是 type=button（它不提交
    //   表单），但它会真的调一次上游、真的花钱——双击就是花两份。
    for (const el of form.querySelectorAll('button[type=submit], input[type=submit], [data-lock]')) {
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
    if (!form || form.hasAttribute('hx-post')) return;
    // **必须延后一拍再禁用。** 表单的提交数据（entry list）是在 submit 事件之后
    // 才构造的，而构造时会跳过所有 disabled 控件——**包括点下去的那个按钮本身**。
    // 于是"在 submit 里立刻禁用"会把提交者的 name=value 一起丢掉。
    //
    // 大多数表单看不出来（数据在 input 里），但侧栏的主题切换整份 payload 就是
    // 按钮上的 name="value"：点「亮」/「暗」提交上去没有 value，后端 422，
    // 页面变成一段 JSON 报错。setTimeout 0 让浏览器先把数据收好再禁用。
    setTimeout(() => lock(form), 0);
  });

  // htmx 的表单不触发原生 submit，用它自己的事件。
  document.addEventListener('htmx:beforeRequest', (e) => {
    const form = e.target.closest && e.target.closest('form');
    if (form) lock(form);
  });
  document.addEventListener('htmx:afterRequest', unlockAll);

  // ---------------------------------------------------------------- 少样本示例
  //
  // 组数不定，所以用 <template> 克隆而不是先渲染 N 个空槽再隐藏：隐藏的 textarea
  // **仍然会被提交**，于是后端收到一串空示例，还得反过来猜哪些是用户真填的。
  // <template> 里的内容不在表单里，克隆出来才算数。
  document.addEventListener('click', (e) => {
    const add = e.target.closest('[data-add-example]');
    if (add) {
      const list = document.querySelector(add.dataset.addExample);
      const tpl = document.getElementById('example-tpl');
      if (!list || !tpl) return;
      list.appendChild(tpl.content.cloneNode(true));
      const last = list.lastElementChild;
      if (last) last.querySelector('textarea')?.focus();
      return;
    }
    const drop = e.target.closest('[data-remove-example]');
    if (drop) drop.closest('.example')?.remove();
  });

  // ------------------------------------------------------------------ 试运行
  //
  // Ctrl/⌘ + Enter 在测试输入框里直接跑。这一步会被反复做几十次（改一句提示词、
  // 再跑一次），每次都去够鼠标是这一页最大的一处磨损。
  //
  // 只绑在那一个 textarea 上，不做全局快捷键：全局的会在别人写 schema 时误触发，
  // 而那一次误触发是要花钱的。
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || !(e.ctrlKey || e.metaKey)) return;
    if (e.target?.id !== 'test_input') return;
    e.preventDefault();
    document.getElementById('run-test')?.click();
  });
  // bfcache：后退回来时 DOM 是缓存的，按钮还禁着
  window.addEventListener('pageshow', unlockAll);

  // --- 给 schema 框填一个样板 ---
  //
  // 从零手写 JSON Schema 是 Agent 编辑页最劝退的一步：空框 + 一句"顶层必须是
  // object"，不写过的人不知道从哪儿下第一笔。给一段能直接改的，比再写三行说明有用。
  //
  // **不覆盖已有内容**：手滑点一下就把写好的 schema 冲掉，那是不可撤销的。
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-fill-schema]');
    if (!btn) return;
    const ta = document.getElementById('output_schema');
    if (!ta) return;
    if (ta.value.trim()) {
      btn.textContent = '框里已经有内容了';
      setTimeout(() => { btn.textContent = '填一个样板'; }, 2000);
      return;
    }
    ta.value = JSON.stringify(
      {
        type: 'object',
        properties: {
          标题: { type: 'string', description: '一句话概括，25 字以内' },
          标签: {
            type: 'array',
            description: '2 到 3 个',
            items: { type: 'string', enum: ['分析', '竞争', '监管', '市场'] },
            minItems: 2,
            maxItems: 3,
          },
        },
        required: ['标题', '标签'],
        additionalProperties: false,
      },
      null,
      2,
    );
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    ta.focus();
  });

})();
