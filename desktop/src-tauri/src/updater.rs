//! 单一完整包更新路线：官网下载与客户端「检查更新」取同一套 Release 资产。
//!
//! 本仓完整负载是 `TripoSR-Lite-Setup-v<version>.exe` + 同目录的
//! `TripoSR-Lite-Data.7z.001..NNN`（外部 torch 运行时约 5.1GB，NSIS 单文件装不下）
//! + `SHA256SUMS`。不引入差分/增量双路线。
//!
//! 自更新七步：检测 → 提示 → 用户授权 → 下载完整包 + SHA256 → 应用有序退出 →
//! `/S` 静默安装 → **安装器按 `/RELAUNCH` 自动重启应用**（最后一步必须托付安装器）。

use anyhow::{anyhow, Context, Result};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::cmp::Ordering;
use std::collections::HashMap;
use std::fs::File;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::time::Duration;
use tauri::{AppHandle, Emitter};

const DEFAULT_REPO: &str = "ReSerendipity/TripoSR-Lite";
const SETUP_PREFIX: &str = "TripoSR-Lite-Setup-v";
const DATA_PREFIX: &str = "TripoSR-Lite-Data.7z.";
const SUMS_NAME: &str = "SHA256SUMS";
const API_TEMPLATE: &str = "https://api.github.com/repos/{repo}/releases/latest";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UpdateAsset {
    pub name: String,
    pub url: String,
    pub size: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UpdateInfo {
    pub version: String,
    pub notes: String,
    pub assets: Vec<UpdateAsset>,
}

#[derive(Debug, Deserialize)]
struct GhAsset {
    name: String,
    browser_download_url: String,
    size: u64,
}

#[derive(Debug, Deserialize)]
struct GhRelease {
    tag_name: String,
    #[serde(default)]
    body: Option<String>,
    assets: Vec<GhAsset>,
}

fn repo_slug() -> String {
    std::env::var("TRIPOSR_UPDATE_REPO")
        .ok()
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| DEFAULT_REPO.to_string())
}

fn parse_version(value: &str) -> Option<Vec<u64>> {
    let parts: Vec<&str> = value.trim().split('.').collect();
    if parts.is_empty() || parts.len() > 3 {
        return None;
    }
    parts.iter().map(|p| p.parse::<u64>().ok()).collect()
}

fn compare_versions(left: &str, right: &str) -> Ordering {
    let mut a = parse_version(left).unwrap_or_default();
    let mut b = parse_version(right).unwrap_or_default();
    let len = a.len().max(b.len());
    a.resize(len, 0);
    b.resize(len, 0);
    a.cmp(&b)
}

fn setup_name(version: &str) -> String {
    format!("{SETUP_PREFIX}{version}.exe")
}

/// 只认精确完整安装包名，不再回退任何代码增量 zip（单一路线契约）。
fn resolve_assets(release: GhRelease) -> Result<UpdateInfo> {
    let version = release.tag_name.trim_start_matches('v').to_string();
    if parse_version(&version).is_none() {
        return Err(anyhow!("Release tag 不是合法版本号: {}", release.tag_name));
    }
    let wanted_setup = setup_name(&version);
    let mut assets = Vec::new();
    let mut setup_count = 0;
    for asset in &release.assets {
        let is_setup = asset.name == wanted_setup;
        let is_data = asset.name.starts_with(DATA_PREFIX);
        let is_sums = asset.name == SUMS_NAME;
        if is_setup {
            setup_count += 1;
        }
        if is_setup || is_data || is_sums {
            assets.push(UpdateAsset {
                name: asset.name.clone(),
                url: asset.browser_download_url.clone(),
                size: asset.size,
            });
        }
    }
    if setup_count != 1 {
        return Err(anyhow!("Release 缺少唯一完整安装包 {wanted_setup}"));
    }
    if !assets.iter().any(|a| a.name.starts_with(DATA_PREFIX)) {
        return Err(anyhow!("Release 缺少数据分卷 {DATA_PREFIX}001..NNN"));
    }
    if !assets.iter().any(|a| a.name == SUMS_NAME) {
        return Err(anyhow!("Release 缺少 {SUMS_NAME}，无法校验完整性"));
    }
    Ok(UpdateInfo {
        version,
        notes: release.body.unwrap_or_default(),
        assets,
    })
}

