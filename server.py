"""
GBK Group AI Voice Agent Server
Stack: Vobiz (telephony) + Sarvam (STT/TTS) + OpenAI (LLM)
Deploy on: Railway / Render / any VPS

This server handles:
1. /answer — Returns XML to Vobiz when call is answered (starts WebSocket stream)
2. /ws — WebSocket endpoint for bidirectional audio streaming
3. /hangup — Receives hangup data, sends post-call summary to Make.com
"""

import os
import json
import base64
import asyncio
import logging
import httpx
import struct
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response
from contextlib import asynccontextmanager

# ─── CONFIGURATION ──────────────────────────────────────────────
SARVAM_API_KEY = os.getenv("SARVAM_API_KEY", "your-sarvam-api-key")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "your-openai-api-key")
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://your-app.railway.app")
MAKE_WEBHOOK_URL = os.getenv("MAKE_WEBHOOK_URL", "https://hook.us1.make.com/your-hangup-webhook")
VOBIZ_AUTH_ID = os.getenv("VOBIZ_AUTH_ID", "SA_CCRV76JF")
VOBIZ_AUTH_TOKEN = os.getenv("VOBIZ_AUTH_TOKEN", "your-vobiz-auth-token")

# Audio settings
SAMPLE_RATE = 8000
CHUNK_DURATION_MS = 20
BYTES_PER_SAMPLE = 2  # 16-bit PCM
CHUNK_SIZE = int(SAMPLE_RATE * BYTES_PER_SAMPLE * CHUNK_DURATION_MS / 1000)  # 320 bytes

# Silence detection
SILENCE_THRESHOLD = 500  # amplitude threshold
SILENCE_DURATION_MS = 1200  # 1.2 seconds of silence = end of utterance
MAX_CALL_DURATION = 300  # 5 minutes max

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gbk-voice-agent")

# ─── GBK GROUP SYSTEM PROMPT ───────────────────────────────────

GBK_SYSTEM_PROMPT = """You are Priya, a warm and professional sales consultant at GBK Group — one of Mumbai's most trusted real estate developers with 35+ years of experience and 5,000+ happy families across Ambernath, Badlapur, Kalyan, and Ulhasnagar.

You are on a live phone call with a lead who recently enquired about a GBK Group property.

LANGUAGE: Speak in Hinglish — Hindi sentence structure with natural English words mixed in. Write your responses in Devanagari script. Match the lead's language preference.

YOUR GOAL (in this exact order):
1. Greet them warmly by name
2. Confirm their interest in the project
3. Qualify them — budget, BHK preference, timeline, purpose
4. Book a site visit with a specific day
5. End the call warmly

CRITICAL RULES:
- Keep EVERY response to 1-2 sentences MAX. This is a phone call.
- Ask ONE question at a time. Never stack questions.
- Sound natural and warm — like a real person.
- Never dump project information unless asked.
- If lead sounds busy, offer to call back.
- If not interested, thank politely and end.
- Never argue or pressure.

LEAD CONTEXT:
Lead Name: {lead_name}
Project Interest: {lead_project}

CONVERSATION FLOW:

OPENING (first message only):
"नमस्ते {lead_name} जी, मैं प्रिया बोल रही हूँ GBK Group की तरफ़ से। आपने हाल ही में {lead_project} में interest दिखाया था — क्या अभी एक-दो minute बात हो सकती है?"

QUALIFICATION (ask one at a time):
1. "आप 1 BHK देख रहे हैं या 2 BHK?"
2. "आपका budget approximately क्या है?"
3. "यह personal use के लिए है या investment के लिए?"
4. "आप अगले कुछ months में buy करने का plan कर रहे हैं या अभी explore कर रहे हैं?"

BOOK SITE VISIT:
"आपने जो बताया उसके हिसाब से, यह project आपके लिए बहुत अच्छा match है। क्या आप site visit करना चाहेंगे? इस Saturday या Sunday — कब comfortable रहेगा?"

OBJECTION HANDLING:
- "Price ज़्यादा है" → "मैं समझ सकती हूँ। GBK में flexible payment plans हैं और 99% home loan support मिलता है।"
- "Family से discuss करना है" → "बिल्कुल। क्या मैं आपको project brochure भेज दूँ?"
- "Possession कब मिलेगी?" → "Possession सिर्फ़ OC मिलने के बाद दी जाती है — पूरी तरह legal compliance।"

CLOSING:
"बहुत-बहुत धन्यवाद {lead_name} जी। आपसे बात करके बहुत अच्छा लगा। आपका दिन शुभ हो!"

NEVER:
- Discuss competitors
- Make up pricing
- Promise exact delivery dates — say "RERA timeline के अनुसार"
- Reveal you are AI unless directly asked — say "मैं प्रिया हूँ, GBK Group की digital assistant"

After each lead response, extract and remember:
- bhk_preference (1RK/1BHK/2BHK)
- budget_range (in lakhs)
- purchase_purpose (personal/investment)
- purchase_timeline (30 days/3 months/6 months/exploring)
- site_visit_booked (yes/no)
- visit_day (Saturday/Sunday/specific date)
- lead_score (HOT/WARM/COLD)
"""

