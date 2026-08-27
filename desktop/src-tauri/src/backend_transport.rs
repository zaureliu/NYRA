//! Controlled HTTP transport for the packaged NYRA frontend.
//!
//! This bridge accepts only NYRA API paths and always connects to the fixed
//! loopback backend; callers cannot select a host, port or external URL.

use serde::{Deserialize, Serialize};
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::time::Duration;

const BACKEND_ADDRESS: &str = "127.0.0.1:8000";
const MAX_REQUEST_BYTES: usize = 32 * 1024 * 1024;
const MAX_RESPONSE_BYTES: u64 = 64 * 1024 * 1024;
const MAX_PATH_BYTES: usize = 8 * 1024;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BackendRequest {
    pub method: String,
    pub path: String,
    #[serde(default)]
    pub headers: Vec<(String, String)>,
    #[serde(default)]
    pub body: Vec<u8>,
}

#[derive(Debug, Serialize)]
pub struct BackendResponse {
    pub status: u16,
    pub status_text: String,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

pub fn execute(request: BackendRequest) -> Result<BackendResponse, String> {
    let method = validate_method(&request.method)?;
    validate_path(&request.path)?;
    if request.body.len() > MAX_REQUEST_BYTES {
        return Err("BACKEND_REQUEST_BODY_TOO_LARGE".into());
    }
    let headers = validate_headers(request.headers)?;
    let address: SocketAddr = BACKEND_ADDRESS
        .parse()
        .map_err(|_| "BACKEND_ADDRESS_INVALID".to_string())?;
    let mut stream = TcpStream::connect_timeout(&address, Duration::from_secs(2))
        .map_err(|_| "BACKEND_OFFLINE".to_string())?;
    stream
        .set_read_timeout(Some(Duration::from_secs(300)))
        .and_then(|_| stream.set_write_timeout(Some(Duration::from_secs(30))))
        .map_err(|_| "BACKEND_TIMEOUT_SETUP_FAILED".to_string())?;

    let mut head = format!(
        "{method} {} HTTP/1.1\r\nHost: {BACKEND_ADDRESS}\r\nConnection: close\r\n",
        request.path
    );
    for (name, value) in headers {
        head.push_str(&name);
        head.push_str(": ");
        head.push_str(&value);
        head.push_str("\r\n");
    }
    if !request.body.is_empty() || matches!(method, "POST" | "PUT" | "PATCH") {
        head.push_str(&format!("Content-Length: {}\r\n", request.body.len()));
    }
    head.push_str("\r\n");
    stream
        .write_all(head.as_bytes())
        .and_then(|_| stream.write_all(&request.body))
        .map_err(|_| "BACKEND_WRITE_FAILED".to_string())?;

    let mut raw = Vec::new();
    stream
        .take(MAX_RESPONSE_BYTES + 1)
        .read_to_end(&mut raw)
        .map_err(|_| "BACKEND_READ_FAILED".to_string())?;
    if raw.len() as u64 > MAX_RESPONSE_BYTES {
        return Err("BACKEND_RESPONSE_TOO_LARGE".into());
    }
    parse_response(&raw)
}

fn validate_method(method: &str) -> Result<&str, String> {
    match method.to_ascii_uppercase().as_str() {
        "GET" => Ok("GET"),
        "POST" => Ok("POST"),
        "PUT" => Ok("PUT"),
        "PATCH" => Ok("PATCH"),
        "DELETE" => Ok("DELETE"),
        _ => Err("BACKEND_METHOD_NOT_ALLOWED".into()),
    }
}

fn validate_path(path: &str) -> Result<(), String> {
    if path.is_empty()
        || path.len() > MAX_PATH_BYTES
        || !(path == "/api"
            || path.starts_with("/api/")
            || path == "/health"
            || path.starts_with("/health?"))
        || path.starts_with("//")
        || path.contains(['\r', '\n', '#', '\\'])
        || path.contains("://")
    {
        return Err("BACKEND_PATH_NOT_ALLOWED".into());
    }
    let lower = path.to_ascii_lowercase();
    if lower.contains("/../")
        || lower.ends_with("/..")
        || lower.contains("%2e%2e")
        || lower.contains("%0d")
        || lower.contains("%0a")
    {
        return Err("BACKEND_PATH_NOT_ALLOWED".into());
    }
    Ok(())
}

fn validate_headers(headers: Vec<(String, String)>) -> Result<Vec<(String, String)>, String> {
    let mut accepted = Vec::with_capacity(headers.len());
    for (name, value) in headers {
        let normalized = name.trim().to_ascii_lowercase();
        let allowed = matches!(
            normalized.as_str(),
            "accept"
                | "accept-language"
                | "content-type"
                | "cache-control"
                | "pragma"
                | "range"
                | "if-match"
                | "if-none-match"
                | "if-modified-since"
                | "if-unmodified-since"
        );
        if !allowed || value.contains(['\r', '\n']) {
            return Err("BACKEND_HEADER_NOT_ALLOWED".into());
        }
        accepted.push((normalized, value));
    }
    Ok(accepted)
}

fn parse_response(raw: &[u8]) -> Result<BackendResponse, String> {
    let boundary = raw
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .ok_or_else(|| "BACKEND_INVALID_HTTP".to_string())?;
    let header_text =
        std::str::from_utf8(&raw[..boundary]).map_err(|_| "BACKEND_INVALID_HTTP".to_string())?;
    let mut lines = header_text.lines();
    let status_line = lines
        .next()
        .ok_or_else(|| "BACKEND_INVALID_HTTP".to_string())?;
    let mut status_parts = status_line.splitn(3, ' ');
    let version = status_parts.next().unwrap_or_default();
    let status = status_parts
        .next()
        .and_then(|value| value.parse::<u16>().ok())
        .ok_or_else(|| "BACKEND_INVALID_HTTP".to_string())?;
    if !version.starts_with("HTTP/1.") || !(100..=599).contains(&status) {
        return Err("BACKEND_INVALID_HTTP".into());
    }
    let status_text = status_parts.next().unwrap_or_default().to_string();
    let mut headers = Vec::new();
    let mut chunked = false;
    let mut content_length = None;
    for line in lines {
        let (name, value) = line
            .split_once(':')
            .ok_or_else(|| "BACKEND_INVALID_HTTP".to_string())?;
        let name = name.trim().to_ascii_lowercase();
        let value = value.trim().to_string();
        if name == "transfer-encoding" && value.to_ascii_lowercase().contains("chunked") {
            chunked = true;
        }
        if name == "content-length" {
            content_length = value.parse::<usize>().ok();
        }
        if !matches!(name.as_str(), "connection" | "transfer-encoding") {
            headers.push((name, value));
        }
    }

    let encoded_body = &raw[boundary + 4..];
    let body = if chunked {
        decode_chunked(encoded_body)?
    } else if let Some(length) = content_length {
        if encoded_body.len() < length {
            return Err("BACKEND_TRUNCATED_RESPONSE".into());
        }
        encoded_body[..length].to_vec()
    } else {
        encoded_body.to_vec()
    };
    Ok(BackendResponse {
        status,
        status_text,
        headers,
        body,
    })
}

fn decode_chunked(encoded: &[u8]) -> Result<Vec<u8>, String> {
    let mut decoded = Vec::new();
    let mut offset = 0usize;
    loop {
        let line_end = encoded[offset..]
            .windows(2)
            .position(|window| window == b"\r\n")
            .map(|position| offset + position)
            .ok_or_else(|| "BACKEND_INVALID_CHUNKED_RESPONSE".to_string())?;
        let size_text = std::str::from_utf8(&encoded[offset..line_end])
            .map_err(|_| "BACKEND_INVALID_CHUNKED_RESPONSE".to_string())?;
        let size =
            usize::from_str_radix(size_text.split(';').next().unwrap_or_default().trim(), 16)
                .map_err(|_| "BACKEND_INVALID_CHUNKED_RESPONSE".to_string())?;
        offset = line_end + 2;
        if size == 0 {
            return Ok(decoded);
        }
        let end = offset
            .checked_add(size)
            .ok_or_else(|| "BACKEND_INVALID_CHUNKED_RESPONSE".to_string())?;
        if end + 2 > encoded.len() || &encoded[end..end + 2] != b"\r\n" {
            return Err("BACKEND_TRUNCATED_RESPONSE".into());
        }
        decoded.extend_from_slice(&encoded[offset..end]);
        if decoded.len() > MAX_RESPONSE_BYTES as usize {
            return Err("BACKEND_RESPONSE_TOO_LARGE".into());
        }
        offset = end + 2;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_only_local_nyra_paths_and_methods() {
        assert!(validate_path("/api/tasks?limit=10").is_ok());
        assert!(validate_path("/health").is_ok());
        assert!(validate_path("http://example.com/api/tasks").is_err());
        assert!(validate_path("/api/%2e%2e/private").is_err());
        assert!(validate_method("PATCH").is_ok());
        assert!(validate_method("OPTIONS").is_err());
    }

    #[test]
    fn preserves_status_headers_and_json_body() {
        let response = parse_response(
            b"HTTP/1.1 409 Conflict\r\nContent-Type: text/plain\r\nContent-Length: 4\r\n\r\nbusy",
        )
        .expect("valid response");
        assert_eq!(response.status, 409);
        assert_eq!(response.status_text, "Conflict");
        assert_eq!(response.body, b"busy");
    }

    #[test]
    fn decodes_chunked_response_body() {
        let response = parse_response(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Type: text/plain\r\n\r\n4\r\nNYRA\r\n1\r\n!\r\n0\r\n\r\n",
        )
        .expect("chunked response");
        assert_eq!(response.body, b"NYRA!");
    }
}
