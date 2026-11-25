// Prevents additional console window on Windows in release
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod health_check;
mod paths;
mod port_manager;
mod python_process;
mod window;

use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

use serde::Serialize;
use tauri::{App, AppHandle, Emitter, Manager, RunEvent};

use python_process::{
    python_mode, resolve_app_dir, resolve_external_runtime, resolve_runtime_dir, PythonProcess,
    PythonState, PythonStatus,
};

/// 加载页/命令共享的启动状态快照（事件 + 轮询双通道，避免事件早于页面订阅）。
#[derive(Debug, Clone, Default, Serialize)]
pub struct StartupSnapshot {
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    pub ready: bool,
    pub model_state: String,
    pub missing: Vec<String>,
    pub port: u16,
    pub model_dir: String,
}

pub struct StartupStore(pub Mutex<StartupSnapshot>);

/// 前端 `get_app_info` 返回值（版本与路径诊断用）。
#[derive(Debug, Clone, Serialize)]
struct AppInfo {
    version: String,
    app_dir: String,
    runtime_dir: String,
    python_mode: String,
    external_runtime: String,
    model_dir: String,
    results_dir: String,
    log_dir: String,
}

fn emit_status(app: &AppHandle, snapshot: StartupSnapshot) {
    if let Some(store) = app.try_state::<Arc<StartupStore>>() {
        if let Ok(mut guard) = store.0.lock() {
            *guard = snapshot.clone();
        }
    }
    if let Err(e) = app.emit("startup-status", &snapshot) {
        log::warn!("广播 startup-status 失败：{e}");
    }
}

fn navigate_to_app(app: &AppHandle, port: u16) {
    let Some(win) = app.get_webview_window(window::MAIN_LABEL) else {
        return;
    };
    let target = format!("http://127.0.0.1:{port}/");
    match target.parse::<tauri::Url>() {
        Ok(url) => {
            if let Err(e) = win.navigate(url) {
                log::error!("导航到后端页面失败（{target}）：{e}");
            }
        }
        Err(e) => log::error!("解析后端地址失败（{target}）：{e}"),
    }
}

/// 后台读取 `/api/model`：不阻塞导航，只刷新状态、写日志。
fn probe_model_async(app: AppHandle, port: u16) {
    std::thread::spawn(move || {
        match health_check::fetch_model(port, Duration::from_secs(360)) {
            Ok(status) => {
                let state = if status.probe.state.is_empty() {
                    "UNKNOWN".to_string()
                } else {
                    status.probe.state.clone()
                };
                let missing: Vec<String> =
                    status.probe.reasons.iter().map(|r| r.message.clone()).collect();
                if status.probe.ready {
                    log::info!("模型就绪（{state}）");
                    emit_status(
                        &app,
                        StartupSnapshot {
                            message: format!("模型就绪（{state}）"),
                            ready: true,
                            model_state: state,
                            port,
                            ..Default::default()
                        },
                    );
                } else {
                    log::warn!("模型未就绪（{state}）：{}", missing.join("；"));
                    emit_status(
                        &app,
                        StartupSnapshot {
                            message: if missing.is_empty() {
                                format!("NOT_READY：{state}")
                            } else {
                                format!("NOT_READY：{}", missing.join("；"))
                            },
                            ready: true,
                            model_state: state,
                            missing,
                            port,
                            ..Default::default()
                        },
                    );
                }
            }
            Err(e) => log::warn!("读取 /api/model 失败：{e:#}"),
        }
    });
}

/// 启动 Python 并等待健康端点，随后导航主窗口。
fn start_python_and_load(app: &AppHandle, state: &Arc<PythonState>) {
    emit_status(
        app,
        StartupSnapshot {
            message: "正在启动本地服务…".into(),
            ..Default::default()
        },
    );

    let started = {
        let mut proc = state.process.lock().unwrap();
        proc.start()
    };
    let port = match started {
        Ok(port) => port,
        Err(e) => {
            log::error!("启动 Python 失败：{e:#}");
            emit_status(
                app,
                StartupSnapshot {
                    message: "启动失败".into(),
                    error: Some(format!("{e:#}")),
                    ..Default::default()
                },
            );
            return;
        }
    };

    log::info!("Python 进程已启动，端口={port}");
    emit_status(
        app,
        StartupSnapshot {
            message: format!("正在等待服务响应（端口 {port}）…"),
            port,
            ..Default::default()
        },
    );

    let timeout_secs = std::env::var("TRIPOSR_STARTUP_TIMEOUT_SECS")
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
        .unwrap_or(300);

    match health_check::wait_for_ready(port, Duration::from_secs(timeout_secs)) {
        Ok(health) => {
            {
                let mut proc = state.process.lock().unwrap();
                proc.mark_running();
            }
            log::info!(
                "Python 服务就绪，端口={port}，版本={}",
                health.version
            );
            emit_status(
                app,
                StartupSnapshot {
                    message: format!("服务已就绪（端口 {port}，v{}）", health.version),
                    ready: true,
                    port,
                    ..Default::default()
                },
            );
            navigate_to_app(app, port);
            probe_model_async(app.clone(), port);
        }
        Err(e) => {
            log::error!("健康检查失败：{e:#}");
            emit_status(
                app,
                StartupSnapshot {
                    message: "启动超时".into(),
                    error: Some(format!("{e:#}")),
                    port,
                    ..Default::default()
                },
            );
        }
    }
}

