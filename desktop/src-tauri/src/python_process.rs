//! Python 子进程管理：解释器解析链、动态端口启动、进程树回收。
//!
//! 契约（与 `scripts/desktop_launch.py` 对齐）：
//! - 壳预抓空闲端口，以 `--host 127.0.0.1 --port N` 传入；
//! - 子进程 stdout/stderr 全量落 `logs/desktop_python_*.log`；
//! - 退出时 taskkill /T /F 杀整棵进程树，并按启动脚本路径扫描孤儿
//!   （含外部推理运行时被监督脚本拉起的 python）。

use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use anyhow::{anyhow, Result};
use serde::Serialize;

use crate::port_manager::find_free_port;

/// 只在外部环境未显式设置时补缺省，避免覆盖用户/测试的同名变量。
fn env_default(cmd: &mut Command, name: &str, value: &str) {
    if std::env::var_os(name).is_none() {
        cmd.env(name, value);
    }
}

/// Python 进程状态
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum PythonStatus {
    Stopped,
    Starting,
    Running,
    Crashed,
}

/// 解释器候选链（顺序即优先级，测试断言顺序）：
/// `TRIPOSR_LITE_PYTHON` 覆盖 → `<app>\runtime\python.exe`（安装态）
/// → 仓内 `.venv\Scripts\python.exe`（开发态）→ PATH 兜底 `python`。
pub fn python_candidates(app_dir: &Path, env_override: Option<PathBuf>) -> Vec<PathBuf> {
    let mut list = Vec::new();
    if let Some(p) = env_override {
        list.push(p);
    }
    list.push(app_dir.join("runtime").join("python.exe"));
    list.push(app_dir.join(".venv").join("Scripts").join("python.exe"));
    list.push(PathBuf::from("python"));
    list
}

/// 取第一个真实存在的候选；`python`（PATH 兜底）视为永远可用。
pub fn resolve_python_exe(candidates: &[PathBuf]) -> Option<PathBuf> {
    candidates
        .iter()
        .find(|p| p.is_absolute() && p.is_file())
        .cloned()
        .or_else(|| candidates.iter().find(|p| !p.is_absolute()).cloned())
}

/// 解析应用代码目录：
/// 1. 安装态：`<exe_dir>\app\scripts\desktop_launch.py` 存在 → `<exe_dir>\app`
/// 2. 开发态：`CARGO_MANIFEST_DIR` 上溯两级到仓库根
pub fn resolve_app_dir() -> PathBuf {
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let bundled = dir.join("app");
            if bundled.join("scripts").join("desktop_launch.py").exists() {
                return bundled;
            }
        }
    }
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(|p| p.parent())
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| PathBuf::from("."))
}

/// 解析应用运行时的可选形态：安装态 `runtime/`，开发态 `.venv`，最后 PATH。
pub fn resolve_runtime_dir(app_dir: &Path) -> PathBuf {
    let bundled = app_dir.join("runtime");
    if bundled.join("python.exe").exists() {
        return bundled;
    }
    let venv = app_dir.join(".venv").join("Scripts");
    if venv.join("python.exe").exists() {
        return venv;
    }
    PathBuf::from("python")
}

/// 运行时形态（诊断用，写入启动日志）
pub fn python_mode(app_dir: &Path) -> &'static str {
    if app_dir.join("runtime").join("python.exe").exists() {
        "bundled"
    } else if app_dir.join(".venv").join("Scripts").join("python.exe").exists() {
        "venv"
    } else {
        "path"
    }
}

/// 外部 TripoSR 推理运行时解释器：安装态 `external/TripoSR/runtime/python.exe`，
/// 开发态 `external/TripoSR/.venv/Scripts/python.exe`。
pub fn resolve_external_runtime(app_dir: &Path) -> Option<PathBuf> {
    let bundled = app_dir
        .join("external")
        .join("TripoSR")
        .join("runtime")
        .join("python.exe");
    if bundled.is_file() {
        return Some(bundled);
    }
    let venv = app_dir
        .join("external")
        .join("TripoSR")
        .join(".venv")
        .join("Scripts")
        .join("python.exe");
    if venv.is_file() {
        return Some(venv);
    }
    None
}

/// Python 子进程管理器
pub struct PythonProcess {
    child: Option<Child>,
    port: u16,
    runtime_dir: PathBuf,
    app_dir: PathBuf,
    log_dir: PathBuf,
    model_dir: PathBuf,
    results_dir: PathBuf,
    external_runtime: Option<PathBuf>,
    status: PythonStatus,
    restart_count: u32,
    max_restarts: u32,
}

