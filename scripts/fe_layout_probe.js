// 布局几何探针：对当前页跑一遍 R1–R9 口径，返回单行 JSON。
// 用法：playwright-cli -s=layout eval "$(cat scripts/fe_layout_probe.js)"
(() => {
  const q = s => document.querySelector(s);
  const rect = el => (el ? el.getBoundingClientRect() : null);
  // 滚动容器：底座是**内容区** el-scrollbar 的 wrap，不是 window。
  // 裸 `.el-scrollbar__wrap` 会命中侧边栏（DOM 序第一，实测 scrollHeight 恒为菜单
  // 高 250/350），把「整页滚不滚」读成侧边栏读数；表格体、设置面板也各有 wrap。
  // 内容区 wrap 的唯一特征：它包着路由页面根 .main-content（lay-content 挂的类），
  // fixedHeader 真/假两种布局都命中这一个，其它 wrap 都不命中。
  const wrap =
    [...document.querySelectorAll(".el-scrollbar__wrap")].find(w =>
      w.querySelector(".main-content")
    ) || null;
  const view = wrap ? wrap.querySelector(":scope > .el-scrollbar__view") : null;
  const card = q(".el-card");
  const table = q(".el-table");
  const pager = q(".el-pagination");
  const band = q(".kk-band");
  const cols = [...document.querySelectorAll(".kk-side > .kk-col")].map(el => {
    const r = rect(el);
    return { left: Math.round(r.left), width: Math.round(r.width) };
  });
  const hits = [
    [960, 700],
    [960, 900],
    [960, 1050]
  ].map(([x, y]) => !!document.elementFromPoint(x, y));
  let scrollProbe = null;
  if (wrap) {
    const before = wrap.scrollTop;
    wrap.scrollTop = 600;
    scrollProbe = wrap.scrollTop;
    wrap.scrollTop = before;
  }
  return JSON.stringify({
    path: location.hash || location.pathname,
    pageScrolls: wrap && view ? view.scrollHeight > wrap.clientHeight : null,
    scrollHeights: view && wrap
      ? { scroll: view.scrollHeight, client: wrap.clientHeight }
      : null,
    canScroll: scrollProbe === null ? null : scrollProbe > 0,
    tableRect: rect(table),
    pagerRect: rect(pager),
    bandTop: band ? Math.round(rect(band).top) : null,
    stickyTop: (() => {
      const s = q(".kk-sticky-bar");
      return s ? Math.round(rect(s).top) : null;
    })(),
    sideCols: cols,
    reachableAt: hits,
    cardOverflow: card ? getComputedStyle(card).overflow : null
  });
})()
