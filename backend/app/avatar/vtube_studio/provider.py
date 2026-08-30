from __future__ import annotations
import asyncio, json, logging, os, time
from pathlib import Path
from typing import Any

from app.avatar.controller import AvatarState
from app.avatar.vtube_studio.auth import VTSAuth
from app.avatar.vtube_studio.client import VTubeStudioClient
from app.avatar.vtube_studio.controller import inject_state
from app.avatar.vtube_studio.models import VTSConnectionState, VTubeStudioConfig
from app.avatar.vtube_studio.motions import list_hotkeys, trigger_hotkey
from app.avatar.vtube_studio.parameters import discover_ids, resolve_mapping
from app.core.paths import DATA_ROOT

CONFIG_PATH=DATA_ROOT/"vtube-studio-settings.json"; TOKEN_PATH=DATA_ROOT/"secrets/vtube_studio_token.json"
logger=logging.getLogger("nyra")


class VTubeStudioAvatarProvider:
    def __init__(self, config: VTubeStudioConfig|None=None) -> None:
        self.config=config or self._load(); self.client=VTubeStudioClient(self.config.host,self.config.port)
        self.auth=VTSAuth(self.client,TOKEN_PATH); self.state=VTSConnectionState.DISABLED if not self.config.enabled else VTSConnectionState.CONNECTING
        self.mapping: dict[str,list[str]]={}; self.model: dict={}; self.hotkeys: list[dict]=[]; self.parameters: list[str]=[]
        self.last_error: str|None=None; self.last_update=0.0; self.update_hz=0.0; self.last_model_check=0.0
        self.authenticated=False; self.last_cursor: dict[str,float]|None=None
        self.last_avatar_mode: str|None=None; self.last_hotkey: str|None=None
        self.presence: dict[str,Any]={"state":"INTERNAL_ACTIVE","alpha":"UNKNOWN","fallback_active":True}
        self._task: asyncio.Task | None=None; self._connect_lock=asyncio.Lock(); self._authorization_requested=False

    @property
    def name(self): return "vtube_studio"
    async def available(self):
        try:
            reader,writer=await asyncio.wait_for(asyncio.open_connection(self.config.host,self.config.port),.35); writer.close(); await writer.wait_closed(); return True
        except (OSError,TimeoutError): return False

    async def connect(self, request_authorization: bool=False) -> dict:
        async with self._connect_lock:
            return await self._connect(request_authorization)

    async def _connect(self, request_authorization: bool=False) -> dict:
        if not self.config.enabled: self.state=VTSConnectionState.DISABLED; return self.status()
        if not self.installed(): self.state=VTSConnectionState.NOT_INSTALLED; return self.status()
        self.state=VTSConnectionState.CONNECTING; self.authenticated=False
        try:
            await self.client.connect(); self.state=VTSConnectionState.CONNECTED; api=await self.auth.state()
            if not api.get("active",True): self.state=VTSConnectionState.API_DISABLED; return self.status()
            token=self.auth.load_token()
            if token:
                self.state=VTSConnectionState.AUTHENTICATING
                self.authenticated=await self.auth.authenticate(token)
                if not self.authenticated and request_authorization:
                    self.auth.clear_token(); token=None
            if not token and request_authorization:
                self._authorization_requested=True
                self.state=VTSConnectionState.AUTHENTICATING
                token=await self.auth.request_token()
            if not token: self.state=VTSConnectionState.AUTH_REQUIRED; return self.status()
            if not self.authenticated:
                self.state=VTSConnectionState.AUTHENTICATING
                self.authenticated=await self.auth.authenticate(token)
            if not self.authenticated: self.state=VTSConnectionState.AUTH_REQUIRED; return self.status()
            self.state=VTSConnectionState.AUTHENTICATED
            await self._refresh_model_metadata(force=True)
            loaded=bool(self.model.get("modelLoaded")); self.state=VTSConnectionState.READY if loaded else VTSConnectionState.MODEL_MISSING
            self.last_error=None
            logger.info("vtube_studio_ready",extra={"model":self.model.get("modelName"),"parameters":sum(len(v) for v in self.mapping.values())})
        except Exception as error:
            self.last_error=type(error).__name__; self.authenticated=False
            self.state=VTSConnectionState.API_DISABLED if isinstance(error,OSError) else VTSConnectionState.ERROR
            await self.client.close()
        return self.status()

    async def start(self) -> None:
        if self._task is None or self._task.done(): self._task=asyncio.create_task(self._reconnect_loop())
    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try: await self._task
            except asyncio.CancelledError: pass
            self._task=None
        await self.client.close()
    async def _reconnect_loop(self) -> None:
        backoff=(1,2,5,10,30); attempt=0
        while True:
            ready=self.state in {VTSConnectionState.READY,VTSConnectionState.MODEL_LOADED}
            if self.config.enabled and self.config.auto_connect and ready:
                try:
                    api=await self.auth.state()
                    if not api.get("currentSessionAuthenticated",False):
                        self.authenticated=False; self.state=VTSConnectionState.RECONNECTING; await self.client.close()
                    elif time.monotonic()-self.last_model_check>=5:
                        await self._refresh_model_metadata()
                except Exception as error:
                    self.last_error=type(error).__name__; self.authenticated=False; self.state=VTSConnectionState.RECONNECTING; await self.client.close()
            elif self.config.enabled and self.config.auto_connect and self.state!=VTSConnectionState.AUTH_REQUIRED:
                if attempt: self.state=VTSConnectionState.RECONNECTING
                request_authorization=not self.auth.load_token() and not self._authorization_requested
                await self.connect(request_authorization)
                attempt=0 if self.state in {VTSConnectionState.READY,VTSConnectionState.MODEL_LOADED} else min(attempt+1,len(backoff)-1)
            await asyncio.sleep(backoff[attempt] if self.config.enabled else 5)

    async def authorize(self): self._authorization_requested=True; return await self.connect(request_authorization=True)
    async def disconnect(self): await self.client.close(); self.authenticated=False; self.state=VTSConnectionState.DISABLED if not self.config.enabled else VTSConnectionState.RECONNECTING
    async def apply(self,state:AvatarState):
        if not self.config.enabled or self.config.renderer in {"CURRENT","INTERNAL"}: return
        now=time.monotonic(); minimum=1/max(15,self.config.target_fps)
        if self.state not in {VTSConnectionState.READY,VTSConnectionState.MODEL_LOADED}:
            if self.config.auto_connect and now-self.last_update>1: await self.connect(False)
            self.last_update=now; return
        mode=self._avatar_mode(state)
        started=time.monotonic()
        try:
            if mode!=self.last_avatar_mode:
                self.last_avatar_mode=mode
                hotkey=self._resolve_hotkey(mode)
                if hotkey:
                    await trigger_hotkey(self.client,hotkey)
                    self.last_hotkey=hotkey
            mouth_changed=state.mouth_open<=0 or now-self.last_update>=minimum
            if self.config.lip_sync and self.mapping.get("mouth_open") and mouth_changed:
                await inject_state(self.client,state,self.mapping)
        except Exception as error: self.last_error=type(error).__name__; self.state=VTSConnectionState.RECONNECTING; await self.client.close()
        elapsed=time.monotonic()-started; self.last_update=now; self.update_hz=round(1/max(minimum,elapsed),1)

    async def apply_cursor(self, state: AvatarState, x: float, y: float) -> bool:
        # Tracking stays owned by VTube Studio. Cursor attention is retained in
        # config for compatibility, but is deliberately a no-op for Presence V1.
        self.last_cursor={"input_x":round(float(x),4),"input_y":round(float(y),4),"applied":False}
        return False

    async def _refresh_model_metadata(self, force: bool=False) -> None:
        previous_id=self.model.get("modelID") or self.model.get("modelId")
        model=(await self.client.call("CurrentModelRequest")).get("data",{})
        model_id=model.get("modelID") or model.get("modelId")
        changed=force or model_id!=previous_id or bool(model.get("modelLoaded"))!=bool(self.model.get("modelLoaded"))
        self.model=model; self.last_model_check=time.monotonic()
        if changed and model.get("modelLoaded"):
            params=(await self.client.call("InputParameterListRequest")).get("data",{})
            available=discover_ids(params); self.parameters=sorted(available); self.mapping=resolve_mapping(available)
            self.hotkeys=await list_hotkeys(self.client)
            self.last_avatar_mode=None
            self.state=VTSConnectionState.READY
            logger.info("vtube_studio_model_detected",extra={"model":model.get("modelName"),"model_id":model_id,"hotkeys":len(self.hotkeys)})
        elif not model.get("modelLoaded"):
            self.parameters=[]; self.mapping={}; self.hotkeys=[]; self.state=VTSConnectionState.MODEL_MISSING

    @staticmethod
    def _avatar_mode(state: AvatarState) -> str:
        value=(state.animation or state.neural_link or "idle").lower()
        return "alert" if value in {"attention","alert"} else value if value in {"idle","listening","thinking","speaking"} else "idle"

    def _resolve_hotkey(self, mode: str) -> str|None:
        configured=self.config.state_hotkeys.get(mode) or self.config.state_hotkeys.get(mode.upper())
        names=[configured,f"NYRA_{mode.upper()}"] if configured else [f"NYRA_{mode.upper()}"]
        for candidate in names:
            for hotkey in self.hotkeys:
                if str(hotkey.get("name","")).casefold()==str(candidate).casefold():
                    return str(hotkey.get("hotkeyID") or hotkey.get("hotkeyId") or "") or None
        return None

    def record_presence(self, status: dict[str,Any]) -> dict[str,Any]:
        allowed={"state","alpha","fallback_active","sender","width","height","format","sender_fps","receiver_fps","frame_count","dropped_frames","last_frame_age_ms","adapter_match","sender_adapter","receiver_adapter","memory_bytes","error"}
        previous_state=self.presence.get("state"); previous_sender=self.presence.get("sender")
        self.presence={key:value for key,value in status.items() if key in allowed}; self.presence["reported_at"]=time.time()
        state=self.presence.get("state"); sender=self.presence.get("sender")
        if state!=previous_state or sender!=previous_sender:
            if state=="VTS_CONNECTING": logger.info("spout_sender_detected",extra={"sender":sender})
            elif state=="VTS_WAITING_FRAMES": logger.info("spout_receiver_connected",extra={"sender":sender})
            elif state=="VTS_ACTIVE": logger.info("vts_presence_active",extra={"sender":sender,"alpha":self.presence.get("alpha")})
            elif state in {"VTS_DEGRADED","FALLBACK_INTERNAL"}: logger.warning("vts_presence_fallback_internal",extra={"reason":self.presence.get("error")})
        return self.presence

    def update(self,config:VTubeStudioConfig):
        self.config=config; self.client.host=config.host; self.client.port=config.port; self._save()
    def status(self):
        ready=self.state in {VTSConnectionState.READY,VTSConnectionState.MODEL_LOADED}
        return {"provider":"vtube_studio","state":self.state,"connected":self.client.socket is not None,
                "authenticated":self.authenticated,"model_loaded":ready,"model":self.model.get("modelName"),
                "model_id":self.model.get("modelID") or self.model.get("modelId"),
                "parameter_count":sum(len(v) for v in self.mapping.values()),"mapping":self.mapping,"update_hz":self.update_hz,
                "parameters":self.parameters,"hotkeys":[{"id":item.get("hotkeyID") or item.get("hotkeyId"),"name":item.get("name"),"type":item.get("type")} for item in self.hotkeys],
                "last_hotkey":self.last_hotkey,"vts_presence":self.presence,
                "last_error":self.last_error,"requests_sent":getattr(self.client,"requests_sent",0),"last_request":getattr(self.client,"last_message_type",None),
                "last_cursor":self.last_cursor,
                "config":self.config.model_dump(mode="json"),"token_configured":bool(self.auth.load_token())}
    def readiness(self)->dict[str,Any]:
        return {**self.status(),"installed":self.installed(),"api_url":self.client.url,"automatic_install":False}
    @staticmethod
    def installed() -> bool:
        paths=[Path("C:/Program Files (x86)/Steam/steamapps/common/VTube Studio/VTube Studio.exe"),Path("C:/Program Files/Steam/steamapps/common/VTube Studio/VTube Studio.exe")]
        return any(p.is_file() for p in paths)
    def _load(self):
        try:return VTubeStudioConfig.model_validate_json(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError,ValueError):return VTubeStudioConfig()
    def _save(self):
        CONFIG_PATH.parent.mkdir(parents=True,exist_ok=True); tmp=CONFIG_PATH.with_suffix(".tmp"); tmp.write_text(self.config.model_dump_json(indent=2)+"\n",encoding="utf-8"); os.replace(tmp,CONFIG_PATH)