# ─── ACTIVE CALL SESSIONS ──────────────────────────────────────

class CallSession:
    """Manages state for a single active call"""
    def __init__(self, call_uuid: str, from_number: str, to_number: str):
        self.call_uuid = call_uuid
        self.from_number = from_number
        self.to_number = to_number
        self.conversation_history = []
        self.audio_buffer = bytearray()
        self.silence_samples = 0
        self.is_speaking = False
        self.greeting_sent = False
        self.start_time = datetime.now()
        self.extracted_data = {
            "bhk_preference": "not mentioned",
            "budget_range": "not mentioned",
            "purchase_purpose": "not mentioned",
            "purchase_timeline": "not mentioned",
            "site_visit_booked": "No",
            "visit_day": "not confirmed",
            "lead_score": "WARM",
            "call_summary": ""
        }
        # Lead context (can be set from Make.com webhook data)
        self.lead_name = "Sir/Madam"
        self.lead_project = "Vishwajeet Heights"

    def get_system_prompt(self):
        return GBK_SYSTEM_PROMPT.format(
            lead_name=self.lead_name,
            lead_project=self.lead_project
        )

# Store active sessions
active_sessions: dict[str, CallSession] = {}


# ─── SARVAM AI FUNCTIONS ───────────────────────────────────────

async def sarvam_stt(audio_bytes: bytes) -> str:
    """Convert speech to text using Sarvam Saaras v2"""
    try:
        import io
        import wave

        # Create WAV file in memory from raw PCM
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, 'wb') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)  # 16-bit
            wav_file.setframerate(SAMPLE_RATE)
            wav_file.writeframes(audio_bytes)
        wav_buffer.seek(0)

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                "https://api.sarvam.ai/speech-to-text",
                headers={"api-subscription-key": SARVAM_API_KEY},
                files={"file": ("audio.wav", wav_buffer, "audio/wav")},
                data={
                    "model": "saaras:v2",
                    "language_code": "hi-IN",
                    "with_timestamps": "false",
                }
            )

            if response.status_code == 200:
                result = response.json()
                transcript = result.get("transcript", "").strip()
                logger.info(f"STT result: {transcript}")
                return transcript
            else:
                logger.error(f"Sarvam STT error: {response.status_code} - {response.text}")
                return ""

    except Exception as e:
        logger.error(f"STT exception: {e}")
        return ""


