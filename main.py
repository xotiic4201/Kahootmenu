# File: main.py - COMPLETE FIXED VERSION
import asyncio
import json
import logging
import uuid
import random
from typing import Dict, Set, Optional, Any
from datetime import datetime
import re

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
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
        self.question_start_time = None
        self.connected = False
        
        # Map Kahoot answer index to color
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
                # Connect to Kahoot's CometD endpoint
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
                    
                    # Perform CometD handshake
                    if not await self._handshake():
                        logger.error("Handshake failed")
                        continue
                    
                    # Subscribe to game channels
                    if not await self._subscribe():
                        logger.error("Subscription failed")
                        continue
                    
                    # Send join command with nickname
                    if not await self._join_game():
                        logger.error("Join game failed")
                        continue
                    
                    # Start listening for messages
                    await self._listen_loop()
                    break
                    
            except websockets.exceptions.InvalidStatusCode as e:
                logger.error(f"Invalid status code: {e}")
                retry_count += 1
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"Kahoot connection error for game {self.game_id}: {e}")
                retry_count += 1
                await asyncio.sleep(2)
            finally:
                self.connected = False
                
        if retry_count >= max_retries:
            await self.callback({
                "type": "error",
                "message": f"Failed to connect to Kahoot game {self.game_id}. Make sure the game PIN is correct and the game is active."
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
                    
            logger.error(f"Handshake failed: {data}")
            return False
            
        except asyncio.TimeoutError:
            logger.error("Handshake timeout")
            return False
        except Exception as e:
            logger.error(f"Handshake error: {e}")
            return False
    
    async def _subscribe(self) -> bool:
        """Subscribe to game events channel"""
        try:
            # Subscribe to game channel
            subscribe_msg = [{
                "channel": "/meta/subscribe",
                "clientId": self.client_id,
                "subscription": f"/game/{self.game_id}",
                "id": str(int(datetime.now().timestamp() * 1000))
            }]
            await self.websocket.send(json.dumps(subscribe_msg))
            
            response = await asyncio.wait_for(self.websocket.recv(), timeout=10)
            logger.info(f"Subscribed to /game/{self.game_id}")
            
            # Also subscribe to service channel
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
            
            # Send connect message to keep alive
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
                # Send heartbeat to keep connection alive
                if (datetime.now() - last_ping).seconds > 30:
                    heartbeat = [{
                        "channel": "/meta/connect",
                        "clientId": self.client_id,
                        "connectionType": "websocket",
                        "id": str(int(datetime.now().timestamp() * 1000))
                    }]
                    await self.websocket.send(json.dumps(heartbeat))
                    logger.debug("Heartbeat sent")
                    
            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"WebSocket closed for game {self.game_id}: {e}")
                break
                
            except json.JSONDecodeError as e:
                logger.error(f"JSON decode error: {e}")
                
            except Exception as e:
                logger.error(f"Error in listen loop: {e}")
                await asyncio.sleep(1)
    
    async def _process_message(self, data: Any):
        """Process incoming Kahoot messages and extract question/answer data"""
        if not isinstance(data, list):
            return
            
        for msg in data:
            channel = msg.get("channel", "")
            game_data = msg.get("data", {})
            
            # Handle game channel messages
            if channel == f"/game/{self.game_id}":
                await self._process_game_message(game_data)
            
            # Handle service channel messages
            elif channel == "/service/controller":
                await self._process_service_message(game_data)
    
    async def _process_game_message(self, game_data: dict):
        """Process messages from game channel"""
        msg_type = game_data.get("type", "")
        
        # Question started
        if msg_type == "questionStart":
            self.current_question = game_data.get("questionIndex", 0) + 1
            self.question_start_time = datetime.now()
            
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
            logger.info(f"Question {self.current_question} started: {question_text[:50]}...")
        
        # Question ended - extract correct answer
        elif msg_type == "questionEnd":
            question = game_data.get("question", {})
            correct_indices = question.get("correctAnswers", [])
            
            # Alternative: look in choices
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
                logger.info(f"Question {self.current_question} correct answer: {self.correct_answer}")
        
        # Answer results
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
        
        # Game state update (scores)
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
        
        # Player count update
        elif msg_type == "playerCount":
            count = game_data.get("count", 0)
            logger.info(f"Players in game: {count}")
    
    async def _process_service_message(self, game_data: dict):
        """Process messages from service channel"""
        msg_type = game_data.get("type", "")
        
        # Join response
        if msg_type == "joined":
            logger.info(f"Successfully joined game {self.game_id} as {self.nickname}")
            await self.callback({
                "type": "joined",
                "message": f"Joined game {self.game_id} as {self.nickname}"
            })
        
        # Error messages
        elif msg_type == "error":
            error_msg = game_data.get("error", "Unknown error")
            logger.error(f"Game error: {error_msg}")
            await self.callback({
                "type": "error",
                "message": error_msg
            })
    
    async def stop(self):
        """Stop the client"""
        self.running = False
        self.connected = False
        if self.websocket:
            try:
                await self.websocket.close()
            except:
                pass
        logger.info(f"Kahoot client stopped for game {self.game_id}")


# ============================================
# FastAPI Application
# ============================================

class ConnectionManager:
    """Manages frontend WebSocket connections and their associated Kahoot clients"""
    
    def __init__(self):
        self.active_connections: Dict[str, Set[WebSocket]] = {}
        self.kahoot_clients: Dict[str, KahootGameClient] = {}
        self.game_players: Dict[str, str] = {}
        self.client_tasks: Dict[str, asyncio.Task] = {}
    
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
        """Send message to all frontend clients connected to this game"""
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
        """Start a Kahoot client for this game"""
        if game_id in self.kahoot_clients:
            await self.stop_kahoot_client(game_id)
        
        # Create callback that broadcasts to frontend
        async def callback(msg):
            await self.broadcast(game_id, msg)
        
        client = KahootGameClient(game_id, nickname, callback)
        self.kahoot_clients[game_id] = client
        self.game_players[game_id] = nickname
        
        # Start the client in background
        task = asyncio.create_task(client.connect_and_listen())
        self.client_tasks[game_id] = task
        
        # Add callback for when task completes
        task.add_done_callback(lambda _: asyncio.create_task(self.stop_kahoot_client(game_id)))
        
        logger.info(f"Started Kahoot client for game {game_id}")
    
    async def stop_kahoot_client(self, game_id: str):
        """Stop and remove Kahoot client for this game"""
        if game_id in self.kahoot_clients:
            await self.kahoot_clients[game_id].stop()
            del self.kahoot_clients[game_id]
        
        if game_id in self.client_tasks:
            if not self.client_tasks[game_id].done():
                self.client_tasks[game_id].cancel()
            del self.client_tasks[game_id]
        
        if game_id in self.game_players:
            del self.game_players[game_id]
        
        logger.info(f"Stopped Kahoot client for game {game_id}")


manager = ConnectionManager()

# Create FastAPI app
app = FastAPI(title="Kahoot Answer Helper")

# Enable CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def root():
    return {
        "message": "Kahoot Answer Helper Backend",
        "status": "running",
        "version": "2.0",
        "websocket_endpoint": "/ws/{game_id}"
    }

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "active_games": len(manager.kahoot_clients),
        "active_connections": sum(len(conns) for conns in manager.active_connections.values())
    }

@app.websocket("/ws/{game_id}")
async def websocket_endpoint(websocket: WebSocket, game_id: str):
    """WebSocket endpoint for frontend clients"""
    game_id = game_id.upper()
    query_params = websocket.query_params
    nickname = query_params.get("nickname", "Helper")
    
    # Accept connection
    await manager.connect(game_id, websocket)
    
    # Start Kahoot client if not already running
    if game_id not in manager.kahoot_clients:
        await manager.start_kahoot_client(game_id, nickname)
    
    try:
        # Keep connection alive and handle incoming messages
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
            elif data == "status":
                await websocket.send_json({
                    "type": "status",
                    "connected": game_id in manager.kahoot_clients,
                    "game_id": game_id
                })
    except WebSocketDisconnect:
        manager.disconnect(game_id, websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        manager.disconnect(game_id, websocket)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
