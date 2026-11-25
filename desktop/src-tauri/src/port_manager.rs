use anyhow::{anyhow, Result};
use std::net::TcpListener;

/// 先绑定 `127.0.0.1:0` 拿到系统分配的空闲端口，再交给 Python 启动脚本。
///
/// 端口只做「预抓」不做「保留」：释放后到子进程绑定之间理论上有竞争，实践上
/// 本机单用户场景可忽略；比固定端口更安全（不会和用户已有的 7861 冲突）。
pub fn find_free_port() -> Result<u16> {
    for _ in 0..5 {
        match TcpListener::bind("127.0.0.1:0") {
            Ok(listener) => {
                let port = listener.local_addr()?.port();
                drop(listener);
                std::thread::sleep(std::time::Duration::from_millis(50));
                return Ok(port);
            }
            Err(_) => continue,
        }
    }
    Err(anyhow!("无法找到空闲端口"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn port_is_usable_and_in_range() {
        let port = find_free_port().unwrap();
        assert!(port > 1024);
        assert!(port < 65535);
    }
}
