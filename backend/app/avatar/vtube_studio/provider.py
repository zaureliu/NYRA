from __future__ import annotations
import asyncio, json, logging, os, time
from collections import deque
from pathlib import Path
from typing import Any

from app.avatar.controller import AvatarState
from app.avatar.vtube_studio.auth import VTSAuth
from app.avatar.vtube_studio.client import VTubeStudioClient
from app.avatar.vtube_studio.controller import inject_emotion, inject_mouse_tracking, inject_state
from app.avatar.vtube_studio.expressions import list_expressions, set_expression
from app.avatar.vtube_studio.models import VTSConnectionState, VTubeStudioConfig
from app.avatar.vtube_studio.mouse_tracking import MouseTrackingController
from app.avatar.vtube_studio.motions import list_hotkeys, trigger_hotkey
from app.avatar.vtube_studio.parameters import discover_ids, mouse_parameter_values, resolve_mapping
from app.core.paths import DATA_ROOT
from app.persona_runtime.models import NyraEmotion

CONFIG_PATH=DATA_ROOT/"vtube-studio-settings.json"; TOKEN_PATH=DATA_ROOT/"secrets/vtube_studio_token.json"
logger=logging.getLogger("nyra")


class VTubeStudioAvatarProvider:
    def __init__(self, config: VTubeStudioConfig|None=None) -> None:
        self.config=config or self._load(); self.client=VTubeStudioClient(self.config.host,self.config.port)
        self.auth=VTSAuth(self.client,TOKEN_PATH); self.state=VTSConnectionState.DISABLED if not self.config.enabled else VTSConnectionState.CONNECTING
        self.mapping: dict[str,list[str]]={}; self.model: dict={}; self.hotkeys: list[dict]=[]; self.expressions: list[dict]=[]; self.parameters: list[str]=[]
        self.last_error: str|None=None; self.last_update=0.0; self.update_hz=0.0; self.last_model_check=0.0
        self.authenticated=False; self.last_cursor: dict[str,Any]|None=None
        self.mouse_tracking=MouseTrackingController(self.config.mouse_tracking)
        self._tracking_last_request=0.0; self._tracking_injections=0
        self._tracking_cost_ms: deque[float]=deque(maxlen=600); self._tracking_timestamps: deque[float]=deque(maxlen=300)
        self.last_avatar_mode: str|None=None; self.last_hotkey: str|None=None
        self.current_emotion="neutral"; self.current_emotion_intensity=0.0; self.current_transition: dict[str,Any]={}
        self.last_emotion_target: dict[str,Any]|None=None; self.last_emotion_update=0.0
        self.last_emotion_presentation: dict[str,Any]={"kind":"offline","target":None,"applied":False,"fallback":"not_synchronized"}
        self.presence: dict[str,Any]={"state":"VTS_UNAVAILABLE","alpha":"UNKNOWN","vts_active":False}
        self._presence_ever_active=False
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
                    logger.warning("vtube_studio_token_rejected_reauthorizing")
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
            self._authorization_requested=False
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
            elif self.config.enabled and self.config.auto_connect:
                if attempt: self.state=VTSConnectionState.RECONNECTING
                # A stored token can be revoked by VTube Studio. Allow exactly
                # one automatic authorization request per failed session so a
                # stale token cannot pin Presence in AUTH_REQUIRED forever.
                request_authorization=not self._authorization_requested
                await self.connect(request_authorization)
                attempt=0 if self.state in {VTSConnectionState.READY,VTSConnectionState.MODEL_LOADED} else min(attempt+1,len(backoff)-1)
            await asyncio.sleep(backoff[attempt] if self.config.enabled else 5)

    async def authorize(self): self._authorization_requested=True; return await self.connect(request_authorization=True)
    async def disconnect(self): await self.client.close(); self.authenticated=False; self.state=VTSConnectionState.DISABLED if not self.config.enabled else VTSConnectionState.RECONNECTING
    async def apply(self,state:AvatarState):
        if not self.config.enabled: return
        now=time.monotonic(); minimum=1/max(15,self.config.target_fps)
        if self.state not in {VTSConnectionState.READY,VTSConnectionState.MODEL_LOADED}:
            # Connection and backoff belong exclusively to _reconnect_loop.
            # A visual state update must never block Persona Runtime while VTS
            # is offline or reconnecting.
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

    async def apply_emotion(self, emotion: str, intensity: float, transition: dict[str,Any]|None=None) -> dict[str,Any]:
        """Apply, but never infer, the canonical Persona Runtime emotion."""
        try: selected=NyraEmotion(str(emotion).casefold()).value
        except ValueError: selected=NyraEmotion.NEUTRAL.value
        self.current_emotion=selected; self.current_emotion_intensity=min(.65,max(0.0,float(intensity)))
        self.current_transition=dict(transition or {})
        return await self._synchronize_emotion()

    async def _synchronize_emotion(self, *, force: bool=False) -> dict[str,Any]:
        model_id=self.model.get("modelID") or self.model.get("modelId")
        if not self.config.enabled:
            return self._record_emotion_result("disabled",None,False,"avatar_expression_disabled",model_id)
        if self.state not in {VTSConnectionState.READY,VTSConnectionState.MODEL_LOADED}:
            return self._record_emotion_result("offline",None,False,"vts_unavailable",model_id)
        target=self._resolve_emotion_target(self.current_emotion)
        now=time.monotonic(); cooldown=max(0,float(self.current_transition.get("cooldown_ms") or 0)/1000)
        same_presentation=(
            self.last_emotion_presentation.get("emotion")==self.current_emotion
            and abs(float(self.last_emotion_presentation.get("intensity") or 0)-self.current_emotion_intensity)<.001
        )
        if not force and same_presentation and target==self.last_emotion_target and now-self.last_emotion_update<cooldown:
            return self.last_emotion_presentation
        try:
            if target!=self.last_emotion_target:
                await self._deactivate_last_emotion()
            kind=str(target.get("kind") or "neutral"); name=target.get("target")
            applied=False; fallback=None
            if kind=="hotkey" and name:
                await trigger_hotkey(self.client,str(target["id"])); applied=True
            elif kind=="expression" and name:
                await set_expression(self.client,str(target["file"]),True); applied=True
            elif kind=="parameter":
                await inject_emotion(self.client,self.current_emotion,self.current_emotion_intensity,self.mapping); applied=True
            elif kind=="neutral":
                applied=self.current_emotion=="neutral"
                if not applied: fallback="model_has_no_compatible_emotion_capability"
            self.last_emotion_target=target; self.last_emotion_update=now
            return self._record_emotion_result(kind,name,applied,fallback,model_id)
        except Exception as error:
            self.last_error=type(error).__name__; self.state=VTSConnectionState.RECONNECTING; await self.client.close()
            return self._record_emotion_result("offline",None,False,"vts_request_failed",model_id)

    async def _deactivate_last_emotion(self) -> None:
        previous=self.last_emotion_target or {}
        if previous.get("kind")=="expression" and previous.get("file"):
            await set_expression(self.client,str(previous["file"]),False)
        elif previous.get("kind")=="hotkey" and previous.get("toggle") and previous.get("id"):
            await trigger_hotkey(self.client,str(previous["id"]))
        elif previous.get("kind")=="parameter":
            await inject_emotion(self.client,"neutral",0.0,self.mapping)

    def _resolve_emotion_target(self, emotion: str) -> dict[str,Any]:
        configured=self.config.emotion_map.get(emotion)
        if configured and configured.kind=="neutral": return {"kind":"neutral","target":None}
        kind=configured.kind if configured else "auto"; requested=configured.target if configured else None
        hotkey=self._find_hotkey(requested) if kind in {"auto","hotkey"} and requested else None
        expression=self._find_expression(requested) if kind in {"auto","expression"} and requested else None
        if hotkey: return hotkey
        if expression: return expression
        if kind=="parameter" and self._has_emotion_parameter(emotion): return {"kind":"parameter","target":emotion}
        if kind=="auto":
            canonical=f"NYRA_{emotion.upper()}"
            hotkey=self._find_hotkey(canonical)
            if hotkey: return hotkey
            expression=self._find_expression(canonical)
            if expression: return expression
            if self._has_emotion_parameter(emotion): return {"kind":"parameter","target":emotion}
        return {"kind":"neutral","target":None}

    def _find_hotkey(self, value: str|None) -> dict[str,Any]|None:
        wanted=self._normalized_name(value)
        if not wanted: return None
        for hotkey in self.hotkeys:
            identifier=str(hotkey.get("hotkeyID") or hotkey.get("hotkeyId") or "")
            name=str(hotkey.get("name") or "")
            if wanted in {self._normalized_name(identifier),self._normalized_name(name)}:
                hotkey_type=str(hotkey.get("type") or "")
                if "expression" not in hotkey_type.casefold(): return None
                return {"kind":"hotkey","target":name or identifier,"id":identifier,"toggle":"toggle" in hotkey_type.casefold()}
        return None

    def _find_expression(self, value: str|None) -> dict[str,Any]|None:
        wanted=self._normalized_name(value)
        if not wanted: return None
        for expression in self.expressions:
            file_name=str(expression.get("file") or expression.get("expressionFile") or "")
            if not file_name:
                continue
            name=str(expression.get("name") or Path(file_name).stem)
            if wanted in {self._normalized_name(file_name),self._normalized_name(Path(file_name).stem),self._normalized_name(name)}:
                return {"kind":"expression","target":name or file_name,"file":file_name}
        return None

    def _has_emotion_parameter(self, emotion: str) -> bool:
        key="concern" if emotion in {"concerned","empathetic","apologetic","uncertain"} else "amused" if emotion=="amused" else None
        return bool(key and self.mapping.get(key))

    @staticmethod
    def _normalized_name(value: str|None) -> str:
        return "".join(character for character in str(value or "").casefold() if character.isalnum())

    def _record_emotion_result(self, kind: str, target: str|None, applied: bool, fallback: str|None, model_id: str|None) -> dict[str,Any]:
        self.last_emotion_presentation={"emotion":self.current_emotion,"intensity":round(self.current_emotion_intensity,3),
            "kind":kind,"target":target,"applied":applied,"fallback":fallback,"model_id":model_id,
            "transition":self.current_transition}
        return dict(self.last_emotion_presentation)

    async def apply_cursor(self, state: AvatarState, x: float, y: float) -> bool:
        started=time.perf_counter(); now=time.monotonic()
        self.mouse_tracking.configure(self.config.mouse_tracking)
        speaking=str(state.animation or state.neural_link).casefold()=="speaking"
        frame=self.mouse_tracking.update(x,y,speaking=speaking)
        if now-self._tracking_last_request < (1/30)*.9 and not frame.reset_head and not frame.reset_all:
            return False
        self._tracking_last_request=now
        result={"input_x":round(float(x),4),"input_y":round(float(y),4),"mode":frame.mode.value,
                "eye_x":round(frame.eye_x,4),"eye_y":round(frame.eye_y,4),
                "head_x":round(frame.head_x,4),"head_y":round(frame.head_y,4),"speaking":speaking}
        if not self.config.enabled or self.state not in {VTSConnectionState.READY,VTSConnectionState.MODEL_LOADED}:
            self.last_cursor={**result,"applied":False,"reason":"VTS_NOT_READY"}
            return False
        values=mouse_parameter_values(frame,self.mapping)
        try:
            response=await inject_mouse_tracking(self.client,frame,self.mapping) if values else None
            applied=response is not None
        except Exception as error:
            self.last_error=type(error).__name__; self.state=VTSConnectionState.RECONNECTING
            await self.client.close()
            elapsed=(time.perf_counter()-started)*1000
            self._tracking_cost_ms.append(elapsed)
            self.last_cursor={**result,"applied":False,"parameter_count":0,"cost_ms":round(elapsed,4),"reason":"VTS_REQUEST_FAILED"}
            return False
        elapsed=(time.perf_counter()-started)*1000
        self._tracking_cost_ms.append(elapsed)
        if applied:
            self._tracking_injections+=1; self._tracking_timestamps.append(now)
        reason=None if applied else "MOUSE_TRACKING_OFF" if frame.mode.value=="OFF" else "MODEL_PARAMETERS_UNAVAILABLE"
        self.last_cursor={**result,"applied":applied,"parameter_count":len(values),
                          "cost_ms":round(elapsed,4),"reason":reason}
        return applied

    async def _refresh_model_metadata(self, force: bool=False) -> None:
        previous_id=self.model.get("modelID") or self.model.get("modelId")
        model=(await self.client.call("CurrentModelRequest")).get("data",{})
        model_id=model.get("modelID") or model.get("modelId")
        changed=force or model_id!=previous_id or bool(model.get("modelLoaded"))!=bool(self.model.get("modelLoaded"))
        self.model=model; self.last_model_check=time.monotonic()
        if changed and model.get("modelLoaded"):
            params=(await self.client.call("InputParameterListRequest")).get("data",{})
            available=discover_ids(params); self.parameters=sorted(available); self.mapping=resolve_mapping(available)
            self.mouse_tracking.reset_transport()
            self.hotkeys=await list_hotkeys(self.client)
            self.expressions=await list_expressions(self.client)
            self.last_avatar_mode=None; self.last_emotion_target=None
            self.state=VTSConnectionState.READY
            await self._synchronize_emotion(force=True)
            logger.info("vtube_studio_model_detected",extra={"model":model.get("modelName"),"model_id":model_id,"hotkeys":len(self.hotkeys)})
        elif not model.get("modelLoaded"):
            self.parameters=[]; self.mapping={}; self.hotkeys=[]; self.expressions=[]; self.state=VTSConnectionState.MODEL_MISSING

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
        allowed={"state","alpha","vts_active","sender","width","height","format","sender_fps","receiver_fps","frame_count","dropped_frames","last_frame_age_ms","adapter_match","sender_adapter","receiver_adapter","memory_bytes","error"}
        previous_state=self.presence.get("state"); previous_sender=self.presence.get("sender")
        self.presence={key:value for key,value in status.items() if key in allowed}; self.presence["reported_at"]=time.time()
        state=self.presence.get("state"); sender=self.presence.get("sender")
        if state!=previous_state or sender!=previous_sender:
            if state=="VTS_CONNECTING": logger.info("spout_sender_detected",extra={"sender":sender})
            elif state=="VTS_WAITING_FRAMES": logger.info("spout_receiver_connected",extra={"sender":sender})
            elif state=="VTS_ACTIVE":
                event="vts_presence_recovered" if self._presence_ever_active else "vts_presence_active"
                logger.info(event,extra={"sender":sender,"alpha":self.presence.get("alpha")})
                self._presence_ever_active=True
            elif state in {"VTS_DEGRADED","VTS_UNAVAILABLE","VTS_OFFLINE"}:
                logger.warning("vts_presence_unavailable",extra={"reason":self.presence.get("error") or "VTS_API_NOT_READY"})
        return self.presence

    def update(self,config:VTubeStudioConfig):
        was_enabled=self.config.enabled
        self.config=config; self.client.host=config.host; self.client.port=config.port; self.last_emotion_target=None
        if not config.enabled: self.state=VTSConnectionState.DISABLED
        elif not was_enabled and self.state==VTSConnectionState.DISABLED: self.state=VTSConnectionState.CONNECTING
        self.mouse_tracking.configure(config.mouse_tracking); self._save()
    def status(self):
        ready=self.state in {VTSConnectionState.READY,VTSConnectionState.MODEL_LOADED}
        timestamps=list(self._tracking_timestamps); costs=list(self._tracking_cost_ms)
        tracking_hz=(len(timestamps)-1)/(timestamps[-1]-timestamps[0]) if len(timestamps)>1 and timestamps[-1]>timestamps[0] else 0.0
        ordered=sorted(costs); p95=ordered[min(len(ordered)-1,max(0,int(len(ordered)*.95)))] if ordered else 0.0
        return {"provider":"vtube_studio","state":self.state,"connected":self.client.socket is not None,
                "authenticated":self.authenticated,"model_loaded":ready,"model":self.model.get("modelName"),
                "model_id":self.model.get("modelID") or self.model.get("modelId"),
                "parameter_count":sum(len(v) for v in self.mapping.values()),"mapping":self.mapping,"update_hz":self.update_hz,
                "parameters":self.parameters,"hotkeys":[{"id":item.get("hotkeyID") or item.get("hotkeyId"),"name":item.get("name"),"type":item.get("type")} for item in self.hotkeys],
                "expressions":[{"file":item.get("file") or item.get("expressionFile"),"name":item.get("name"),"active":bool(item.get("active"))} for item in self.expressions],
                "emotion_capabilities":{emotion.value:self._resolve_emotion_target(emotion.value) for emotion in NyraEmotion},
                "last_emotion_presentation":self.last_emotion_presentation,
                "last_hotkey":self.last_hotkey,"vts_presence":self.presence,
                "last_error":self.last_error,"requests_sent":getattr(self.client,"requests_sent",0),"last_request":getattr(self.client,"last_message_type",None),
                "last_cursor":self.last_cursor,
                "mouse_tracking":{"mode":self.config.mouse_tracking.value,"target_hz":30,"actual_hz":round(tracking_hz,2),
                                  "injections":self._tracking_injections,"average_cost_ms":round(sum(costs)/len(costs),4) if costs else 0.0,
                                  "p95_cost_ms":round(p95,4),"samples":len(costs),
                                  "eyes_available":bool(self.mapping.get("eye_x") or self.mapping.get("eye_y")),
                                  "head_available":bool(self.mapping.get("head_x") or self.mapping.get("head_y")),"body_enabled":False},
                "config":self.config.model_dump(mode="json"),"token_configured":bool(self.auth.load_token())}
    def readiness(self)->dict[str,Any]:
        return {**self.status(),"installed":self.installed(),"api_url":self.client.url,"automatic_install":False}
    @staticmethod
    def installed() -> bool:
        paths=[Path("C:/Program Files (x86)/Steam/steamapps/common/VTube Studio/VTube Studio.exe"),Path("C:/Program Files/Steam/steamapps/common/VTube Studio/VTube Studio.exe")]
        return any(p.is_file() for p in paths)
    def _load(self):
        try:
            raw=json.loads(CONFIG_PATH.read_text(encoding="utf-8")); config=VTubeStudioConfig.model_validate(raw)
            if raw!=config.model_dump(mode="json"): self._persist(config)
            return config
        except (OSError,ValueError):return VTubeStudioConfig()
    def _save(self):
        self._persist(self.config)
    @staticmethod
    def _persist(config: VTubeStudioConfig) -> None:
        CONFIG_PATH.parent.mkdir(parents=True,exist_ok=True); tmp=CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(config.model_dump_json(indent=2)+"\n",encoding="utf-8"); os.replace(tmp,CONFIG_PATH)
