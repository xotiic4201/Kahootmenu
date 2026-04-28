# File: main.py - Complete with embedded frontend
import asyncio
import json
import logging
import uuid
from typing import Dict, Set, Any
from datetime import datetime

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import websockets

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================
# Kahoot WebSocket Client (Fixed CometD Protocol)
# ============================================

class KahootGameClient:
    """Manages WebSocket connection to Kahoot for a specific game"""
    
    def __init__(self, game_id: str, nickname: str, ws_callback):
        self.game_id = game_id.upper()
        self.nickname = nickname
        self.callback = ws_callback
        self.websocket = None
        self.running = False
        self.client_id = None
        self.current_question = 0
        self.correct_answer = None
        self.connected = False
        
        self.answer_map = {
            0: "red",
            1: "blue",
            2: "yellow",
            3: "green"
        }
        
    async def connect_and_listen(self):
        """Main entry point - connect to Kahoot and start listening"""
        self.running = True
        retry_count = 0
        max_retries = 3
        
        while retry_count < max_retries and self.running:
            try:
                kahoot_ws_url = f"wss://kahoot.it/cometd/{self.game_id}/connect"
                logger.info(f"Connecting to Kahoot for game {self.game_id} (attempt {retry_count + 1})")
                
                async with websockets.connect(
                    kahoot_ws_url,
                    ping_interval=20,
                    ping_timeout=60,
                    close_timeout=10
                ) as ws:
                    self.websocket = ws
                    self.connected = True
                    logger.info(f"WebSocket connected to Kahoot for game {self.game_id}")
                    
                    if not await self._handshake():
                        logger.error("Handshake failed")
                        continue
                    
                    if not await self._subscribe():
                        logger.error("Subscription failed")
                        continue
                    
                    if not await self._join_game():
                        logger.error("Join game failed")
                        continue
                    
                    await self._listen_loop()
                    break
                    
            except Exception as e:
                logger.error(f"Kahoot connection error: {e}")
                retry_count += 1
                await asyncio.sleep(2)
            finally:
                self.connected = False
                
        if retry_count >= max_retries:
            await self.callback({
                "type": "error",
                "message": f"Failed to connect to Kahoot game {self.game_id}"
            })
        
        self.running = False
        logger.info(f"Kahoot client stopped for game {self.game_id}")
    
    async def _handshake(self) -> bool:
        """CometD handshake with Kahoot"""
        try:
            msg_id = str(int(datetime.now().timestamp() * 1000))
            handshake_msg = [{
                "channel": "/meta/handshake",
                "version": "1.0",
                "supportedConnectionTypes": ["websocket", "long-polling"],
                "id": msg_id
            }]
            await self.websocket.send(json.dumps(handshake_msg))
            
            response = await asyncio.wait_for(self.websocket.recv(), timeout=10)
            data = json.loads(response)
            
            if isinstance(data, list) and len(data) > 0:
                self.client_id = data[0].get("clientId")
                if self.client_id:
                    logger.info(f"Handshake complete, clientId: {self.client_id}")
                    return True
            return False
        except Exception as e:
            logger.error(f"Handshake error: {e}")
            return False
    
    async def _subscribe(self) -> bool:
        """Subscribe to game events channel"""
        try:
            subscribe_msg = [{
                "channel": "/meta/subscribe",
                "clientId": self.client_id,
                "subscription": f"/game/{self.game_id}",
                "id": str(int(datetime.now().timestamp() * 1000))
            }]
            await self.websocket.send(json.dumps(subscribe_msg))
            
            service_sub = [{
                "channel": "/meta/subscribe",
                "clientId": self.client_id,
                "subscription": "/service/controller",
                "id": str(int(datetime.now().timestamp() * 1000) + 1)
            }]
            await self.websocket.send(json.dumps(service_sub))
            
            return True
        except Exception as e:
            logger.error(f"Subscription error: {e}")
            return False
    
    async def _join_game(self) -> bool:
        """Send join command to Kahoot"""
        try:
            join_data = {
                "channel": "/service/controller",
                "clientId": self.client_id,
                "data": {
                    "gameid": self.game_id,
                    "host": "kahoot.it",
                    "name": self.nickname,
                    "type": "login",
                    "content": json.dumps({
                        "device": {
                            "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                            "screen": {"width": 1920, "height": 1080}
                        },
                        "gameid": self.game_id
                    })
                },
                "id": str(int(datetime.now().timestamp() * 1000))
            }
            await self.websocket.send(json.dumps([join_data]))
            logger.info(f"Join request sent for nickname: {self.nickname}")
            
            connect_msg = [{
                "channel": "/meta/connect",
                "clientId": self.client_id,
                "connectionType": "websocket",
                "id": str(int(datetime.now().timestamp() * 1000))
            }]
            await self.websocket.send(json.dumps(connect_msg))
            
            return True
        except Exception as e:
            logger.error(f"Join game error: {e}")
            return False
    
    async def _listen_loop(self):
        """Listen for Kahoot messages and extract answers"""
        last_ping = datetime.now()
        
        while self.running and self.connected:
            try:
                message = await asyncio.wait_for(self.websocket.recv(), timeout=25)
                data = json.loads(message)
                await self._process_message(data)
                last_ping = datetime.now()
                
            except asyncio.TimeoutError:
                if (datetime.now() - last_ping).seconds > 30:
                    heartbeat = [{
                        "channel": "/meta/connect",
                        "clientId": self.client_id,
                        "connectionType": "websocket",
                        "id": str(int(datetime.now().timestamp() * 1000))
                    }]
                    await self.websocket.send(json.dumps(heartbeat))
                    
            except websockets.exceptions.ConnectionClosed:
                logger.warning(f"WebSocket closed for game {self.game_id}")
                break
            except Exception as e:
                logger.error(f"Error in listen loop: {e}")
                await asyncio.sleep(1)
    
    async def _process_message(self, data: Any):
        """Process incoming Kahoot messages"""
        if not isinstance(data, list):
            return
            
        for msg in data:
            channel = msg.get("channel", "")
            game_data = msg.get("data", {})
            
            if channel == f"/game/{self.game_id}":
                await self._process_game_message(game_data)
    
    async def _process_game_message(self, game_data: dict):
        """Process game messages"""
        msg_type = game_data.get("type", "")
        
        if msg_type == "questionStart":
            self.current_question = game_data.get("questionIndex", 0) + 1
            question_obj = game_data.get("question", {})
            question_text = question_obj.get("questionText", "Loading...")
            answers = question_obj.get("answers", [])
            time_limit = game_data.get("timeLimit", 20)
            
            await self.callback({
                "type": "question_start",
                "question_number": self.current_question,
                "question_text": question_text,
                "answers": answers,
                "time_limit": time_limit
            })
            logger.info(f"Question {self.current_question} started")
        
        elif msg_type == "questionEnd":
            question = game_data.get("question", {})
            correct_indices = question.get("correctAnswers", [])
            
            if not correct_indices:
                choices = game_data.get("choices", [])
                for idx, choice in enumerate(choices):
                    if choice.get("correct"):
                        correct_indices = [idx]
                        break
            
            if correct_indices:
                correct_idx = correct_indices[0] if isinstance(correct_indices, list) else correct_indices
                self.correct_answer = self.answer_map.get(correct_idx, "unknown")
                
                await self.callback({
                    "type": "correct_answer",
                    "color": self.correct_answer,
                    "question_number": self.current_question
                })
                logger.info(f"Correct answer: {self.correct_answer}")
        
        elif msg_type == "answerResults":
            choices = game_data.get("choices", [])
            for idx, choice in enumerate(choices):
                if choice.get("correct"):
                    self.correct_answer = self.answer_map.get(idx, "unknown")
                    await self.callback({
                        "type": "correct_answer",
                        "color": self.correct_answer,
                        "question_number": self.current_question
                    })
                    break
        
        elif msg_type == "gameStateUpdate":
            players = game_data.get("players", [])
            for player in players:
                if player.get("name") == self.nickname:
                    await self.callback({
                        "type": "score",
                        "score": player.get("score", 0),
                        "rank": player.get("rank", 0)
                    })
                    break
    
    async def stop(self):
        """Stop the client"""
        self.running = False
        self.connected = False
        if self.websocket:
            try:
                await self.websocket.close()
            except:
                pass


