"""Lector del chat de Kick en tiempo real (Pusher sobre websocket, sin cuenta).

Por cada mensaje:
  - se cuenta en el bucket de N segundos (actividad del chat);
  - se marca si es 'hype' (risas, sorpresa, "clip"...);
  - se buscan menciones a OTROS streamers configurados (p. ej. "gear" en el
    chat de Westcol), que luego se convierten en señales de chipeo;
  - opcionalmente se guarda el texto (sirve para darle a Claude el 'sentir' del chat).

La escritura a SQLite se hace por lotes cada 2 s desde un hilo aparte.
"""

from __future__ import annotations

import collections
import json
import logging
import threading
import time

import websocket  # websocket-client

from .db import Database
from .kick import KickClient
from .mentions import HypeDetector, MentionMatcher

log = logging.getLogger(__name__)

CHAT_EVENTS = {"App\\Events\\ChatMessageEvent", "App\\Events\\ChatMessageSentEvent"}
PUSHER_URL = "wss://ws-{cluster}.pusher.com/app/{key}?protocol=7&client=js&version=8.4.0&flash=false"


class _Bucket:
    __slots__ = ("n", "users", "hype", "mentions")

    def __init__(self):
        self.n = 0
        self.users: set[str] = set()
        self.hype = 0
        self.mentions: collections.Counter = collections.Counter()