fn client(timeout: Duration) -> Result<reqwest::blocking::Client> {
    reqwest::blocking::Client::builder()
        .user_agent("TripoSR-Lite-Updater/1.0")
        .timeout(timeout)
        .build()
        .context("创建更新 HTTP 客户端失败")
}

/// 第一步：自动检测的底层调用。返回 None 表示已是最新版。
pub fn check_update() -> std::result::Result<Option<UpdateInfo>, String> {
    let url = API_TEMPLATE.replace("{repo}", &repo_slug());
    let release = client(Duration::from_secs(15))
        .and_then(|c| c.get(&url).send().context("检查更新请求失败"))
        .and_then(|r| r.error_for_status().context("GitHub Release 返回错误状态"))
        .and_then(|r| r.json::<GhRelease>().context("Release JSON 解析失败"))
        .map_err(|e| e.to_string())?;
    let info = resolve_assets(release).map_err(|e| e.to_string())?;
    let current = env!("CARGO_PKG_VERSION");
    if compare_versions(&info.version, current) == Ordering::Greater {
        Ok(Some(info))
    } else {
        Ok(None)
    }
}

fn updates_dir(version: &str) -> Result<PathBuf> {
    let root = std::env::var("TRIPOSR_UPDATES_DIR")
        .ok()
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            let local = std::env::var("LOCALAPPDATA").unwrap_or_else(|_| ".".to_string());
            Path::new(&local).join("TripoSR-Lite").join("updates")
        });
    let dir = root.join(version);
    std::fs::create_dir_all(&dir).context("创建更新下载目录失败")?;
    Ok(dir)
}

fn sha256_hex(data: &[u8]) -> String {
    hex::encode(Sha256::digest(data))
}

fn download_asset(
    http: &reqwest::blocking::Client,
    asset: &UpdateAsset,
    dir: &Path,
    app: &AppHandle,
    index: usize,
    total: usize,
) -> Result<String> {
    let mut response = http
        .get(&asset.url)
        .send()
        .with_context(|| format!("下载 {} 失败", asset.name))?
        .error_for_status()
        .with_context(|| format!("下载 {} 返回错误状态", asset.name))?;
    let path = dir.join(&asset.name);
    let mut file = File::create(&path).with_context(|| format!("创建 {} 失败", asset.name))?;
    let mut hasher = Sha256::new();
    let mut buffer = [0u8; 64 * 1024];
    loop {
        let read = response.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
        file.write_all(&buffer[..read])?;
    }
    file.flush()?;
    let _ = app.emit(
        "update-progress",
        serde_json::json!({"ratio": (index + 1) as f64 / total as f64, "file": asset.name}),
    );
    let digest = hasher.finalize();
    Ok(sha256_hex(&digest))
}

fn verify_checksums(dir: &Path, hashes: &HashMap<String, String>) -> Result<()> {
    let text = std::fs::read_to_string(dir.join(SUMS_NAME)).context("读取 SHA256SUMS 失败")?;
    let mut checked = 0usize;
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let mut fields = line.split_whitespace();
        let expected = fields.next().unwrap_or("");
        let name = fields.next().unwrap_or("");
        if name.is_empty() {
            continue;
        }
        let actual = hashes
            .get(name)
            .ok_or_else(|| anyhow!("SHA256SUMS 列出了未下载的文件 {name}"))?;
        if actual.eq_ignore_ascii_case(expected) {
            checked += 1;
        } else {
            return Err(anyhow!("{name} SHA256 不匹配：期望 {expected}，实际 {actual}"));
        }
    }
    if checked == 0 {
        return Err(anyhow!("SHA256SUMS 为空或格式不符"));
    }
    Ok(())
}

/// 传给安装器的参数：`/S` 静默 + `/RELAUNCH` 装完自动拉起主程序。
fn installer_args() -> Vec<&'static str> {
    vec!["/S", "/RELAUNCH"]
}

#[cfg(windows)]
fn launch_installer(exe: &Path) -> Result<()> {
    use std::os::windows::process::CommandExt;
    const DETACHED_PROCESS: u32 = 0x0000_0008;
    const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
    let mut cmd = std::process::Command::new(exe);
    cmd.args(installer_args())
        .creation_flags(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP);
    cmd.spawn().context("启动静默安装器失败")?;
    Ok(())
}

