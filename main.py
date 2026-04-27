# File: main.py
import asyncio
import json
import logging
import uuid
from typing import Dict, Set, Optional, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import httpx
import websockets

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================
# Kahoot WebSocket Client (CometD Protocol)
# ============================================

class KahootGameClient:
    """Manages WebSocket connection to Kahoot for a specific game"""
    
    def __init__(self, game_id: str, nickname: str, ws_callback):
        self.game_id = game_id.upper()
        self.nickname = nickname
        self.callback = ws_callback  # Async function to send updates to frontend
        self.websocket = None
        self.running = False
        self.task = None
        self.client_id = str(uuid.uuid4())
        self.channel = None
        self.current_question = 0
        self.correct_answer = None  # Store as color: "red", "blue", "yellow", "green"
        
        # Map Kahoot answer index to color
        self.answer_map = {
            0: "red",
            1: "blue",
            2: "yellow",
            3: "green"
        }
        # Reverse map for color to index
        self.color_to_index = {v: k for k, v in self.answer_map.items()}
        
    async def connect_and_listen(self):
        """Main entry point - connect to Kahoot and start listening"""
        self.running = True
        try:
            # Connect to Kahoot's CometD endpoint
            kahoot_ws_url = f"wss://kahoot.it/cometd/{self.game_id}/connect"
            async with websockets.connect(kahoot_ws_url) as ws:
                self.websocket = ws
                logger.info(f"Connected to Kahoot for game {self.game_id}")
                
                # Perform CometD handshake
                await self._handshake()
                
                # Subscribe to game channels
                await self._subscribe()
                
                # Send join command with nickname
                await self._join_game()
                
                # Start listening for messages
                await self._listen_loop()
                
        except Exception as e:
            logger.error(f"Kahoot connection error for game {self.game_id}: {e}")
            await self.callback({
                "type": "error",
                "message": f"Failed to connect: {str(e)}"
            })
        finally:
            self.running = False
            logger.info(f"Kahoot client stopped for game {self.game_id}")
    
    async def _handshake(self):
        """CometD handshake with Kahoot"""
        handshake_msg = [{
            "channel": "/meta/handshake",
            "version": "1.0",
            "supportedConnectionTypes": ["websocket"],
            "id": "1"
        }]
        await self.websocket.send(json.dumps(handshake_msg))
        response = await self.websocket.recv()
        data = json.loads(response)
        if isinstance(data, list) and len(data) > 0:
            self.client_id = data[0].get("clientId")
            logger.info(f"Handshake complete, clientId: {self.client_id}")
    
    async def _subscribe(self):
        """Subscribe to game events channel"""
        subscribe_msg = [{
            "channel": "/meta/subscribe",
            "clientId": self.client_id,
            "subscription": f"/game/{self.game_id}",
            "id": "2"
        }]
        await self.websocket.send(json.dumps(subscribe_msg))
        response = await self.websocket.recv()
        logger.info(f"Subscribed to /game/{self.game_id}")
    
    async def _join_game(self):
        """Send join command to Kahoot"""
        join_data = {
            "id": 3,
            "clientId": self.client_id,
            "channel": "/service/controller",
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
            }
        }
        await self.websocket.send(json.dumps([join_data]))
        logger.info(f"Join request sent for nickname: {self.nickname}")
    
    async def _listen_loop(self):
        """Listen for Kahoot messages and extract answers"""
        while self.running:
            try:
                message = await asyncio.wait_for(self.websocket.recv(), timeout=30)
                data = json.loads(message)
                await self._process_message(data)
            except asyncio.TimeoutError:
                # Send heartbeat to keep connection alive
                heartbeat = [{
                    "channel": "/meta/connect",
                    "clientId": self.client_id,
                    "connectionType": "websocket",
                    "id": "4"
                }]
                await self.websocket.send(json.dumps(heartbeat))
            except websockets.exceptions.ConnectionClosed:
                logger.warning(f"WebSocket closed for game {self.game_id}")
                break
            except Exception as e:
                logger.error(f"Error in listen loop: {e}")
                break
    
    async def _process_message(self, data: Any):
        """Process incoming Kahoot messages and extract question/answer data"""
        if not isinstance(data, list):
            return
            
        for msg in data:
            if msg.get("channel") != "/game/" + self.game_id:
                continue
                
            game_data = msg.get("data", {})
            
            # Check for question start
            if game_data.get("type") == "questionStart":
                self.current_question = game_data.get("questionIndex", 0) + 1
                await self.callback({
                    "type": "question_start",
                    "question_number": self.current_question,
                    "question_text": game_data.get("question", {}).get("questionText", ""),
                    "answers": game_data.get("question", {}).get("answers", []),
                    "time_limit": game_data.get("timeLimit", 20)
                })
                logger.info(f"Question {self.current_question} started")
                
            # Check for correct answer extraction from question data
            elif game_data.get("type") == "questionEnd":
                # Sometimes correct answer is in questionEnd
                question = game_data.get("question", {})
                correct_indices = question.get("correctAnswers", [])
                if correct_indices and len(correct_indices) > 0:
                    correct_idx = correct_indices[0]
                    self.correct_answer = self.answer_map.get(correct_idx, "unknown")
                    await self.callback({
                        "type": "correct_answer",
                        "color": self.correct_answer,
                        "question_number": self.current_question
                    })
                    logger.info(f"Correct answer: {self.correct_answer}")
            
            # Check for answer results (some versions send correct answer here)
            elif game_data.get("type") == "answerResults":
                # Extract correct answer from choices
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
            
            # Check for game state updates (score, rank)
            elif game_data.get("type") == "gameStateUpdate":
                players = game_data.get("players", [])
                for player in players:
                    if player.get("name") == self.nickname:
                        await self.callback({
                            "type": "score",
                            "score": player.get("score", 0),
                            "rank": player.get("rank", 0)
                        })
                        break
            
            # Alternative: extract from "question" message
            elif game_data.get("type") == "question":
                # Some question messages contain the correct answer directly
                correct_answer_idx = game_data.get("correctAnswer")
                if correct_answer_idx is not None:
                    self.correct_answer = self.answer_map.get(correct_answer_idx, "unknown")
                    await self.callback({
                        "type": "correct_answer",
                        "color": self.correct_answer
                    })
    
    async def stop(self):
        """Stop the client"""
        self.running = False
        if self.websocket:
            await self.websocket.close()


