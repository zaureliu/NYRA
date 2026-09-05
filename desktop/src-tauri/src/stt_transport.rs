//! Audio transport only. Existing WebView capture owns the microphone; the
//! backend alone owns providers and credentials. Fixed loopback destination.
use crate::conversation_transport::{connect_local_websocket, read_frame};
use serde_json::{json, Value};
use std::io::Write;
use std::net::{Shutdown, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;
use tauri::ipc::Channel;

const MAX_AUDIO_BYTES: usize = 32768;
type Outbound = (u8, Vec<u8>);

struct Connection {
    owner: String,
    id: String,
    socket: TcpStream,
    sender: mpsc::SyncSender<Outbound>,
    closed: Arc<AtomicBool>,
    workers: Vec<JoinHandle<()>>,
}

impl Connection {
    fn stop(self) {
        self.closed.store(true, Ordering::SeqCst);
        let _ = self.socket.shutdown(Shutdown::Both);
        for worker in self.workers {
            let _ = worker.join();
        }
    }
}

#[derive(Clone, Default)]
pub struct SttBridge {
    connection: Arc<Mutex<Option<Connection>>>,
}

fn validate_identity(value: &str, limit: usize) -> Result<(), String> {
    if value.is_empty()
        || value.len() > limit
        || !value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'-'))
    {
        return Err("STT_INVALID_IDENTIFIER".into());
    }
    Ok(())
}

fn write_frame(stream: &mut TcpStream, opcode: u8, payload: &[u8]) -> Result<(), String> {
    if payload.len() > MAX_AUDIO_BYTES {
        return Err("STT_FRAME_TOO_LARGE".into());
    }
    let mut mask = [0u8; 4];
    getrandom::fill(&mut mask).map_err(|_| "STT_MASK_FAILED".to_string())?;
    let mut frame = Vec::with_capacity(payload.len() + 8);
    frame.push(0x80 | opcode);
    if payload.len() < 126 {
        frame.push(0x80 | payload.len() as u8);
    } else {
        frame.push(0x80 | 126);
        frame.extend_from_slice(&(payload.len() as u16).to_be_bytes());
    }
    frame.extend_from_slice(&mask);
    frame.extend(
        payload
            .iter()
            .enumerate()
            .map(|(index, byte)| byte ^ mask[index % 4]),
    );
    stream
        .write_all(&frame)
        .map_err(|_| "STT_WRITE_FAILED".to_string())
}

impl SttBridge {
    pub fn open(
        &self,
        owner: String,
        id: String,
        ticket: String,
        channel: Channel<Value>,
    ) -> Result<(), String> {
        validate_identity(&id, 80)?;
        validate_identity(&ticket, 128)?;
        let mut slot = self
            .connection
            .lock()
            .map_err(|_| "STT_LOCK_FAILED".to_string())?;
        if slot
            .as_ref()
            .is_some_and(|connection| !connection.closed.load(Ordering::SeqCst))
        {
            return Err("STT_CAPTURE_BUSY".into());
        }
        if let Some(previous) = slot.take() {
            previous.stop();
        }
        let mut socket = connect_local_websocket("/api/stt/stream")?;
        socket
            .set_write_timeout(Some(Duration::from_secs(2)))
            .map_err(|_| "STT_TIMEOUT_FAILED".to_string())?;
        write_frame(
            &mut socket,
            1,
            json!({"ticket": ticket}).to_string().as_bytes(),
        )?;
        let mut reader = socket
            .try_clone()
            .map_err(|_| "STT_SOCKET_FAILED".to_string())?;
        let mut writer = socket
            .try_clone()
            .map_err(|_| "STT_SOCKET_FAILED".to_string())?;
        let (sender, receiver) = mpsc::sync_channel::<Outbound>(32);
        let closed = Arc::new(AtomicBool::new(false));
        let writing_closed = Arc::clone(&closed);
        let writing_channel = channel.clone();
        let sender_worker = thread::Builder::new()
            .name("nyra-stt-send".into())
            .spawn(move || {
                while !writing_closed.load(Ordering::SeqCst) {
                    match receiver.recv_timeout(Duration::from_millis(100)) {
                        Ok((opcode, data)) => {
                            if write_frame(&mut writer, opcode, &data).is_err() {
                                break;
                            }
                        }
                        Err(mpsc::RecvTimeoutError::Timeout) => continue,
                        Err(_) => break,
                    }
                }
                if !writing_closed.swap(true, Ordering::SeqCst) {
                    let _ = writing_channel.send(
                        json!({"type":"error", "message":"STT local connection interrupted"}),
                    );
                }
                let _ = writer.shutdown(Shutdown::Both);
            })
            .map_err(|_| "STT_THREAD_FAILED".to_string())?;
        let reading_closed = Arc::clone(&closed);
        let pong_sender = sender.clone();
        let reader_worker = thread::Builder::new()
            .name("nyra-stt-receive".into())
            .spawn(move || {
                let mut fragments = Vec::new();
                while !reading_closed.load(Ordering::SeqCst) {
                    let Ok(frame) = read_frame(&mut reader) else {
                        break;
                    };
                    match frame.opcode {
                        0 | 1 => {
                            if fragments.len() + frame.payload.len() > 1024 * 1024 {
                                break;
                            }
                            fragments.extend_from_slice(&frame.payload);
                            if frame.finished {
                                let Ok(value) = serde_json::from_slice::<Value>(&fragments) else {
                                    break;
                                };
                                let terminal =
                                    value["type"] == "result" || value["type"] == "error";
                                if channel.send(value).is_err() {
                                    break;
                                }
                                fragments.clear();
                                if terminal {
                                    reading_closed.store(true, Ordering::SeqCst);
                                    break;
                                }
                            }
                        }
                        8 => break,
                        9 => {
                            if pong_sender.try_send((10, frame.payload)).is_err() {
                                break;
                            }
                        }
                        10 => {}
                        _ => break,
                    }
                }
                if !reading_closed.swap(true, Ordering::SeqCst) {
                    let _ = channel
                        .send(json!({"type":"error", "message":"STT local connection closed"}));
                }
                let _ = reader.shutdown(Shutdown::Both);
            });
        match reader_worker {
            Ok(reader_worker) => {
                *slot = Some(Connection {
                    owner,
                    id,
                    socket,
                    sender,
                    closed,
                    workers: vec![sender_worker, reader_worker],
                });
                Ok(())
            }
            Err(_) => {
                closed.store(true, Ordering::SeqCst);
                let _ = socket.shutdown(Shutdown::Both);
                let _ = sender_worker.join();
                Err("STT_THREAD_FAILED".into())
            }
        }
    }

