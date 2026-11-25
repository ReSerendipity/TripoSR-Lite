//! 主窗口：加载页起步、外部链接拦截、桥接脚本注入。

use tauri::{AppHandle, Manager, WebviewWindow, WebviewWindowBuilder};

/// 主窗口标签（capabilities/main.json 的 windows 名单同步）
pub const MAIN_LABEL: &str = "main";
/// 空闲标题
pub const IDLE_TITLE: &str = "TripoSR-Lite 单图重建工作台";
/// 前端桥接脚本（编译期内嵌，运行时以 initialization_script 注入）
pub const BRIDGE_JS: &str = include_str!("../../src/desktop-bridge.js");

/// 允许在 WebView 内导航的 URL：Tauri 内部协议 + 本机回环后端。
pub fn is_internal_url(url: &tauri::Url) -> bool {
    match url.scheme() {
        "tauri" | "about" | "ipc" | "data" => true,
        "http" | "https" => matches!(
            url.host_str(),
            Some("127.0.0.1") | Some("localhost") | Some("tauri.localhost") | None
        ),
        _ => false,
    }
}

/// 外部链接交给系统默认浏览器。
pub fn open_external(url: &str) {
    #[cfg(windows)]
    {
        let _ = std::process::Command::new("rundll32.exe")
            .arg("url.dll,FileProtocolHandler")
            .arg(url)
            .spawn();
    }
    #[cfg(not(windows))]
    {
        let _ = std::process::Command::new("xdg-open").arg(url).spawn();
    }
}

/// 构建主窗口：先显示壳自带加载页，后端就绪后由 main.rs 导航到本机服务。
pub fn build_main_window(app: &AppHandle) -> tauri::Result<WebviewWindow> {
    WebviewWindowBuilder::new(app, MAIN_LABEL, tauri::WebviewUrl::App("index.html".into()))
        .title(IDLE_TITLE)
        .inner_size(1360.0, 860.0)
        .min_inner_size(1080.0, 720.0)
        .center()
        .resizable(true)
        .decorations(true)
        .visible(false)
        .initialization_script(BRIDGE_JS)
        .on_navigation(|url| {
            if is_internal_url(url) {
                true
            } else {
                open_external(url.as_str());
                false
            }
        })
        .on_new_window(|url, _features| {
            open_external(url.as_str());
            tauri::webview::NewWindowResponse::Deny
        })
        .build()
        .inspect(|win| {
            let _ = win.show();
        })
}

/// 显示并聚焦主窗口（二次启动转到已运行实例）。
pub fn show_and_focus(app: &AppHandle) {
    if let Some(win) = app.get_webview_window(MAIN_LABEL) {
        let _ = win.unminimize();
        let _ = win.show();
        let _ = win.set_focus();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn local_and_tauri_urls_are_internal() {
        assert!(is_internal_url(&tauri::Url::parse(
            "tauri://localhost/index.html"
        )
        .unwrap()));
        assert!(is_internal_url(&tauri::Url::parse("http://127.0.0.1:8321/").unwrap()));
        assert!(is_internal_url(&tauri::Url::parse("http://localhost:5173/").unwrap()));
        assert!(is_internal_url(&tauri::Url::parse("about:blank").unwrap()));
    }

    #[test]
    fn external_urls_are_blocked() {
        assert!(!is_internal_url(&tauri::Url::parse("https://github.com/x").unwrap()));
        assert!(!is_internal_url(&tauri::Url::parse("http://8.8.8.8/").unwrap()));
    }

    #[test]
    fn window_title_is_product_name() {
        assert_eq!(IDLE_TITLE, "TripoSR-Lite 单图重建工作台");
    }
}
