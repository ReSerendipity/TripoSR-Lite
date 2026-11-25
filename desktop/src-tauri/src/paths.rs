use std::ffi::OsString;
use std::path::PathBuf;

/// 用户级应用目录：`%LOCALAPPDATA%\TripoSR-Lite`。
///
/// 纯函数便于测试：传入 `None` 时回退临时目录（仅在异常环境兜底）。
pub fn local_app_dir_with(base: Option<OsString>) -> PathBuf {
    base.map(PathBuf::from)
        .unwrap_or_else(std::env::temp_dir)
        .join("TripoSR-Lite")
}

/// 当前用户的 `%LOCALAPPDATA%\TripoSR-Lite`。
pub fn local_app_dir() -> PathBuf {
    local_app_dir_with(std::env::var_os("LOCALAPPDATA"))
}

/// 环境变量优先，缺省回退指定目录（空字符串视为未设置）。
pub fn env_path(name: &str, fallback: PathBuf) -> PathBuf {
    match std::env::var_os(name) {
        Some(value) if !value.is_empty() => PathBuf::from(value),
        _ => fallback,
    }
}

/// 模型目录：安装态落 `%LOCALAPPDATA%\TripoSR-Lite\models\triposr`，可用 `TRIPOSR_MODEL_DIR` 覆盖。
pub fn model_dir() -> PathBuf {
    env_path(
        "TRIPOSR_MODEL_DIR",
        local_app_dir().join("models").join("triposr"),
    )
}

/// 推理产物目录：`%LOCALAPPDATA%\TripoSR-Lite\results`，可用 `TRIPOSR_RESULTS_DIR` 覆盖。
pub fn results_dir() -> PathBuf {
    env_path("TRIPOSR_RESULTS_DIR", local_app_dir().join("results"))
}

/// 壳日志目录：`%LOCALAPPDATA%\TripoSR-Lite\logs`，可用 `TRIPOSR_LOG_DIR` 覆盖。
pub fn log_dir() -> PathBuf {
    env_path("TRIPOSR_LOG_DIR", local_app_dir().join("logs"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn local_app_dir_appends_product_folder() {
        let p = local_app_dir_with(Some(OsString::from(r"C:\Users\demo\AppData\Local")));
        assert_eq!(
            p,
            PathBuf::from(r"C:\Users\demo\AppData\Local\TripoSR-Lite")
        );
    }

    #[test]
    fn env_path_falls_back_when_unset_or_empty() {
        let fallback = PathBuf::from(r"C:\fallback");
        assert_eq!(
            env_path("TRIPOSR_TEST_PATH_SHOULD_BE_UNSET_XYZ", fallback.clone()),
            fallback
        );
    }

    #[test]
    fn default_dirs_all_live_under_local_app_dir() {
        let base = local_app_dir();
        assert_eq!(
            model_dir(),
            env_path(
                "TRIPOSR_MODEL_DIR",
                base.join("models").join("triposr")
            )
        );
        assert_eq!(
            results_dir(),
            env_path("TRIPOSR_RESULTS_DIR", base.join("results"))
        );
        assert_eq!(log_dir(), env_path("TRIPOSR_LOG_DIR", base.join("logs")));
    }
}
