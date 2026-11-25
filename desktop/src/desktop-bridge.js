/* TripoSR-Lite 桌面壳加载页桥接。
 * - Tauri 环境：直接复用 withGlobalTauri 暴露的 window.__TAURI__（core.invoke / event.listen）。
 * - 浏览器直开 index.html：全部降级为 no-op，页面显示「请通过桌面应用启动」。 */
(function () {
  "use strict";
  var tauri = window.__TAURI__;
  var isTauri = !!(tauri && tauri.core && typeof tauri.core.invoke === "function");

  window.__TRIPOSR_DESKTOP__ = {
    isTauri: isTauri,
    invoke: function (command, args) {
      if (!isTauri) return Promise.reject(new Error("浏览器模式无桌面桥"));
      return tauri.core.invoke(command, args);
    },
    listen: function (event, handler) {
      if (!isTauri) return Promise.resolve(function () {});
      return tauri.event.listen(event, handler);
    },
  };
})();