/// 后台监控：Python 意外退出时最多自动重启 3 次。
fn start_watchdog(app: AppHandle, state: Arc<PythonState>) {
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_secs(2));

        let needs_restart = {
            let mut proc = state.process.lock().unwrap();
            proc.status() == PythonStatus::Running && !proc.is_alive()
        };
        if !needs_restart {
            continue;
        }

        log::warn!("检测到 Python 进程退出，尝试自动重启");
        emit_status(
            &app,
            StartupSnapshot {
                message: "检测到服务退出，正在重启…".into(),
                ..Default::default()
            },
        );

        let restarted = {
            let mut proc = state.process.lock().unwrap();
            proc.try_restart().unwrap_or(false)
        };
        if !restarted {
            emit_status(
                &app,
                StartupSnapshot {
                    message: "已停止自动重启".into(),
                    error: Some("Python 进程多次崩溃，请查看日志后点击「重试启动」。".into()),
                    ..Default::default()
                },
            );
            continue;
        }

        let port = {
            let proc = state.process.lock().unwrap();
            proc.port()
        };
        if health_check::wait_for_ready(port, Duration::from_secs(30)).is_ok() {
            {
                let mut proc = state.process.lock().unwrap();
                proc.mark_running();
            }
            navigate_to_app(&app, port);
            emit_status(
                &app,
                StartupSnapshot {
                    message: format!("服务已恢复（端口 {port}）"),
                    ready: true,
                    port,
                    ..Default::default()
                },
            );
            probe_model_async(app.clone(), port);
        } else {
            emit_status(
                &app,
                StartupSnapshot {
                    message: "重启后服务未就绪".into(),
                    error: Some("自动重启后健康检查仍失败，请查看日志。".into()),
                    port,
                    ..Default::default()
                },
            );
        }
    });
}

// ===== Tauri 命令（build.rs AppManifest 与 capabilities 同步）=====

#[tauri::command]
fn get_app_info() -> AppInfo {
    let app_dir = resolve_app_dir();
    let external = resolve_external_runtime(&app_dir)
        .map(|p| p.display().to_string())
        .unwrap_or_else(|| "(none)".to_string());
    AppInfo {
        version: env!("CARGO_PKG_VERSION").to_string(),
        python_mode: python_mode(&app_dir).to_string(),
        runtime_dir: resolve_runtime_dir(&app_dir).display().to_string(),
        app_dir: app_dir.display().to_string(),
        external_runtime: external,
        model_dir: paths::model_dir().display().to_string(),
        results_dir: paths::results_dir().display().to_string(),
        log_dir: paths::log_dir().display().to_string(),
    }
}

#[tauri::command]
fn get_startup_status(store: tauri::State<'_, Arc<StartupStore>>) -> StartupSnapshot {
    store.0.lock().map(|guard| guard.clone()).unwrap_or_default()
}

#[tauri::command]
fn retry_start(app: AppHandle, state: tauri::State<'_, Arc<PythonState>>) -> Result<(), String> {
    {
        let mut proc = state.process.lock().unwrap();
        proc.stop().map_err(|e| format!("{e:#}"))?;
    }
    let handle = app.clone();
    let shared = state.inner().clone();
    std::thread::spawn(move || start_python_and_load(&handle, &shared));
    Ok(())
}

#[tauri::command]
fn open_model_dir() -> Result<String, String> {
    let dir = paths::model_dir();
    std::fs::create_dir_all(&dir).map_err(|e| format!("创建模型目录失败：{e}"))?;
    #[cfg(windows)]
    {
        std::process::Command::new("explorer.exe")
            .arg(&dir)
            .spawn()
            .map_err(|e| format!("打开模型目录失败：{e}"))?;
    }
    #[cfg(not(windows))]
    {
        std::process::Command::new("xdg-open")
            .arg(&dir)
            .spawn()
            .map_err(|e| format!("打开模型目录失败：{e}"))?;
    }
    Ok(dir.display().to_string())
}