async def sarvam_tts(text: str) -> bytes:
    """Convert text to speech using Sarvam Bulbul v2"""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                "https://api.sarvam.ai/text-to-speech",
                headers={
                    "api-subscription-key": SARVAM_API_KEY,
                    "Content-Type": "application/json"
                },
                json={
                    "inputs": [text],
                    "target_language_code": "hi-IN",
                    "speaker": "meera",
                    "pitch": 0,
                    "pace": 1.0,
                    "loudness": 1.5,
                    "speech_sample_rate": SAMPLE_RATE,
                    "enable_preprocessing": True,
                    "model": "bulbul:v2"
                }
            )

            if response.status_code == 200:
                result = response.json()
                audio_b64 = result.get("audios", [None])[0]
                if audio_b64:
                    audio_bytes = base64.b64decode(audio_b64)
                    logger.info(f"TTS generated: {len(audio_bytes)} bytes")
                    return audio_bytes
                else:
                    logger.error("No audio in TTS response")
                    return b""
            else:
                logger.error(f"Sarvam TTS error: {response.status_code} - {response.text}")
                return b""

    except Exception as e:
        logger.error(f"TTS exception: {e}")
        return b""


# ─── OPENAI FUNCTIONS ──────────────────────────────────────────

async def openai_chat(session: CallSession, user_message: str) -> str:
    """Get AI response from OpenAI GPT-4o-mini"""
    try:
        # Add user message to history
        session.conversation_history.append({
            "role": "user",
            "content": user_message
        })

        messages = [
            {"role": "system", "content": session.get_system_prompt()}
        ] + session.conversation_history

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "gpt-4o-mini",
                    "messages": messages,
                    "temperature": 0.3,
                    "max_tokens": 250
                }
            )

            if response.status_code == 200:
                result = response.json()
                ai_response = result["choices"][0]["message"]["content"].strip()
                session.conversation_history.append({
                    "role": "assistant",
                    "content": ai_response
                })
                logger.info(f"AI response: {ai_response}")
                return ai_response
            else:
                logger.error(f"OpenAI error: {response.status_code} - {response.text}")
                return "Sorry, ek minute please."

    except Exception as e:
        logger.error(f"OpenAI exception: {e}")
        return "Sorry, ek minute please."


async def extract_call_data(session: CallSession) -> dict:
    """Extract structured data from conversation using OpenAI"""
    try:
        conversation_text = "\n".join([
            f"{'Lead' if m['role'] == 'user' else 'Priya'}: {m['content']}"
            for m in session.conversation_history
        ])

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "gpt-4o-mini",
                    "messages": [
                        {
                            "role": "system",
                            "content": """Extract the following from this real estate sales call conversation. Return ONLY a JSON object with these exact keys:
{
  "bhk_preference": "1RK or 1BHK or 2BHK or not mentioned",
  "budget_range": "amount in lakhs or not mentioned",
  "purchase_purpose": "personal or investment or not mentioned",
  "purchase_timeline": "within 30 days or within 3 months or within 6 months or just exploring or not mentioned",
  "site_visit_booked": "Yes or No",
  "visit_day": "Saturday or Sunday or specific date or not confirmed",
  "lead_score": "HOT or WARM or COLD. HOT=budget confirmed+timeline within 30 days+site visit booked. WARM=interested but exploring. COLD=not interested.",
  "call_summary": "2-3 sentence summary of the call"
}
Return ONLY the JSON. No other text."""
                        },
                        {
                            "role": "user",
                            "content": conversation_text
                        }
                    ],
                    "temperature": 0.1,
                    "max_tokens": 300
                }
            )

            if response.status_code == 200:
                result = response.json()
                text = result["choices"][0]["message"]["content"].strip()
                # Clean up potential markdown formatting
                text = text.replace("```json", "").replace("```", "").strip()
                extracted = json.loads(text)
                logger.info(f"Extracted data: {extracted}")
                return extracted
            else:
                logger.error(f"Extraction error: {response.status_code}")
                return session.extracted_data

    except Exception as e:
        logger.error(f"Extraction exception: {e}")
        return session.extracted_data


# ─── AUDIO UTILITY FUNCTIONS ──────────────────────────────────

