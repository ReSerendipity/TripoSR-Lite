use anyhow::{anyhow, Result};
use serde::{Deserialize, Serialize};
use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::{Duration, Instant};

/// `GET /api/health` 响应（后端契约见 docs/RELEASE_PROFILE.md）。
#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct ServiceHealth {
    #[serde(default)]
    pub status: String,
    #[serde(default)]
    pub app: String,
    #[serde(default)]
    pub version: String,
}

/// `GET /api/model` 中壳关心的字段：就绪状态与缺失原因。
#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct ModelStatus {
    #[serde(default)]
    pub probe: Probe,
}

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct Probe {
    #[serde(default)]
    pub state: String,
    #[serde(default)]
    pub ready: bool,
    #[serde(default)]
    pub reasons: Vec<ProbeReason>,
    #[serde(default)]
    pub warnings: Vec<ProbeReason>,
}

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct ProbeReason {
    #[serde(default)]
    pub code: String,
    #[serde(default)]
    pub message: String,
    #[serde(default)]
    pub hint: String,
}

/// 极简本机 HTTP GET：只连 127.0.0.1，读完整响应后拆头/体。
///
/// 不引入 reqwest/tokio：健康检查只访问本机明文 HTTP，减少壳的依赖面与构建时间。
pub fn fetch_health(port: u16, timeout: Duration) -> Result<ServiceHealth> {
    let body = http_get_json(port, "/api/health", timeout)?;
    serde_json::from_str(&body)
        .map_err(|e| anyhow!("解析 /api/health 失败：{e}；响应体前 160 字符：{}", snippet(&body)))
}

pub fn fetch_model(port: u16, timeout: Duration) -> Result<ModelStatus> {
    let body = http_get_json(port, "/api/model", timeout)?;
    serde_json::from_str(&body)
        .map_err(|e| anyhow!("解析 /api/model 失败：{e}；响应体前 160 字符：{}", snippet(&body)))
}

fn http_get_json(port: u16, path: &str, timeout: Duration) -> Result<String> {
    let raw = http_get(port, path, timeout)?;
    parse_http_body(&raw)
}

fn http_get(port: u16, path: &str, timeout: Duration) -> Result<String> {
    let addr = std::net::SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = TcpStream::connect_timeout(&addr, timeout)?;
    stream.set_read_timeout(Some(timeout))?;
    stream.set_write_timeout(Some(timeout))?;
    let request = format!(
        "GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\nAccept: application/json\r\n\r\n"
    );
    stream.write_all(request.as_bytes())?;
    let mut raw = String::new();
    stream.read_to_string(&mut raw)?;
    Ok(raw)
}

/// 校验状态行 200 并返回响应体；支持 Content-Length 与 chunked 两种编码。
pub fn parse_http_body(raw: &str) -> Result<String> {
    let (head, body) = raw
        .split_once("\r\n\r\n")
        .ok_or_else(|| anyhow!("响应缺少头部/正文分隔"))?;
    let status_line = head.lines().next().unwrap_or_default();
    if !status_line.starts_with("HTTP/") || !status_line.contains(" 200") {
        return Err(anyhow!("HTTP 状态异常：{status_line}"));
    }
    let chunked = head.lines().any(|line| {
        line.to_ascii_lowercase().starts_with("transfer-encoding:")
            && line.to_ascii_lowercase().contains("chunked")
    });
    if chunked {
        let mut out = String::new();
        let mut rest = body;
        loop {
            let Some((size_line, tail)) = rest.split_once("\r\n") else {
                return Err(anyhow!("chunked 响应截断"));
            };
            let size = usize::from_str_radix(size_line.trim(), 16)
                .map_err(|e| anyhow!("chunked 长度非法：{e}"))?;
            if size == 0 {
                break;
            }
            if tail.len() < size {
                return Err(anyhow!("chunked 数据不足"));
            }
            out.push_str(&tail[..size]);
            rest = tail[size..].strip_prefix("\r\n").unwrap_or(&tail[size..]);
        }
        return Ok(out);
    }
    Ok(body.to_string())
}

fn snippet(body: &str) -> String {
    body.chars().take(160).collect()
}

/// 轮询直到 `/api/health` 可解析；超时返回最后一次错误。
pub fn wait_for_ready(port: u16, timeout: Duration) -> Result<ServiceHealth> {
    let start = Instant::now();
    let mut last_error = String::from("尚未发起请求");
    while start.elapsed() < timeout {
        match fetch_health(port, Duration::from_secs(3)) {
            Ok(health) => return Ok(health),
            Err(e) => {
                last_error = format!("{e:#}");
                std::thread::sleep(Duration::from_millis(500));
            }
        }
    }
    Err(anyhow!(
        "服务启动超时（{}s）：{last_error}",
        timeout.as_secs()
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_plain_200_body() {
        let raw = "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: 52\r\n\r\n{\"status\":\"ok\",\"app\":\"TripoSR-Lite\",\"version\":\"0.1.0\"}";
        let body = parse_http_body(raw).unwrap();
        let health: ServiceHealth = serde_json::from_str(&body).unwrap();
        assert_eq!(health.status, "ok");
        assert_eq!(health.version, "0.1.0");
    }

    #[test]
    fn rejects_non_200() {
        let raw = "HTTP/1.1 503 Service Unavailable\r\ncontent-length: 0\r\n\r\n";
        assert!(parse_http_body(raw).is_err());
    }

    #[test]
    fn decodes_chunked_body() {
        let raw = "HTTP/1.1 200 OK\r\ntransfer-encoding: chunked\r\n\r\n7\r\n{\"ready\r\n4\r\n\":1}\r\n0\r\n\r\n";
        let body = parse_http_body(raw).unwrap();
        assert_eq!(body.replace("\r\n", ""), "{\"ready\":1}");
    }

    #[test]
    fn missing_header_separator_is_error() {
        assert!(parse_http_body("HTTP/1.1 200 OK").is_err());
    }

    #[test]
    fn parses_model_not_ready_payload() {
        let raw = r#"{"probe":{"state":"NOT_READY","ready":false,"reasons":[{"code":"MISSING_WEIGHTS","message":"缺少 model.ckpt","hint":"下载"}],"warnings":[]},"model":{}}"#;
        let status: ModelStatus = serde_json::from_str(raw).unwrap();
        assert!(!status.probe.ready);
        assert_eq!(status.probe.state, "NOT_READY");
        assert_eq!(status.probe.reasons[0].code, "MISSING_WEIGHTS");
    }
}
