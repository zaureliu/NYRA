//! Narrow loopback transport for the packaged conversation channel.
//!
//! The backend rejects cross-site browser traffic from the packaged WebView.
//! This module relays the existing event WebSocket through Tauri events. REST
//! calls use the central allowlisted backend transport.

use serde_json::Value;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::Duration;
use tauri::{AppHandle, Emitter};

const BACKEND_ADDRESS: &str = "127.0.0.1:8000";
const EVENTS_PATH: &str = "/api/ws";
const MAX_EVENT_BYTES: usize = 4 * 1024 * 1024;

struct BridgeState {
    started: AtomicBool,
    connected: AtomicBool,
}

pub struct ConversationBridge {
    state: Arc<BridgeState>,
}

impl ConversationBridge {
    pub fn new() -> Self {
        Self {
            state: Arc::new(BridgeState {
                started: AtomicBool::new(false),
                connected: AtomicBool::new(false),
            }),
        }
    }

    pub fn start(&self, app: AppHandle) -> bool {
        if self
            .state
            .started
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .is_ok()
        {
            let state = Arc::clone(&self.state);
            if thread::Builder::new()
                .name("nyra-conversation-events".into())
                .spawn(move || relay_forever(app, state))
                .is_err()
            {
                self.state.started.store(false, Ordering::SeqCst);
            }
        }
        self.state.connected.load(Ordering::SeqCst)
    }
}

fn connect(timeout: Duration) -> Result<TcpStream, String> {
    let address: SocketAddr = BACKEND_ADDRESS
        .parse()
        .map_err(|_| "CONVERSATION_ADDRESS_INVALID".to_string())?;
    TcpStream::connect_timeout(&address, timeout)
        .map_err(|_| "CONVERSATION_BACKEND_OFFLINE".to_string())
}

fn relay_forever(app: AppHandle, state: Arc<BridgeState>) {
    loop {
        match connect_events() {
            Ok(stream) => {
                set_connected(&app, &state, true);
                let _ = relay_connection(&app, stream);
            }
            Err(_) => {}
        }
        set_connected(&app, &state, false);
        thread::sleep(Duration::from_secs(1));
    }
}

fn set_connected(app: &AppHandle, state: &BridgeState, connected: bool) {
    if state.connected.swap(connected, Ordering::SeqCst) != connected {
        let _ = app.emit("nyra-backend-connection", connected);
    }
}

fn connect_events() -> Result<TcpStream, String> {
    connect_local_websocket(EVENTS_PATH)
}

pub(crate) fn connect_local_websocket(path: &str) -> Result<TcpStream, String> {
    if !matches!(path, "/api/ws" | "/api/stt/stream") {
        return Err("EVENTS_PATH_NOT_ALLOWED".into());
    }
    let mut stream = connect(Duration::from_secs(2))?;
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .and_then(|_| stream.set_write_timeout(Some(Duration::from_secs(5))))
        .map_err(|_| "EVENTS_TIMEOUT_SETUP_FAILED".to_string())?;
    let request = format!(
        "GET {path} HTTP/1.1\r\nHost: {BACKEND_ADDRESS}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
    );
    stream
        .write_all(request.as_bytes())
        .map_err(|_| "EVENTS_HANDSHAKE_WRITE_FAILED".to_string())?;
    let header = read_http_header(&mut stream)?;
    if !header.starts_with(b"HTTP/1.1 101") {
        return Err("EVENTS_HANDSHAKE_REJECTED".into());
    }
    stream
        .set_read_timeout(None)
        .map_err(|_| "EVENTS_TIMEOUT_CLEAR_FAILED".to_string())?;
    Ok(stream)
}

fn read_http_header(stream: &mut TcpStream) -> Result<Vec<u8>, String> {
    let mut header = Vec::with_capacity(1024);
    let mut byte = [0u8; 1];
    while header.len() < 16 * 1024 {
        stream
            .read_exact(&mut byte)
            .map_err(|_| "EVENTS_HANDSHAKE_READ_FAILED".to_string())?;
        header.push(byte[0]);
        if header.ends_with(b"\r\n\r\n") {
            return Ok(header);
        }
    }
    Err("EVENTS_HANDSHAKE_TOO_LARGE".into())
}