impl PythonProcess {
    pub fn new(
        runtime_dir: PathBuf,
        app_dir: PathBuf,
        log_dir: PathBuf,
        model_dir: PathBuf,
        results_dir: PathBuf,
        external_runtime: Option<PathBuf>,
    ) -> Self {
        Self {
            child: None,
            port: 0,
            runtime_dir,
            app_dir,
            log_dir,
            model_dir,
            results_dir,
            external_runtime,
            status: PythonStatus::Stopped,
            restart_count: 0,
            max_restarts: 3,
        }
    }

    pub fn start(&mut self) -> Result<u16> {
        let port = find_free_port()?;
        self.port = port;

        let python_exe = resolve_python_exe(&python_candidates(
            &self.app_dir,
            std::env::var_os("TRIPOSR_LITE_PYTHON").map(PathBuf::from),
        ))
        .ok_or_else(|| anyhow!("找不到可用 Python 解释器（runtime/.venv/PATH 均未命中）"))?;
        log::info!("运行时目录（配置）：{}", self.runtime_dir.display());
        let launch_script = self.app_dir.join("scripts").join("desktop_launch.py");
        if !launch_script.is_file() {
            return Err(anyhow!("启动脚本不存在: {}", launch_script.display()));
        }

        std::fs::create_dir_all(&self.log_dir)?;
        let log_path = self.log_dir.join(format!(
            "desktop_python_{}.log",
            chrono::Local::now().format("%Y%m%d_%H%M%S")
        ));
        let log_writer = std::fs::File::create(&log_path)?;

        let upstream = self.app_dir.join("external").join("TripoSR");
        let supervisor = self.app_dir.join("scripts").join("run_triposr_real.py");

        let mut cmd = Command::new(&python_exe);
        cmd.arg(&launch_script)
            .arg("--host")
            .arg("127.0.0.1")
            .arg("--port")
            .arg(port.to_string())
            .current_dir(&self.app_dir)
            .env("PYTHONNOUSERSITE", "1")
            .env("PYTHONUNBUFFERED", "1")
            .env("PYTHONDONTWRITEBYTECODE", "1")
            .env("TRIPOSR_MODEL_DIR", &self.model_dir)
            .env("TRIPOSR_RESULTS_DIR", &self.results_dir)
            .env("TRIPOSR_UPSTREAM_DIR", &upstream)
            .env("TRIPOSR_SUPERVISOR", &supervisor)
            .stdout(Stdio::from(log_writer.try_clone()?))
            .stderr(Stdio::from(log_writer));
        if let Some(runtime) = &self.external_runtime {
            cmd.env("TRIPOSR_RUNTIME_PYTHON", runtime);
        }
        // 首次冷启动要加载 4GB+ 的 torch/CUDA DLL（杀软首次扫描会更久），
        // 实测冷启动 130–172s，30s 会把「慢」误报成 RUNTIME_PROBE_FAILED；放宽并延长缓存。
        env_default(&mut cmd, "TRIPOSR_PROBE_TIMEOUT_S", "300");
        env_default(&mut cmd, "TRIPOSR_PROBE_TTL_S", "300");
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
        }

        let child = cmd.spawn()?;
        log::info!(
            "Python 进程已启动，PID={}，端口={}，解释器={}，日志={}",
            child.id(),
            port,
            python_exe.display(),
            log_path.display()
        );
        self.child = Some(child);
        self.status = PythonStatus::Starting;
        Ok(port)
    }

    pub fn mark_running(&mut self) {
        self.status = PythonStatus::Running;
        self.restart_count = 0;
    }

    pub fn is_alive(&mut self) -> bool {
        if let Some(child) = &mut self.child {
            match child.try_wait() {
                Ok(None) => true,
                Ok(Some(status)) => {
                    log::warn!("Python 进程已退出，状态={status}");
                    false
                }
                Err(_) => false,
            }
        } else {
            false
        }
    }

    pub fn try_restart(&mut self) -> Result<bool> {
        if self.restart_count >= self.max_restarts {
            self.status = PythonStatus::Crashed;
            return Ok(false);
        }
        self.restart_count += 1;
        log::info!(
            "正在重启 Python 进程（第 {}/{} 次）",
            self.restart_count,
            self.max_restarts
        );
        self.stop()?;
        self.start()?;
        Ok(true)
    }

    pub fn stop(&mut self) -> Result<()> {
        if let Some(mut child) = self.child.take() {
            let pid = child.id();
            #[cfg(windows)]
            {
                let killed = Command::new("taskkill")
                    .args(["/PID", &pid.to_string(), "/T", "/F"])
                    .stdout(Stdio::null())
                    .stderr(Stdio::null())
                    .status()
                    .map(|s| s.success())
                    .unwrap_or(false);
                if !killed {
                    let _ = child.kill();
                }
                let _ = child.wait();
                log::info!("Python 进程树已停止（根 PID={pid}）");
                self.kill_orphans();
            }
            #[cfg(not(windows))]
            {
                let _ = child.kill();
                let _ = child.wait();
                log::info!("Python 进程已停止（根 PID={pid}）");
            }
        }
        self.status = PythonStatus::Stopped;
        Ok(())
    }

    /// 杀掉父进程已退出、仍以本应用启动脚本/监督脚本运行的孤儿 python。
    #[cfg(windows)]
    fn kill_orphans(&self) {
        let anchor = self.app_dir.display().to_string().replace('\'', "''");
        let ps = format!(
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | \
             Where-Object {{ ($_.CommandLine -like '*{anchor}*') -and \
             (($_.CommandLine -like '*desktop_launch.py*') -or ($_.CommandLine -like '*run_triposr_real.py*')) }} | \
             ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}"
        );
        let _ = Command::new("powershell")
            .args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", &ps])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }

    pub fn port(&self) -> u16 {
        self.port
    }

    pub fn status(&self) -> PythonStatus {
        self.status
    }
}