def pcm_to_mulaw(pcm_data: bytes) -> bytes:
    """Convert 16-bit PCM to µ-law for Vobiz"""
    MULAW_MAX = 0x1FFF
    MULAW_BIAS = 33

    mulaw_bytes = bytearray()
    for i in range(0, len(pcm_data), 2):
        if i + 1 >= len(pcm_data):
            break
        sample = struct.unpack_from('<h', pcm_data, i)[0]

        sign = 0
        if sample < 0:
            sign = 0x80
            sample = -sample

        sample = min(sample, MULAW_MAX)
        sample += MULAW_BIAS

        exponent = 7
        for exp in range(7, 0, -1):
            if sample < (1 << (exp + 3)):
                exponent = exp - 1

        mantissa = (sample >> (exponent + 3)) & 0x0F
        mulaw_byte = ~(sign | (exponent << 4) | mantissa) & 0xFF
        mulaw_bytes.append(mulaw_byte)

    return bytes(mulaw_bytes)


def detect_silence(audio_bytes: bytes, threshold: int = SILENCE_THRESHOLD) -> bool:
    """Detect if audio chunk is silence"""
    if len(audio_bytes) < 2:
        return True

    samples = []
    for i in range(0, len(audio_bytes) - 1, 2):
        sample = struct.unpack_from('<h', audio_bytes, i)[0]
        samples.append(abs(sample))

    if not samples:
        return True

    avg_amplitude = sum(samples) / len(samples)
    return avg_amplitude < threshold


# ─── FASTAPI APP ───────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 GBK Voice Agent Server starting...")
    logger.info(f"📡 Public URL: {PUBLIC_URL}")
    yield
    logger.info("🛑 Server shutting down...")

app = FastAPI(title="GBK Group Voice Agent", lifespan=lifespan)


# ─── ENDPOINT: /answer ─────────────────────────────────────────
# Vobiz calls this when the lead picks up
# Returns XML with WebSocket stream URL

@app.post("/answer")
@app.get("/answer")
async def answer_call(request: Request):
    """Handle answered call — return XML to start WebSocket stream"""
    form_data = await request.form() if request.method == "POST" else request.query_params

    call_uuid = form_data.get("CallUUID", "unknown")
    from_number = form_data.get("From", "unknown")
    to_number = form_data.get("To", "unknown")

    logger.info(f"📞 Call answered: {call_uuid} | From: {from_number} | To: {to_number}")

    # Create session
    session = CallSession(call_uuid, from_number, to_number)
    active_sessions[call_uuid] = session

    # WebSocket URL for audio streaming
    ws_url = PUBLIC_URL.replace("https://", "wss://").replace("http://", "ws://")

    xml_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Stream
        bidirectional="true"
        keepCallAlive="true"
        contentType="audio/x-mulaw;rate=8000"
        statusCallbackUrl="{PUBLIC_URL}/stream-status"
        extraHeaders="callUUID={call_uuid}">
        {ws_url}/ws
    </Stream>
    <Record
        action="{PUBLIC_URL}/recording-done"
        recordSession="true"
        maxLength="300"
        fileFormat="mp3"
    />