# ============================================
# FastAPI Application
# ============================================

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, Set[WebSocket]] = {}
        self.kahoot_clients: Dict[str, KahootGameClient] = {}
        self.game_players: Dict[str, str] = {}
    
    async def connect(self, game_id: str, websocket: WebSocket):
        await websocket.accept()
        if game_id not in self.active_connections:
            self.active_connections[game_id] = set()
        self.active_connections[game_id].add(websocket)
        logger.info(f"Frontend connected to game {game_id}")
    
    def disconnect(self, game_id: str, websocket: WebSocket):
        if game_id in self.active_connections:
            self.active_connections[game_id].discard(websocket)
            if not self.active_connections[game_id]:
                del self.active_connections[game_id]
                asyncio.create_task(self.stop_kahoot_client(game_id))
    
    async def broadcast(self, game_id: str, message: dict):
        if game_id not in self.active_connections:
            return
        disconnected = set()
        for ws in self.active_connections[game_id]:
            try:
                await ws.send_json(message)
            except Exception:
                disconnected.add(ws)
        for ws in disconnected:
            self.disconnect(game_id, ws)
    
    async def start_kahoot_client(self, game_id: str, nickname: str):
        if game_id in self.kahoot_clients:
            await self.stop_kahoot_client(game_id)
        
        async def callback(msg):
            await self.broadcast(game_id, msg)
        
        client = KahootGameClient(game_id, nickname, callback)
        self.kahoot_clients[game_id] = client
        self.game_players[game_id] = nickname
        asyncio.create_task(client.connect_and_listen())
        logger.info(f"Started Kahoot client for game {game_id}")
    
    async def stop_kahoot_client(self, game_id: str):
        if game_id in self.kahoot_clients:
            await self.kahoot_clients[game_id].stop()
            del self.kahoot_clients[game_id]
        if game_id in self.game_players:
            del self.game_players[game_id]


