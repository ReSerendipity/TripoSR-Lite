fn main() {
    // Tauri 2.11.2+：主窗口导航到 http://127.0.0.1:*（remote 内容）后默认无法调用应用命令。
    // 必须显式声明 AppManifest 命令清单，tauri-build 才会生成 allow-* 权限文件，
    // 再由 capabilities/main.json 引用。清单必须与 main.rs 的 generate_handler! 一致。
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new().commands(&[
                "get_app_info",
                "get_startup_status",
                "retry_start",
                "open_model_dir",
                "check_update",
                "install_update",
            ]),
        ),
    )
    .expect("tauri-build 失败");
}