</Response>"""

    logger.info(f"Returning XML with WebSocket URL: {ws_url}/ws")
    return Response(content=xml_response, media_type="application/xml")


# ─── ENDPOINT: /ws ──────────────────────────────────────────────
# WebSocket for bidirectional audio streaming

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Handle bidirectional audio stream from Vobiz"""
    await websocket.accept()
    logger.info("🔌 WebSocket connected")

    session: Optional[CallSession] = None
    stream_id = None

    try:
        async for message in websocket.iter_text():
            data = json.loads(message)
            event = data.get("event", "")

            # ── STREAM START ──
            if event == "start":
                stream_id = data.get("streamId", "unknown")
                call_uuid = data.get("callId", "")

                # Try to find session from extraHeaders or callId
                if call_uuid and call_uuid in active_sessions:
                    session = active_sessions[call_uuid]
                else:
                    # Create a new session if not found
                    session = CallSession(call_uuid or stream_id, "", "")
                    if call_uuid:
                        active_sessions[call_uuid] = session

                logger.info(f"▶️ Stream started: {stream_id} | Call: {call_uuid}")

                # Send greeting after a small delay
                if session and not session.greeting_sent:
                    session.greeting_sent = True
                    asyncio.create_task(
                        send_greeting(websocket, session)
                    )

            # ── INCOMING AUDIO (lead speaking) ──
            elif event == "media":
                if not session:
                    continue

                media = data.get("media", {})
                payload = media.get("payload", "")
                if not payload:
                    continue

                # Decode incoming audio
                audio_chunk = base64.b64decode(payload)

                # Check if this chunk is silence or speech
                is_silence = detect_silence(audio_chunk)

                if not is_silence:
                    # Lead is speaking — accumulate audio
                    session.audio_buffer.extend(audio_chunk)
                    session.silence_samples = 0
                    session.is_speaking = True
                else:
                    if session.is_speaking:
                        session.silence_samples += CHUNK_DURATION_MS
                        # If silence exceeds threshold, process the utterance
                        if session.silence_samples >= SILENCE_DURATION_MS:
                            if len(session.audio_buffer) > CHUNK_SIZE * 5:
                                # Process accumulated audio
                                audio_to_process = bytes(session.audio_buffer)
                                session.audio_buffer.clear()
                                session.is_speaking = False
                                session.silence_samples = 0

                                # Process in background
                                asyncio.create_task(
                                    process_utterance(websocket, session, audio_to_process)
                                )

            # ── STREAM STOP ──
            elif event == "stop":
                logger.info(f"⏹️ Stream stopped: {stream_id}")
                if session:
                    asyncio.create_task(handle_call_end(session))
                break

    except WebSocketDisconnect:
        logger.info("🔌 WebSocket disconnected")
        if session:
            asyncio.create_task(handle_call_end(session))
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        if session:
            asyncio.create_task(handle_call_end(session))


async def send_greeting(websocket: WebSocket, session: CallSession):
    """Send initial greeting via TTS"""
    try:
        await asyncio.sleep(0.5)  # Small delay for connection to stabilize

        greeting = f"नमस्ते {session.lead_name} जी, मैं प्रिया बोल रही हूँ GBK Group की तरफ़ से। आपने हाल ही में {session.lead_project} में interest दिखाया था — क्या अभी एक-दो minute बात हो सकती है?"

        session.conversation_history.append({
            "role": "assistant",
            "content": greeting
        })

        audio_bytes = await sarvam_tts(greeting)
        if audio_bytes:
            await send_audio_to_vobiz(websocket, audio_bytes)
            logger.info("✅ Greeting sent")
        else:
            logger.error("❌ Failed to generate greeting audio")

    except Exception as e:
        logger.error(f"Greeting error: {e}")


async def process_utterance(websocket: WebSocket, session: CallSession, audio_bytes: bytes):
    """Process a complete utterance: STT → LLM → TTS → send back"""
    try:
        # Step 1: Speech to Text
        transcript = await sarvam_stt(audio_bytes)
        if not transcript or len(transcript.strip()) < 2:
            logger.info("Empty transcript, skipping")
            return

        logger.info(f"🎤 Lead said: {transcript}")

        # Step 2: Get AI response
        ai_response = await openai_chat(session, transcript)
        logger.info(f"🤖 Priya says: {ai_response}")

        # Step 3: Text to Speech
        response_audio = await sarvam_tts(ai_response)

        # Step 4: Send audio back to Vobiz
        if response_audio:
            await send_audio_to_vobiz(websocket, response_audio)
            logger.info("🔊 Response audio sent to lead")
        else:
            logger.error("Failed to generate response audio")

    except Exception as e:
        logger.error(f"Process utterance error: {e}")


async def send_audio_to_vobiz(websocket: WebSocket, audio_bytes: bytes):
    """Send audio back to Vobiz via WebSocket playAudio events"""
    try:
        # Send audio in chunks for smooth playback
        chunk_size = 640  # 40ms at 8kHz, 16-bit = 640 bytes

        for i in range(0, len(audio_bytes), chunk_size):
            chunk = audio_bytes[i:i + chunk_size]
            chunk_b64 = base64.b64encode(chunk).decode('utf-8')

            play_event = {
                "event": "playAudio",
                "media": {
                    "contentType": "audio/x-l16",
                    "sampleRate": SAMPLE_RATE,
                    "payload": chunk_b64
                }
            }

            await websocket.send_json(play_event)
            # Small delay to match real-time playback
            await asyncio.sleep(CHUNK_DURATION_MS / 1000)

    except Exception as e:
        logger.error(f"Send audio error: {e}")