# ============================================
# FastAPI Application
# ============================================

class ConnectionManager:
    """Manages frontend WebSocket connections and their associated Kahoot clients"""
    
    def __init__(self):
        self.active_connections: Dict[str, Set[WebSocket]] = {}  # game_id -> set of frontend ws
        self.kahoot_clients: Dict[str, KahootGameClient] = {}    # game_id -> Kahoot client
        self.game_players: Dict[str, str] = {}                   # game_id -> nickname
    
    async def connect(self, game_id: str, websocket: WebSocket):
        await websocket.accept()
        if game_id not in self.active_connections:
            self.active_connections[game_id] = set()
        self.active_connections[game_id].add(websocket)
        logger.info(f"Frontend connected to game {game_id}, total: {len(self.active_connections[game_id])}")
    
    def disconnect(self, game_id: str, websocket: WebSocket):
        if game_id in self.active_connections:
            self.active_connections[game_id].discard(websocket)
            if not self.active_connections[game_id]:
                del self.active_connections[game_id]
                # Also stop Kahoot client if no frontends are connected
                self.stop_kahoot_client(game_id)
        logger.info(f"Frontend disconnected from game {game_id}")
    
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
        asyncio.create_task(client.connect_and_listen())
        logger.info(f"Started Kahoot client for game {game_id} with nickname {nickname}")
    
    async def stop_kahoot_client(self, game_id: str):
        """Stop and remove Kahoot client for this game"""
        if game_id in self.kahoot_clients:
            await self.kahoot_clients[game_id].stop()
            del self.kahoot_clients[game_id]
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
    return {"message": "Kahoot Answer Helper Backend", "status": "running"}

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
            # Optional: handle pings or commands from frontend
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(game_id, websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        manager.disconnect(game_id, websocket)

@app.get("/health")
async def health():
    return {"status": "healthy"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