manager = ConnectionManager()
app = FastAPI(title="Kahoot Answer Helper")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================
# Embedded HTML Frontend
# ============================================

HTML_CONTENT = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Kahoot Menu</title>
    <link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Mono:wght@400;500&family=Syne:wght@400;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --black:   #0d0d0d;
            --black2:  #141414;
            --black3:  #1c1c1c;
            --black4:  #242424;
            --red:     #b91c1c;
            --red-dim: #7f1d1d;
            --red-glow:#b91c1c55;
            --red-hot: #dc2626;
            --white:   #f5f5f5;
            --grey:    #6b6b6b;
            --grey-lt: #a3a3a3;
            --ans-red:    #991b1b;
            --ans-blue:   #1d4ed8;
            --ans-yellow: #a16207;
            --ans-green:  #166534;
        }

        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            font-family: 'Syne', sans-serif;
            background: var(--black);
            color: var(--white);
            min-height: 100vh;
            overflow-x: hidden;
        }

        /* ─── NOISE OVERLAY ─── */
        body::before {
            content: '';
            position: fixed;
            inset: 0;
            background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)' opacity='0.04'/%3E%3C/svg%3E");
            pointer-events: none;
            z-index: 9999;
            opacity: 0.35;
        }

        /* ─── RADIAL ACCENT ─── */
        .bg-accent {
            position: fixed;
            top: -200px;
            right: -200px;
            width: 600px;
            height: 600px;
            background: radial-gradient(circle, var(--red-glow) 0%, transparent 65%);
            pointer-events: none;
            animation: pulse-bg 6s ease-in-out infinite;
        }

        @keyframes pulse-bg {
            0%, 100% { opacity: 0.4; transform: scale(1); }
            50%       { opacity: 0.7; transform: scale(1.1); }
        }

        /* ─── LAYOUT ─── */
        .layout {
            display: grid;
            grid-template-columns: 1fr 300px;
            gap: 0;
            min-height: 100vh;
        }

        /* ─── SIDEBAR ─── */
        .sidebar {
            background: var(--black2);
            border-right: 1px solid #2a2a2a;
            display: flex;
            flex-direction: column;
            padding: 28px 24px;
            position: sticky;
            top: 0;
            height: 100vh;
            overflow-y: auto;
        }

        .logo {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 22px;
            letter-spacing: 3px;
            color: var(--red-hot);
            margin-bottom: 32px;
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .logo-dot {
            width: 8px; height: 8px;
            border-radius: 50%;
            background: var(--red-hot);
            animation: blink 1.4s ease-in-out infinite;
        }

        @keyframes blink {
            0%, 100% { opacity: 1; box-shadow: 0 0 6px var(--red-hot); }
            50%       { opacity: 0.2; box-shadow: none; }
        }

        .section-label {
            font-family: 'DM Mono', monospace;
            font-size: 9px;
            letter-spacing: 2.5px;
            color: var(--grey);
            text-transform: uppercase;
            margin-bottom: 10px;
        }

        .input-block {
            margin-bottom: 18px;
        }

        .inp {
            width: 100%;
            background: var(--black3);
            border: 1px solid #2a2a2a;
            border-radius: 6px;
            padding: 11px 14px;
            color: var(--white);
            font-family: 'DM Mono', monospace;
            font-size: 13px;
            outline: none;
            transition: border-color 0.2s, box-shadow 0.2s;
        }

        .inp:focus {
            border-color: var(--red);
            box-shadow: 0 0 0 2px var(--red-glow);
        }

        .inp::placeholder { color: var(--grey); }

        .btn {
            width: 100%;
            padding: 13px;
            border: none;
            border-radius: 6px;
            font-family: 'Bebas Neue', sans-serif;
            font-size: 16px;
            letter-spacing: 2px;
            cursor: pointer;
            transition: all 0.2s;
            position: relative;
            overflow: hidden;
        }

        .btn::after {
            content: '';
            position: absolute;
            inset: 0;
            background: white;
            opacity: 0;
            transition: opacity 0.15s;
        }

        .btn:active::after { opacity: 0.08; }

        .btn-primary {
            background: var(--red);
            color: var(--white);
        }

        .btn-primary:hover {
            background: var(--red-hot);
            box-shadow: 0 4px 20px var(--red-glow);
        }

        .btn-secondary {
            background: var(--black4);
            color: var(--grey-lt);
            border: 1px solid #2a2a2a;
            margin-top: 8px;
        }

        .btn-secondary:hover {
            border-color: var(--grey);
            color: var(--white);
        }

        .status-pill {
            margin-top: 14px;
            padding: 9px 14px;
            border-radius: 6px;
            font-family: 'DM Mono', monospace;
            font-size: 11px;
            display: flex;
            align-items: center;
            gap: 8px;
            transition: all 0.3s;
        }

        .status-pill.online  { background: #14290f; color: #4ade80; border: 1px solid #166534; }
        .status-pill.offline { background: #1a1010; color: var(--grey); border: 1px solid #2a2020; }

        .status-dot {
            width: 6px; height: 6px;
            border-radius: 50%;
            flex-shrink: 0;
        }

        .status-pill.online  .status-dot { background: #4ade80; box-shadow: 0 0 6px #4ade80; animation: blink 1s infinite; }
        .status-pill.offline .status-dot { background: var(--grey); }

        /* ─── STATS MINI ─── */
        .stat-row {
            display: flex;
            gap: 10px;
            margin-top: 28px;
        }

        .stat-mini {
            flex: 1;
            background: var(--black3);
            border: 1px solid #2a2a2a;
            border-radius: 8px;
            padding: 12px 10px;
            text-align: center;
        }

        .stat-mini-val {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 26px;
            color: var(--red-hot);
            line-height: 1;
        }

        .stat-mini-lbl {
            font-family: 'DM Mono', monospace;
            font-size: 9px;
            color: var(--grey);
            letter-spacing: 1.5px;
            margin-top: 4px;
        }

        /* ─── HISTORY ─── */
        .history-list {
            flex: 1;
            overflow-y: auto;
            margin-top: 20px;
        }

        .history-list::-webkit-scrollbar { width: 3px; }
        .history-list::-webkit-scrollbar-track { background: transparent; }
        .history-list::-webkit-scrollbar-thumb { background: var(--red-dim); border-radius: 2px; }

        .history-item {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 9px 12px;
            border-radius: 6px;
            margin-bottom: 6px;
            background: var(--black3);
            border: 1px solid #222;
            animation: slideIn 0.3s ease;
            font-family: 'DM Mono', monospace;
            font-size: 11px;
        }

        @keyframes slideIn {
            from { opacity: 0; transform: translateX(-10px); }
            to   { opacity: 1; transform: translateX(0); }
        }

        .hist-num {
            font-size: 9px;
            color: var(--grey);
            min-width: 14px;
        }

        .hist-dot {
            width: 10px; height: 10px;
            border-radius: 2px;
            flex-shrink: 0;
        }

        .hist-dot.red    { background: #dc2626; }
        .hist-dot.blue   { background: #2563eb; }
        .hist-dot.yellow { background: #d97706; }
        .hist-dot.green  { background: #16a34a; }

        .hist-label { color: var(--grey-lt); font-size: 11px; }
        .hist-pts   { margin-left: auto; color: var(--red-hot); font-size: 10px; }

        /* ─── MAIN PANEL ─── */
        .main {
            padding: 32px;
            display: flex;
            flex-direction: column;
            gap: 20px;
        }

        /* ─── TOP BAR ─── */
        .top-bar {
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        .page-title {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 36px;
            letter-spacing: 4px;
            color: var(--white);
        }

        .page-title span { color: var(--red-hot); }

        .badge {
            font-family: 'DM Mono', monospace;
            font-size: 10px;
            letter-spacing: 1.5px;
            padding: 5px 12px;
            border-radius: 100px;
            background: var(--black3);
            border: 1px solid #2a2a2a;
            color: var(--grey-lt);
        }

        /* ─── ANSWER HERO ─── */
        .answer-hero {
            background: var(--black2);
            border: 1px solid #2a2a2a;
            border-radius: 12px;
            padding: 40px 32px;
            text-align: center;
            position: relative;
            overflow: hidden;
            min-height: 200px;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
        }

        .answer-hero::before {
            content: '';
            position: absolute;
            inset: 0;
            background: linear-gradient(135deg, transparent 60%, #1a0a0a 100%);
        }

        .hero-eyebrow {
            font-family: 'DM Mono', monospace;
            font-size: 10px;
            letter-spacing: 3px;
            color: var(--grey);
            text-transform: uppercase;
            margin-bottom: 16px;
            position: relative;
        }

        .hero-answer {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 80px;
            letter-spacing: 6px;
            line-height: 1;
            position: relative;
            transition: all 0.4s cubic-bezier(0.34, 1.56, 0.64, 1);
        }

        .hero-answer.red    { color: #ef4444; text-shadow: 0 0 40px #ef444455; }
        .hero-answer.blue   { color: #60a5fa; text-shadow: 0 0 40px #60a5fa55; }
        .hero-answer.yellow { color: #fbbf24; text-shadow: 0 0 40px #fbbf2455; }
        .hero-answer.green  { color: #4ade80; text-shadow: 0 0 40px #4ade8055; }
        .hero-answer.waiting { color: #3a3a3a; font-size: 28px; letter-spacing: 4px; }

        .hero-answer.pop {
            animation: pop 0.5s cubic-bezier(0.34, 1.56, 0.64, 1);
        }

        @keyframes pop {
            0%   { transform: scale(0.5); opacity: 0; }
            70%  { transform: scale(1.08); }
            100% { transform: scale(1); opacity: 1; }
        }

        .hero-sub {
            font-family: 'DM Mono', monospace;
            font-size: 11px;
            color: var(--grey);
            margin-top: 14px;
            letter-spacing: 2px;
            position: relative;
        }

        /* ─── QUESTION CARD ─── */
        .q-card {
            background: var(--black2);
            border: 1px solid #2a2a2a;
            border-radius: 12px;
            padding: 24px 28px;
            position: relative;
            overflow: hidden;
        }

        .q-card::after {
            content: '';
            position: absolute;
            left: 0; top: 0; bottom: 0;
            width: 3px;
            background: var(--red);
        }

        .q-meta {
            display: flex;
            align-items: center;
            gap: 16px;
            margin-bottom: 12px;
        }

        .q-num-badge {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 13px;
            letter-spacing: 2px;
            background: var(--red-dim);
            color: #fca5a5;
            padding: 3px 12px;
            border-radius: 100px;
        }

        .q-timer {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 22px;
            color: var(--red-hot);
            margin-left: auto;
            transition: color 0.3s;
        }

        .q-timer.urgent { color: #ef4444; animation: flicker 0.5s infinite; }

        @keyframes flicker {
            0%, 100% { opacity: 1; }
            50%       { opacity: 0.5; }
        }

        .q-text {
            font-size: 17px;
            font-weight: 700;
            line-height: 1.45;
            color: var(--white);
        }

        /* ─── ANSWER GRID ─── */
        .answers-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
        }

        .ans-card {
            border-radius: 10px;
            padding: 20px 18px;
            display: flex;
            align-items: center;
            gap: 14px;
            font-family: 'Syne', sans-serif;
            font-weight: 700;
            font-size: 15px;
            cursor: default;
            border: 2px solid transparent;
            transition: transform 0.2s, box-shadow 0.3s, border-color 0.3s;
            position: relative;
            overflow: hidden;
        }

        .ans-card::before {
            content: '';
            position: absolute;
            inset: 0;
            background: white;
            opacity: 0;
            transition: opacity 0.2s;
        }

        .ans-card.red    { background: #2d0a0a; color: #fca5a5; }
        .ans-card.blue   { background: #0a1230; color: #93c5fd; }
        .ans-card.yellow { background: #1a1000; color: #fde68a; }
        .ans-card.green  { background: #061a0f; color: #86efac; }

        .ans-card.highlight.red    { border-color: #ef4444; box-shadow: 0 0 24px #ef444440; transform: scale(1.02); }
        .ans-card.highlight.blue   { border-color: #60a5fa; box-shadow: 0 0 24px #60a5fa40; transform: scale(1.02); }
        .ans-card.highlight.yellow { border-color: #fbbf24; box-shadow: 0 0 24px #fbbf2440; transform: scale(1.02); }
        .ans-card.highlight.green  { border-color: #4ade80; box-shadow: 0 0 24px #4ade8040; transform: scale(1.02); }

        .ans-shape {
            width: 28px; height: 28px;
            flex-shrink: 0;
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .ans-shape svg { width: 20px; height: 20px; }

        .ans-shape.red    svg { fill: #ef4444; }
        .ans-shape.blue   svg { fill: #60a5fa; }
        .ans-shape.yellow svg { fill: #fbbf24; }
        .ans-shape.green  svg { fill: #4ade80; }

        .ans-label { font-size: 12px; opacity: 0.5; margin-top: 2px; font-family: 'DM Mono', monospace; }
        .ans-name  { font-family: 'Bebas Neue', sans-serif; font-size: 18px; letter-spacing: 1.5px; }

        /* ─── SCORE STRIP ─── */
        .score-strip {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 12px;
        }

        .score-card {
            background: var(--black2);
            border: 1px solid #2a2a2a;
            border-radius: 10px;
            padding: 18px 16px;
            text-align: center;
            transition: border-color 0.3s;
        }

        .score-card:first-child { border-color: var(--red-dim); }

        .score-val {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 38px;
            color: var(--white);
            line-height: 1;
        }

        .score-card:first-child .score-val { color: var(--red-hot); }

        .score-lbl {
            font-family: 'DM Mono', monospace;
            font-size: 9px;
            letter-spacing: 2px;
            color: var(--grey);
            margin-top: 4px;
        }

        /* ─── ANSWER RATE BAR ─── */
        .rate-bar-wrap {
            background: var(--black2);
            border: 1px solid #2a2a2a;
            border-radius: 10px;
            padding: 18px 22px;
        }

        .rate-header {
            display: flex;
            justify-content: space-between;
            margin-bottom: 10px;
        }

        .rate-title {
            font-family: 'DM Mono', monospace;
            font-size: 10px;
            letter-spacing: 2px;
            color: var(--grey);
        }

        .rate-pct {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 16px;
            color: var(--red-hot);
        }

        .rate-bar {
            height: 6px;
            background: var(--black3);
            border-radius: 100px;
            overflow: hidden;
        }

        .rate-fill {
            height: 100%;
            background: linear-gradient(90deg, var(--red-dim), var(--red-hot));
            border-radius: 100px;
            transition: width 0.8s cubic-bezier(0.4, 0, 0.2, 1);
        }

        /* ─── TOOLTIP / INSTRUCTION ─── */
        .info-box {
            background: var(--black2);
            border: 1px solid #2a2a2a;
            border-radius: 10px;
            padding: 18px 22px;
        }

        .info-box h4 {
            font-family: 'DM Mono', monospace;
            font-size: 10px;
            letter-spacing: 2.5px;
            color: var(--red-hot);
            margin-bottom: 12px;
        }

        .info-step {
            display: flex;
            gap: 12px;
            margin-bottom: 8px;
            font-size: 12px;
            color: var(--grey-lt);
            line-height: 1.5;
        }

        .step-num {
            font-family: 'Bebas Neue', sans-serif;
            font-size: 14px;
            color: var(--red);
            min-width: 16px;
        }

        /* ─── ANIMATIONS ─── */
        .fade-in { animation: fadeIn 0.5s ease both; }
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(8px); }
            to   { opacity: 1; transform: translateY(0); }
        }

        /* ─── MOBILE ─── */
        @media (max-width: 768px) {
            .layout { grid-template-columns: 1fr; }
            .sidebar { position: relative; height: auto; }
            .main { padding: 20px 16px; }
            .hero-answer { font-size: 54px; }
            .score-strip { grid-template-columns: repeat(3, 1fr); }
        }
    </style>
</head>
<body>
<div class="bg-accent"></div>

<div class="layout">

    <!-- ─── SIDEBAR ─── -->
    <aside class="sidebar" style="order:2; grid-column:2;">
        <div class="logo">
            <div class="logo-dot"></div>
            KAHOOT<span style="color:var(--grey);">.ASSIST</span>
        </div>

        <div class="section-label">Game PIN</div>
        <div class="input-block">
            <input class="inp" id="gameId" type="text" placeholder="000000" maxlength="6" style="font-size:22px;letter-spacing:6px;text-align:center;">
        </div>

        <div class="section-label">Nickname</div>
        <div class="input-block">
            <input class="inp" id="nickname" type="text" placeholder="HelperBot" value="HelperBot">
        </div>

        <button class="btn btn-primary" id="connectBtn">CONNECT</button>
        <button class="btn btn-secondary" id="disconnectBtn">DISCONNECT</button>

        <div class="status-pill offline" id="statusDisplay">
            <div class="status-dot"></div>
            <span id="statusText">OFFLINE</span>
        </div>

        <div class="stat-row">
            <div class="stat-mini">
                <div class="stat-mini-val" id="statCorrect">0</div>
                <div class="stat-mini-lbl">CORRECT</div>
            </div>
            <div class="stat-mini">
                <div class="stat-mini-val" id="statStreak">0</div>
                <div class="stat-mini-lbl">STREAK</div>
            </div>
        </div>

        <div class="section-label" style="margin-top:24px;">ANSWER LOG</div>
        <div class="history-list" id="historyList">
            <div style="font-family:'DM Mono',monospace;font-size:11px;color:var(--grey);padding:10px 0;">No answers yet.</div>
        </div>
    </aside>

    <!-- ─── MAIN ─── -->
    <main class="main" style="order:1; grid-column:1;">
        <div class="top-bar fade-in">
            <div class="page-title">ANSWER<span>.</span>HELPER</div>
            <div class="badge" id="gameLabel">NO GAME</div>
        </div>

        <!-- Hero Answer -->
        <div class="answer-hero fade-in" id="answerHero" style="animation-delay:0.1s">
            <div class="hero-eyebrow">CORRECT ANSWER</div>
            <div class="hero-answer waiting" id="heroAnswer">WAITING</div>
            <div class="hero-sub" id="heroSub">Connect and join a game to begin</div>
        </div>

        <!-- Question Card -->
        <div class="q-card fade-in" style="animation-delay:0.2s">
            <div class="q-meta">
                <div class="q-num-badge" id="qNum">Q --</div>
                <div class="q-timer" id="qTimer">--s</div>
            </div>
            <div class="q-text" id="qText">Waiting for question…</div>
        </div>

        <!-- Answer Grid -->
        <div class="answers-grid fade-in" style="animation-delay:0.3s">
            <div class="ans-card red" data-color="red">
                <div class="ans-shape red">
                    <svg viewBox="0 0 20 20"><circle cx="10" cy="10" r="9"/></svg>
                </div>
                <div>
                    <div class="ans-name">RED</div>
                    <div class="ans-label">OPTION A</div>
                </div>
            </div>
            <div class="ans-card blue" data-color="blue">
                <div class="ans-shape blue">
                    <svg viewBox="0 0 20 20"><rect x="2" y="2" width="16" height="16" rx="2"/></svg>
                </div>
                <div>
                    <div class="ans-name">BLUE</div>
                    <div class="ans-label">OPTION B</div>
                </div>
            </div>
            <div class="ans-card yellow" data-color="yellow">
                <div class="ans-shape yellow">
                    <svg viewBox="0 0 20 20"><polygon points="10,2 18,18 2,18"/></svg>
                </div>
                <div>
                    <div class="ans-name">YELLOW</div>
                    <div class="ans-label">OPTION C</div>
                </div>
            </div>
            <div class="ans-card green" data-color="green">
                <div class="ans-shape green">
                    <svg viewBox="0 0 20 20"><polygon points="10,2 18,10 10,18 2,10"/></svg>
                </div>
                <div>
                    <div class="ans-name">GREEN</div>
                    <div class="ans-label">OPTION D</div>
                </div>
            </div>
        </div>

        <!-- Score Strip -->
        <div class="score-strip fade-in" style="animation-delay:0.4s">
            <div class="score-card">
                <div class="score-val" id="scoreVal">0</div>
                <div class="score-lbl">SCORE</div>
            </div>
            <div class="score-card">
                <div class="score-val" id="rankVal">—</div>
                <div class="score-lbl">RANK</div>
            </div>
            <div class="score-card">
                <div class="score-val" id="qCountVal">0</div>
                <div class="score-lbl">QUESTIONS</div>
            </div>
        </div>

        <!-- Accuracy Bar -->
        <div class="rate-bar-wrap fade-in" style="animation-delay:0.45s">
            <div class="rate-header">
                <div class="rate-title">ANSWER ACCURACY</div>
                <div class="rate-pct" id="accuracyPct">0%</div>
            </div>
            <div class="rate-bar">
                <div class="rate-fill" id="accuracyFill" style="width:0%"></div>
            </div>
        </div>

        <!-- Instructions -->
        <div class="info-box fade-in" style="animation-delay:0.5s">
            <h4>HOW TO USE</h4>
            <div class="info-step"><div class="step-num">1</div><div>Enter the 6-digit PIN from the host's screen</div></div>
            <div class="info-step"><div class="step-num">2</div><div>Set your nickname — must match your kahoot.it name exactly</div></div>
            <div class="info-step"><div class="step-num">3</div><div>Click CONNECT and join on kahoot.it simultaneously</div></div>
            <div class="info-step"><div class="step-num">4</div><div>Correct answer appears here the moment the question starts</div></div>
        </div>
    </main>
</div>

<script>
    const WS_URL = "wss://https://kahootmenu.onrender.com/ws/";

    /* ── State ── */
    let ws = null;
    let totalQ = 0, correctCount = 0, streak = 0;
    let timerInterval = null;
    let currentQNum = 0;
    let historyData = [];

    /* ── DOM refs ── */
    const gameIdInp   = document.getElementById('gameId');
    const nickInp     = document.getElementById('nickname');
    const connectBtn  = document.getElementById('connectBtn');
    const discBtn     = document.getElementById('disconnectBtn');
    const statusEl    = document.getElementById('statusDisplay');
    const statusTxt   = document.getElementById('statusText');
    const gameLbl     = document.getElementById('gameLabel');
    const heroAns     = document.getElementById('heroAnswer');
    const heroSub     = document.getElementById('heroSub');
    const qNumEl      = document.getElementById('qNum');
    const qTimerEl    = document.getElementById('qTimer');
    const qTextEl     = document.getElementById('qText');
    const scoreEl     = document.getElementById('scoreVal');
    const rankEl      = document.getElementById('rankVal');
    const qCountEl    = document.getElementById('qCountVal');
    const accuracyPct = document.getElementById('accuracyPct');
    const accuracyFil = document.getElementById('accuracyFill');
    const statCorrect = document.getElementById('statCorrect');
    const statStreak  = document.getElementById('statStreak');
    const histList    = document.getElementById('historyList');
    const ansCards    = document.querySelectorAll('.ans-card');

    /* ── Status ── */
    function setStatus(online, msg) {
        statusEl.className = 'status-pill ' + (online ? 'online' : 'offline');
        statusTxt.textContent = msg;
    }

    /* ── Clear highlights ── */
    function clearHighlights() {
        ansCards.forEach(c => c.classList.remove('highlight'));
    }

    /* ── Set hero to waiting ── */
    function setWaiting(msg = 'WAITING') {
        heroAns.className = 'hero-answer waiting';
        heroAns.textContent = msg;
        heroSub.textContent = 'Next answer incoming…';
        clearHighlights();
    }

    /* ── Show correct answer ── */
    function showAnswer(color) {
        clearHighlights();
        const names = { red:'RED', blue:'BLUE', yellow:'YELLOW', green:'GREEN' };
        heroAns.className = 'hero-answer ' + color + ' pop';
        heroAns.textContent = names[color] || color.toUpperCase();
        heroSub.textContent = '← SELECT THIS ANSWER';
        const card = document.querySelector(`.ans-card[data-color="${color}"]`);
        if (card) card.classList.add('highlight');
        localStorage.setItem('kahoot_correct_answer', color);
        localStorage.setItem('kahoot_correct_answer_timestamp', Date.now().toString());

        // Add to history
        correctCount++;
        streak++;
        updateStats();
        addHistoryItem(currentQNum, color, null);
    }

    /* ── Update accuracy ── */
    function updateStats() {
        statCorrect.textContent = correctCount;
        statStreak.textContent  = streak;
        const pct = totalQ > 0 ? Math.round((correctCount / totalQ) * 100) : 0;
        accuracyPct.textContent = pct + '%';
        accuracyFil.style.width = pct + '%';
    }

    /* ── Add history row ── */
    function addHistoryItem(qNum, color, pts) {
        const names = { red:'Red', blue:'Blue', yellow:'Yellow', green:'Green' };
        const el = document.createElement('div');
        el.className = 'history-item';
        el.innerHTML = `
            <span class="hist-num">${qNum}</span>
            <span class="hist-dot ${color}"></span>
            <span class="hist-label">${names[color]}</span>
            ${pts != null ? `<span class="hist-pts">+${pts}</span>` : ''}
        `;
        if (histList.querySelector('div[style]')) histList.innerHTML = '';
        histList.insertBefore(el, histList.firstChild);
    }

    /* ── Start countdown ── */
    function startTimer(seconds, qNum) {
        if (timerInterval) clearInterval(timerInterval);
        let t = seconds;
        qTimerEl.textContent = t + 's';
        qTimerEl.className = 'q-timer';
        timerInterval = setInterval(() => {
            t--;
            if (currentQNum !== qNum) { clearInterval(timerInterval); return; }
            qTimerEl.textContent = t + 's';
            if (t <= 5) qTimerEl.className = 'q-timer urgent';
            if (t <= 0) clearInterval(timerInterval);
        }, 1000);
    }

    /* ── Handle message ── */
    function onMessage(data) {
        switch (data.type) {
            case 'question_start':
                currentQNum = data.question_number;
                totalQ++;
                qNumEl.textContent  = `Q ${currentQNum}`;
                qTextEl.textContent = data.question_text || 'Loading…';
                qCountEl.textContent = totalQ;
                if (data.time_limit) startTimer(data.time_limit, currentQNum);
                setWaiting('?');
                break;
            case 'correct_answer':
                showAnswer(data.color);
                break;
            case 'score':
                scoreEl.textContent = data.score?.toLocaleString() ?? 0;
                rankEl.textContent  = data.rank ?? '—';
                break;
            case 'error':
                setStatus(false, 'ERR: ' + data.message);
                break;
        }
    }

    /* ── Connect ── */
    function connect() {
        const pin  = gameIdInp.value.trim().toUpperCase();
        const nick = nickInp.value.trim();
        if (pin.length < 4) { alert('Enter a valid PIN'); return; }
        if (!nick)           { alert('Enter a nickname'); return; }
        if (ws) ws.close();

        gameLbl.textContent = 'PIN: ' + pin;
        setStatus(false, 'CONNECTING…');
        ws = new WebSocket(`${WS_URL}${pin}?nickname=${encodeURIComponent(nick)}`);

        ws.onopen = () => {
            setStatus(true, 'LIVE · ' + nick);
            localStorage.setItem('kahoot_game_id',  pin);
            localStorage.setItem('kahoot_nickname',  nick);
            localStorage.setItem('kahoot_ws_url',    WS_URL);
        };
        ws.onmessage = e => {
            try { onMessage(JSON.parse(e.data)); } catch {}
        };
        ws.onerror  = () => setStatus(false, 'CONNECTION ERROR');
        ws.onclose  = () => { setStatus(false, 'OFFLINE'); ws = null; };
    }

    /* ── Disconnect ── */
    function disconnect() {
        if (ws) { ws.close(); ws = null; }
        setStatus(false, 'OFFLINE');
        gameLbl.textContent = 'NO GAME';
        setWaiting('WAITING');
        heroSub.textContent = 'Connect and join a game to begin';
        qNumEl.textContent  = 'Q --';
        qTimerEl.textContent = '--s';
        qTimerEl.className  = 'q-timer';
        qTextEl.textContent = 'Waiting for question…';
        clearHighlights();
        if (timerInterval) clearInterval(timerInterval);
        localStorage.removeItem('kahoot_correct_answer');
    }

    connectBtn.addEventListener('click', connect);
    discBtn.addEventListener('click', disconnect);

    /* ── Restore session ── */
    const storedPin  = localStorage.getItem('kahoot_game_id');
    const storedNick = localStorage.getItem('kahoot_nickname');
    if (storedPin && storedNick) {
        gameIdInp.value = storedPin;
        nickInp.value   = storedNick;
        setTimeout(connect, 500);
    }
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    return HTMLResponse(content=HTML_CONTENT)

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "active_games": len(manager.kahoot_clients),
        "active_connections": sum(len(conns) for conns in manager.active_connections.values())
    }

@app.websocket("/ws/{game_id}")
async def websocket_endpoint(websocket: WebSocket, game_id: str):
    game_id = game_id.upper()
    query_params = websocket.query_params
    nickname = query_params.get("nickname", "Helper")
    
    await manager.connect(game_id, websocket)
    
    if game_id not in manager.kahoot_clients:
        await manager.start_kahoot_client(game_id, nickname)
    
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(game_id, websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        manager.disconnect(game_id, websocket)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