# ─── POST-CALL HANDLING ─────────────────────────────────────────

async def handle_call_end(session: CallSession):
    """Extract data and send to Make.com webhook"""
    try:
        logger.info(f"📊 Processing call end for: {session.call_uuid}")

        # Extract structured data from conversation
        if session.conversation_history:
            extracted = await extract_call_data(session)
            session.extracted_data.update(extracted)

        # Send to Make.com webhook
        payload = {
            "call_uuid": session.call_uuid,
            "from_number": session.from_number,
            "to_number": session.to_number,
            "call_duration": (datetime.now() - session.start_time).total_seconds(),
            "status": "completed",
            "lead_name": session.lead_name,
            "lead_project": session.lead_project,
            "extracted_data": session.extracted_data,
            "conversation_turns": len(session.conversation_history),
            "timestamp": datetime.now().isoformat()
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(MAKE_WEBHOOK_URL, json=payload)
            logger.info(f"📤 Sent to Make.com: {response.status_code}")

        # Cleanup session
        if session.call_uuid in active_sessions:
            del active_sessions[session.call_uuid]

    except Exception as e:
        logger.error(f"Handle call end error: {e}")


# ─── ENDPOINT: /hangup ─────────────────────────────────────────

@app.post("/hangup")
@app.get("/hangup")
async def hangup_call(request: Request):
    """Handle call hangup from Vobiz"""
    form_data = await request.form() if request.method == "POST" else request.query_params
    call_uuid = form_data.get("CallUUID", "unknown")
    logger.info(f"📵 Call hung up: {call_uuid}")

    if call_uuid in active_sessions:
        session = active_sessions[call_uuid]
        await handle_call_end(session)

    return Response(content="OK", status_code=200)


# ─── ENDPOINT: /stream-status ──────────────────────────────────

@app.post("/stream-status")
async def stream_status(request: Request):
    """Handle stream status callbacks from Vobiz"""
    form_data = await request.form()
    event = form_data.get("Event", "")
    call_uuid = form_data.get("CallUUID", "")
    logger.info(f"📡 Stream status: {event} | Call: {call_uuid}")
    return Response(content="OK", status_code=200)


# ─── ENDPOINT: /recording-done ─────────────────────────────────

@app.post("/recording-done")
async def recording_done(request: Request):
    """Handle recording completion from Vobiz"""
    form_data = await request.form()
    recording_url = form_data.get("RecordingURL", "")
    call_uuid = form_data.get("CallUUID", "")
    logger.info(f"🎙️ Recording ready: {recording_url} | Call: {call_uuid}")

    # You can store recording URL in GHL via Make webhook
    return Response(content="OK", status_code=200)


# ─── ENDPOINT: /set-lead-context ────────────────────────────────
# Call this from Make.com BEFORE triggering the Vobiz call
# to set lead name and project for personalization

@app.post("/set-lead-context")
async def set_lead_context(request: Request):
    """Set lead context before call starts"""
    data = await request.json()
    phone = data.get("phone", "")
    lead_name = data.get("name", "Sir/Madam")
    lead_project = data.get("project", "Vishwajeet Heights")

    # Store in a temporary lookup
    if not hasattr(app, "lead_context"):
        app.lead_context = {}

    app.lead_context[phone] = {
        "name": lead_name,
        "project": lead_project
    }

    logger.info(f"📋 Lead context set: {phone} → {lead_name} | {lead_project}")
    return {"status": "ok"}


# ─── HEALTH CHECK ──────────────────────────────────────────────

@app.get("/")
async def health():
    return {
        "status": "running",
        "agent": "Priya - GBK Group",
        "active_calls": len(active_sessions),
        "timestamp": datetime.now().isoformat()
    }


# ─── RUN ───────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