#[cfg(not(windows))]
fn launch_installer(exe: &Path) -> Result<()> {
    let mut cmd = std::process::Command::new(exe);
    cmd.args(installer_args());
    cmd.spawn().context("启动安装器失败")?;
    Ok(())
}

/// 第四~七步：下载 + 校验 → 发事件 → 有序退出 → 安装器接手重启。
pub fn install_update(app: AppHandle, info: UpdateInfo) -> std::result::Result<(), String> {
    let dir = updates_dir(&info.version).map_err(|e| e.to_string())?;
    let http = client(Duration::from_secs(1800)).map_err(|e| e.to_string())?;
    let total = info.assets.len();
    let mut hashes = HashMap::new();
    for (index, asset) in info.assets.iter().enumerate() {
        let hash = download_asset(&http, asset, &dir, &app, index, total).map_err(|e| e.to_string())?;
        hashes.insert(asset.name.clone(), hash);
    }
    verify_checksums(&dir, &hashes).map_err(|e| e.to_string())?;
    let _ = app.emit(
        "update-installing",
        serde_json::json!({"message": "应用即将关闭，安装程序将自动完成更新并重新启动。"}),
    );
    let setup = dir.join(setup_name(&info.version));
    launch_installer(&setup).map_err(|e| e.to_string())?;
    // 事件已发出、安装器已独立存活；将死的应用无法完成第 7 步，交给安装器。
    std::process::exit(0);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn version_comparison_handles_padding() {
        assert_eq!(compare_versions("0.2.0", "0.1.9"), Ordering::Greater);
        assert_eq!(compare_versions("0.1.0", "0.1"), Ordering::Equal);
        assert_eq!(compare_versions("1.0.0", "2.0.0"), Ordering::Less);
        assert_eq!(compare_versions("bad", "0.1.0"), Ordering::Less);
    }

    #[test]
    fn installer_args_carry_relaunch_flag() {
        assert_eq!(installer_args(), vec!["/S", "/RELAUNCH"]);
    }

    #[test]
    fn resolve_assets_accepts_only_the_complete_package() {
        let json = r#"{
            "tag_name": "v0.2.0", "body": "notes",
            "assets": [
              {"name":"TripoSR-Lite-Setup-v0.2.0.exe","browser_download_url":"https://x/setup","size":1},
              {"name":"TripoSR-Lite-Data.7z.001","browser_download_url":"https://x/001","size":2},
              {"name":"TripoSR-Lite-Data.7z.002","browser_download_url":"https://x/002","size":3},
              {"name":"SHA256SUMS","browser_download_url":"https://x/sums","size":4},
              {"name":"app-v0.2.0.zip","browser_download_url":"https://x/zip","size":5}
            ]
        }"#;
        let release: GhRelease = serde_json::from_str(json).unwrap();
        let info = resolve_assets(release).unwrap();
        assert_eq!(info.version, "0.2.0");
        assert_eq!(info.assets.len(), 4); // setup + 两卷 + sums，增量 zip 被忽略
        assert_eq!(info.notes, "notes");
    }

    #[test]
    fn resolve_assets_rejects_missing_setup_or_volumes() {
        let release: GhRelease = serde_json::from_str(
            r#"{"tag_name":"v0.2.0","assets":[{"name":"app-v0.2.0.zip","browser_download_url":"u","size":1}]}"#,
        )
        .unwrap();
        assert!(resolve_assets(release).is_err());
    }

    #[test]
    fn checksums_are_verified_against_downloaded_bytes() {
        let dir = std::env::temp_dir().join(format!("triposr-sums-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let file = dir.join("TripoSR-Lite-Data.7z.001");
        std::fs::write(&file, b"payload").unwrap();
        let expected = sha256_hex(b"payload");
        std::fs::write(
            dir.join(SUMS_NAME),
            format!("{expected}  TripoSR-Lite-Data.7z.001\n"),
        )
        .unwrap();
        let mut hashes = HashMap::new();
        hashes.insert("TripoSR-Lite-Data.7z.001".to_string(), expected);
        assert!(verify_checksums(&dir, &hashes).is_ok());
        hashes.insert("TripoSR-Lite-Data.7z.001".to_string(), "deadbeef".to_string());
        assert!(verify_checksums(&dir, &hashes).is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