/// 全局共享的 Python 进程管理器
pub struct PythonState {
    pub process: Mutex<PythonProcess>,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn touch_python(app: &Path, sub: &[&str]) {
        let dir = sub.iter().fold(app.to_path_buf(), |p, s| p.join(s));
        std::fs::create_dir_all(&dir).expect("创建目录");
        std::fs::write(dir.join("python.exe"), b"").expect("写占位解释器");
    }

    #[test]
    fn candidate_order_is_override_runtime_venv_then_path() {
        let base = std::env::temp_dir().join(format!("triposr-cand-{}", std::process::id()));
        std::fs::create_dir_all(&base).unwrap();
        let override_path = base.join("override-python.exe");
        let list = python_candidates(&base, Some(override_path.clone()));
        assert_eq!(list[0], override_path);
        assert_eq!(list[1], base.join("runtime").join("python.exe"));
        assert_eq!(list[2], base.join(".venv").join("Scripts").join("python.exe"));
        assert_eq!(list[3], PathBuf::from("python"));
    }

    #[test]
    fn resolve_prefers_runtime_then_venv() {
        let base = std::env::temp_dir().join(format!("triposr-resolve-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();
        touch_python(&base, &[".venv", "Scripts"]);
        let picked = resolve_python_exe(&python_candidates(&base, None)).expect("应命中 .venv");
        assert!(picked.ends_with("python.exe"));
        assert!(picked.to_string_lossy().contains(".venv"));

        touch_python(&base, &["runtime"]);
        let picked = resolve_python_exe(&python_candidates(&base, None)).expect("应命中 runtime");
        assert!(picked.to_string_lossy().contains("runtime"));

        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn resolve_falls_back_to_path_python() {
        let empty = std::env::temp_dir().join(format!("triposr-empty-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&empty);
        std::fs::create_dir_all(&empty).unwrap();
        let picked = resolve_python_exe(&python_candidates(&empty, None)).expect("PATH 兜底");
        assert_eq!(picked, PathBuf::from("python"));
        let _ = std::fs::remove_dir_all(&empty);
    }

    #[test]
    fn mode_reports_bundled_venv_path() {
        let base = std::env::temp_dir().join(format!("triposr-mode-{}", std::process::id()));
        std::fs::create_dir_all(&base).unwrap();
        assert_eq!(python_mode(&base), "path");
        touch_python(&base, &[".venv", "Scripts"]);
        assert_eq!(python_mode(&base), "venv");
        touch_python(&base, &["runtime"]);
        assert_eq!(python_mode(&base), "bundled");
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn external_runtime_prefers_bundled_then_venv() {
        let base = std::env::temp_dir().join(format!("triposr-ext-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        std::fs::create_dir_all(&base).unwrap();
        assert!(resolve_external_runtime(&base).is_none());
        touch_python(&base, &["external", "TripoSR", ".venv", "Scripts"]);
        let picked = resolve_external_runtime(&base).expect("开发态外部运行时");
        assert!(picked.to_string_lossy().contains(".venv"));
        touch_python(&base, &["external", "TripoSR", "runtime"]);
        let picked = resolve_external_runtime(&base).expect("安装态外部运行时");
        assert!(picked.to_string_lossy().ends_with(r"runtime\python.exe"));
        let _ = std::fs::remove_dir_all(&base);
    }
}