fn relay_connection(app: &AppHandle, mut stream: TcpStream) -> Result<(), String> {
    let mut fragmented = Vec::new();
    loop {
        let frame = read_frame(&mut stream)?;
        match frame.opcode {
            0x0 => {
                if fragmented.len() + frame.payload.len() > MAX_EVENT_BYTES {
                    return Err("EVENT_TOO_LARGE".into());
                }
                fragmented.extend_from_slice(&frame.payload);
                if frame.finished {
                    emit_event(app, &fragmented)?;
                    fragmented.clear();
                }
            }
            0x1 => {
                if frame.finished {
                    emit_event(app, &frame.payload)?;
                } else {
                    fragmented = frame.payload;
                }
            }
            0x8 => return Err("EVENTS_CLOSED".into()),
            0x9 => write_masked_frame(&mut stream, 0xA, &frame.payload)?,
            0xA => {}
            _ => {}
        }
    }
}

fn emit_event(app: &AppHandle, payload: &[u8]) -> Result<(), String> {
    let event: Value =
        serde_json::from_slice(payload).map_err(|_| "EVENT_INVALID_JSON".to_string())?;
    app.emit("nyra-backend-event", event)
        .map_err(|_| "EVENT_EMIT_FAILED".to_string())
}

pub(crate) struct WebSocketFrame {
    pub(crate) finished: bool,
    pub(crate) opcode: u8,
    pub(crate) payload: Vec<u8>,
}

pub(crate) fn read_frame(stream: &mut TcpStream) -> Result<WebSocketFrame, String> {
    let mut prefix = [0u8; 2];
    stream
        .read_exact(&mut prefix)
        .map_err(|_| "EVENT_FRAME_READ_FAILED".to_string())?;
    let finished = prefix[0] & 0x80 != 0;
    let opcode = prefix[0] & 0x0F;
    let masked = prefix[1] & 0x80 != 0;
    let mut length = u64::from(prefix[1] & 0x7F);
    if length == 126 {
        let mut extended = [0u8; 2];
        stream
            .read_exact(&mut extended)
            .map_err(|_| "EVENT_FRAME_READ_FAILED".to_string())?;
        length = u64::from(u16::from_be_bytes(extended));
    } else if length == 127 {
        let mut extended = [0u8; 8];
        stream
            .read_exact(&mut extended)
            .map_err(|_| "EVENT_FRAME_READ_FAILED".to_string())?;
        length = u64::from_be_bytes(extended);
    }
    if length > MAX_EVENT_BYTES as u64 {
        return Err("EVENT_TOO_LARGE".into());
    }
    let mut mask = [0u8; 4];
    if masked {
        stream
            .read_exact(&mut mask)
            .map_err(|_| "EVENT_FRAME_READ_FAILED".to_string())?;
    }
    let mut payload = vec![0u8; length as usize];
    stream
        .read_exact(&mut payload)
        .map_err(|_| "EVENT_FRAME_READ_FAILED".to_string())?;
    if masked {
        for (index, byte) in payload.iter_mut().enumerate() {
            *byte ^= mask[index % 4];
        }
    }
    Ok(WebSocketFrame {
        finished,
        opcode,
        payload,
    })
}

fn write_masked_frame(stream: &mut TcpStream, opcode: u8, payload: &[u8]) -> Result<(), String> {
    if payload.len() > 125 {
        return Err("EVENT_CONTROL_FRAME_TOO_LARGE".into());
    }
    let mask = [0x13u8, 0x37, 0x73, 0x91];
    let mut frame = Vec::with_capacity(payload.len() + 6);
    frame.push(0x80 | opcode);
    frame.push(0x80 | payload.len() as u8);
    frame.extend_from_slice(&mask);
    frame.extend(
        payload
            .iter()
            .enumerate()
            .map(|(index, byte)| byte ^ mask[index % 4]),
    );
    stream
        .write_all(&frame)
        .map_err(|_| "EVENT_FRAME_WRITE_FAILED".to_string())
}