    pub fn send(&self, owner: &str, id: &str, audio: Vec<u8>, end: bool) -> Result<(), String> {
        if !end && (audio.is_empty() || audio.len() > MAX_AUDIO_BYTES || audio.len() % 2 != 0) {
            return Err("STT_INVALID_AUDIO_FRAME".into());
        }
        let slot = self
            .connection
            .lock()
            .map_err(|_| "STT_LOCK_FAILED".to_string())?;
        let connection = slot
            .as_ref()
            .filter(|connection| connection.owner == owner && connection.id == id)
            .ok_or_else(|| "STT_STREAM_NOT_OWNED".to_string())?;
        let frame = if end {
            (1, b"{\"type\":\"end\"}".to_vec())
        } else {
            (2, audio)
        };
        connection.sender.try_send(frame).map_err(|_| {
            connection.closed.store(true, Ordering::SeqCst);
            let _ = connection.socket.shutdown(Shutdown::Both);
            "STT_QUEUE_FULL_OR_CLOSED".to_string()
        })
    }

    pub fn close(&self, owner: &str, id: &str) {
        if let Ok(mut slot) = self.connection.lock() {
            if slot
                .as_ref()
                .is_some_and(|connection| connection.owner == owner && connection.id == id)
            {
                if let Some(connection) = slot.take() {
                    connection.stop();
                }
            }
        }
    }

    pub fn stop(&self) {
        if let Ok(mut slot) = self.connection.lock() {
            if let Some(connection) = slot.take() {
                connection.stop();
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    #[test]
    fn rejects_injected_identifiers_and_remote_path() {
        assert!(validate_identity("one-time_token", 128).is_ok());
        for value in [
            "",
            "token\r\nAuthorization: injected",
            "https://remote",
            "x?key=y",
        ] {
            assert!(validate_identity(value, 128).is_err());
        }
        assert!(connect_local_websocket("https://remote").is_err());
    }

    #[test]
    fn binary_pcm_uses_bounded_masked_websocket_frames() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let sender = thread::spawn(move || {
            let mut stream = TcpStream::connect(address).unwrap();
            write_frame(&mut stream, 2, &vec![42; 8192]).unwrap();
            assert!(write_frame(&mut stream, 2, &vec![0; MAX_AUDIO_BYTES + 1]).is_err());
        });
        let (mut socket, _) = listener.accept().unwrap();
        let frame = read_frame(&mut socket).unwrap();
        assert_eq!(frame.opcode, 2);
        assert_eq!(frame.payload, vec![42; 8192]);
        assert!(frame.finished);
        sender.join().unwrap();
    }

    #[test]
    fn closed_bridge_rejects_audio_and_shutdown_is_idempotent() {
        let bridge = SttBridge::default();
        assert!(bridge.send("main", "session", vec![0, 0], false).is_err());
        assert!(bridge.send("main", "session", vec![0], false).is_err());
        bridge.stop();
        bridge.stop();
    }
}