fn setup(app: &mut App) -> Result<(), Box<dyn std::error::Error>> {
    let handle = app.handle().clone();

    let app_dir = resolve_app_dir();
    let runtime_dir = resolve_runtime_dir(&app_dir);
    let external_runtime = resolve_external_runtime(&app_dir);
    let log_dir = paths::log_dir();
    let model_dir = paths::model_dir();
    let results_dir = paths::results_dir();

    for dir in [&log_dir, &model_dir, &results_dir] {
        if let Err(e) = std::fs::create_dir_all(dir) {
            log::warn!("创建目录失败 {}：{e}", dir.display());
        }
    }

    log::info!("应用目录：{}", app_dir.display());
    log::info!(
        "运行时目录：{}（形态 {}）",
        runtime_dir.display(),
        python_mode(&app_dir)
    );
    log::info!(
        "外部推理运行时：{}",
        external_runtime
            .as_ref()
            .map(|p| p.display().to_string())
            .unwrap_or_else(|| "(none)".into())
    );
    log::info!("模型目录：{}", model_dir.display());
    log::info!("结果目录：{}", results_dir.display());
    log::info!("日志目录：{}", log_dir.display());

    let python_state = Arc::new(PythonState {
        process: Mutex::new(PythonProcess::new(
            runtime_dir,
            app_dir,
            log_dir,
            model_dir,
            results_dir,
            external_runtime,
        )),
    });
    app.manage(python_state.clone());
    app.manage(Arc::new(StartupStore(Mutex::new(StartupSnapshot::default()))));

    window::build_main_window(&handle)?;

    let startup_handle = handle.clone();
    let startup_state = python_state.clone();
    std::thread::spawn(move || start_python_and_load(&startup_handle, &startup_state));

    start_watchdog(handle, python_state);
    Ok(())
}

// ===== 日志：stdout + logs/triposr-lite-shell.log（5MB × 3 滚动）=====

const SHELL_LOG_MAX_BYTES: u64 = 5 * 1024 * 1024;
const SHELL_LOG_BACKUPS: u32 = 3;

fn rotate_shell_log(path: &std::path::Path) {
    let oldest = path.with_extension(format!("log.{SHELL_LOG_BACKUPS}"));
    let _ = std::fs::remove_file(&oldest);
    for i in (1..SHELL_LOG_BACKUPS).rev() {
        let from = path.with_extension(format!("log.{i}"));
        let to = path.with_extension(format!("log.{}", i + 1));
        if from.exists() {
            let _ = std::fs::rename(&from, &to);
        }
    }
    let _ = std::fs::rename(path, path.with_extension("log.1"));
}

struct DualLogger {
    file: Mutex<Option<std::fs::File>>,
    path: Mutex<Option<std::path::PathBuf>>,
}

impl log::Log for DualLogger {
    fn enabled(&self, _metadata: &log::Metadata) -> bool {
        true
    }

    fn log(&self, record: &log::Record) {
        use std::io::Write;
        let line = format!(
            "[{}] [{}] {}",
            chrono::Local::now().format("%Y-%m-%d %H:%M:%S"),
            record.level(),
            record.args()
        );
        let mut stdout = std::io::stdout();
        let _ = writeln!(stdout, "{line}");
        if let Ok(mut guard) = self.file.lock() {
            if let Some(file) = guard.as_mut() {
                if let Ok(meta) = file.metadata() {
                    if meta.len() > SHELL_LOG_MAX_BYTES {
                        if let Some(p) = self.path.lock().ok().and_then(|p| p.clone()) {
                            let _ = file.flush();
                            *guard = None;
                            rotate_shell_log(&p);
                            if let Ok(new_file) =
                                std::fs::OpenOptions::new().create(true).append(true).open(&p)
                            {
                                *guard = Some(new_file);
                            }
                        }
                    }
                }
                if let Some(file) = guard.as_mut() {
                    let _ = writeln!(file, "{line}");
                }
            }
        }
    }

    fn flush(&self) {
        use std::io::Write;
        if let Ok(mut guard) = self.file.lock() {
            if let Some(file) = guard.as_mut() {
                let _ = file.flush();
            }
        }
    }
}

static LOGGER: OnceLock<Arc<DualLogger>> = OnceLock::new();

fn init_logging() {
    let file = paths::log_dir().join("triposr-lite-shell.log");
    if let Some(parent) = file.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let handle = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&file)
        .ok();
    let logger = Arc::new(DualLogger {
        file: Mutex::new(handle),
        path: Mutex::new(Some(file)),
    });
    let _ = LOGGER.set(logger.clone());
    if let Err(e) = log::set_boxed_logger(Box::new(logger)) {
        eprintln!("日志初始化失败：{e}");
    }
    let level = std::env::var("RUST_LOG")
        .unwrap_or_else(|_| "info".into())
        .to_lowercase();
    log::set_max_level(match level.as_str() {
        "debug" => log::LevelFilter::Debug,
        "trace" => log::LevelFilter::Trace,
        "warn" => log::LevelFilter::Warn,
        "error" => log::LevelFilter::Error,
        _ => log::LevelFilter::Info,
    });
}

fn main() {
    init_logging();

    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            window::show_and_focus(app);
        }))
        .invoke_handler(tauri::generate_handler![
            get_app_info,
            get_startup_status,
            retry_start,
            open_model_dir,
        ])
        .setup(setup)
        .build(tauri::generate_context!())
        .expect("启动 Tauri 应用失败")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                let state = app.state::<Arc<PythonState>>();
                let mut proc = state.process.lock().unwrap();
                if let Err(e) = proc.stop() {
                    log::warn!("退出清理 Python 进程失败：{e:#}");
                }
                log::info!("TripoSR-Lite 壳已退出");
            }
        });
}