class ChatListener(threading.Thread):
    def __init__(self, cfg: dict, db: Database, session: dict, streamer: dict,
                 kick: KickClient, matcher: MentionMatcher):
        super().__init__(name=f"chat-{streamer['slug']}", daemon=True)
        self.cfg = cfg
        self.db = db
        self.session = session
        self.streamer = streamer
        self.slug = streamer["slug"]
        self.kick = kick
        self.matcher = matcher
        self.hype = HypeDetector(cfg["chat"]["palabras_hype"])
        self.bucket_s = float(cfg["deteccion"]["bucket_s"])
        self.save_text = bool(cfg["chat"]["guardar_mensajes"])
        self._stop_evt = threading.Event()
        self._ws: websocket.WebSocketApp | None = None
        self._lock = threading.Lock()
        self._buckets: dict[float, _Bucket] = {}
        self._raw: list[tuple] = []
        self._recent: collections.deque[float] = collections.deque()
        self._seen_ids: collections.deque[str] = collections.deque(maxlen=5000)
        self._seen_set: set[str] = set()
        self._keys = list(cfg["chat"]["pusher_keys"])
        self._key_idx = 0
        self._last_msg = time.time()
        self._total = 0
        self._chatroom_id: int | None = streamer.get("chatroom_id")
        self._state = "iniciando"
        self._last_mentions: collections.deque[dict] = collections.deque(maxlen=20)

    # -- estado -------------------------------------------------------------------
    @property
    def status(self) -> dict:
        with self._lock:
            now = time.time()
            while self._recent and now - self._recent[0] > 60:
                self._recent.popleft()
            return {
                "estado": self._state,
                "chatroom_id": self._chatroom_id,
                "msgs_min": len(self._recent),
                "total": self._total,
                "menciones_recientes": list(self._last_mentions)[-5:],
            }

    def stop(self) -> None:
        self._stop_evt.set()
        ws = self._ws
        if ws:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    # -- ciclo principal ------------------------------------------------------------
    def run(self) -> None:
        flusher = threading.Thread(target=self._flush_loop, name=f"chatflush-{self.slug}", daemon=True)
        flusher.start()
        while not self._stop_evt.is_set() and self._chatroom_id is None:
            try:
                self._chatroom_id = self.kick.get_channel(self.slug).chatroom_id
            except Exception as exc:  # noqa: BLE001
                self._state = "error API"
                log.warning("[%s] no obtuve el chatroom_id (%s); reintento en 60 s. "
                            "Puedes fijarlo a mano en config.yaml (chatroom_id).", self.slug, exc)
                self._stop_evt.wait(60)
        backoff = 2.0
        while not self._stop_evt.is_set():
            key = self._keys[self._key_idx % len(self._keys)]
            url = PUSHER_URL.format(cluster=self.cfg["chat"]["pusher_cluster"], key=key)
            self._ws = websocket.WebSocketApp(
                url, on_open=self._on_open, on_message=self._on_message,
                on_error=self._on_error, on_close=self._on_close,
            )
            started = time.time()
            self._state = "conectando"
            self._ws.run_forever(ping_interval=0, skip_utf8_validation=True)
            if self._stop_evt.is_set():
                break
            backoff = 2.0 if time.time() - started > 120 else min(backoff * 2, 120)
            self._state = f"reconectando en {backoff:.0f}s"
            self._stop_evt.wait(backoff)
        self._state = "detenido"
        self._flush(force=True)

    # -- Pusher ---------------------------------------------------------------------
    def _send(self, event: str, data: dict) -> None:
        try:
            if self._ws:
                self._ws.send(json.dumps({"event": event, "data": data}))
        except Exception:  # noqa: BLE001
            pass

    def _on_open(self, ws) -> None:
        self._last_msg = time.time()

    def _subscribe(self) -> None:
        cid = self._chatroom_id
        for channel in (f"chatrooms.{cid}.v2", f"chatrooms.{cid}"):
            self._send("pusher:subscribe", {"auth": "", "channel": channel})
        self._state = "conectado"
        log.info("[%s] chat conectado (chatroom %s)", self.slug, cid)

    def _on_message(self, ws, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        event = msg.get("event", "")
        if event == "pusher:connection_established":
            self._subscribe()
        elif event == "pusher:ping":
            self._send("pusher:pong", {})
        elif event == "pusher:error":
            data = msg.get("data") or {}
            code = data.get("code") if isinstance(data, dict) else None
            log.warning("[%s] Pusher error %s: %s", self.slug, code, data)
            if code and 4000 <= int(code) < 4100:  # app key inválida / cluster equivocado
                self._key_idx += 1
                ws.close()
        elif event in CHAT_EVENTS:
            try:
                data = json.loads(msg.get("data") or "{}")
            except ValueError:
                return
            self._handle_chat(data)

    def _on_error(self, ws, error) -> None:
        if not self._stop_evt.is_set():
            log.debug("[%s] websocket error: %s", self.slug, error)

    def _on_close(self, ws, code, reason) -> None:
        if not self._stop_evt.is_set():
            log.info("[%s] chat desconectado (%s %s)", self.slug, code, reason)

    # -- procesamiento de mensajes ----------------------------------------------------
    def _handle_chat(self, data: dict) -> None:
        mid = str(data.get("id") or "")
        if mid:
            if mid in self._seen_set:
                return  # llegó por los dos canales suscritos
            if len(self._seen_ids) == self._seen_ids.maxlen:
                self._seen_set.discard(self._seen_ids[0])
            self._seen_ids.append(mid)
            self._seen_set.add(mid)
        content = str(data.get("content") or "")
        user = str((data.get("sender") or {}).get("username") or "")
        ts = time.time()
        bucket_ts = ts - (ts % self.bucket_s)
        is_hype = self.hype.is_hype(content)
        mentions = self.matcher.find_others(content, self.slug)
        with self._lock:
            b = self._buckets.get(bucket_ts)
            if b is None:
                b = self._buckets[bucket_ts] = _Bucket()
            b.n += 1
            b.users.add(user)
            b.hype += int(is_hype)
            for target, n in mentions.items():
                b.mentions[target] += n
                self._last_mentions.append({"ts": ts, "target": target, "texto": content[:120]})
            if self.save_text:
                self._raw.append((self.session["id"], self.slug, ts, user[:40], content[:300]))
            self._recent.append(ts)
            self._total += 1
            self._last_msg = ts

    def _flush_loop(self) -> None:
        last_ping = time.time()
        while not self._stop_evt.wait(2):
            self._flush()
            now = time.time()
            if now - last_ping > 60:
                self._send("pusher:ping", {})
                last_ping = now
            # Conectado pero 15 min sin mensajes: probablemente la clave o el canal cambiaron.
            if self._state == "conectado" and now - self._last_msg > 900:
                log.warning("[%s] 15 min sin mensajes; reconecto probando otra clave de Pusher", self.slug)
                self._key_idx += 1
                self._last_msg = now
                if self._ws:
                    self._ws.close()

    def _flush(self, force: bool = False) -> None:
        now = time.time()
        current = now - (now % self.bucket_s)
        with self._lock:
            ready = [k for k in self._buckets if force or k < current]
            buckets = [(k, self._buckets.pop(k)) for k in ready]
            raw, self._raw = self._raw, []
        if buckets:
            sid = self.session["id"]
            self.db.add_chat_buckets([(sid, self.slug, k, b.n, len(b.users), b.hype) for k, b in buckets])
            ment = [(sid, self.slug, k, t, n) for k, b in buckets for t, n in b.mentions.items()]
            if ment:
                self.db.add_chat_mentions(ment)
        if raw:
            self.db.add_chat_messages(raw)
