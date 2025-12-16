import discord
from discord import app_commands
from google import genai
from google.genai import types
import re
import os
import asyncio
import tempfile
import audioop
import wave

# --- Configuration ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Text/Multimodal Model
TEXT_MODEL = "gemini-2.5-flash-lite"

# Voice Model (Gemini Live API)
VOICE_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"

# --- Initialize Gemini Client ---
client_gemini = None
try:
    if GEMINI_API_KEY:
        client_gemini = genai.Client(api_key=GEMINI_API_KEY)
    else:
        print("Error: GEMINI_API_KEY environment variable not set.")
except Exception as e:
    print(f"Error initializing Gemini client: {e}")

# --- Discord Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# Structure: {channel_id: [{"role": "user", "text": "...", "files": [...]}]}
channel_histories = {}
MAX_HISTORY_MESSAGES = 20

# Structure: {voice_channel_id: {"session": session_object, "text_channel": text_channel_obj}}
voice_sessions = {}

# --- Utility Functions ---
def detect_language(text: str) -> str:
    if re.search(r'[а-яА-Я]', text):
        return "Отвечай на русском языке."
    return "Reply in English."

async def upload_attachments_to_gemini(attachments: list[discord.Attachment]) -> list:
    """Uploads files to Gemini and returns the File objects."""
    gemini_files = []
    supported_mimes = {
        'image/png', 'image/jpeg', 'image/webp', 'image/heic', 'image/heif',
        'video/mp4', 'video/mpeg', 'video/mov', 'video/avi', 'video/x-flv',
        'video/mpg', 'video/webm', 'video/wmv', 'video/3gpp',
        'audio/wav', 'audio/mp3', 'audio/aiff', 'audio/aac', 'audio/ogg',
        'audio/flac', 'application/pdf'
    }
    for attachment in attachments:
        if attachment.content_type and attachment.content_type not in supported_mimes:
            continue
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=f"_{attachment.filename}") as tmp_file:
                await attachment.save(tmp_file.name)
                tmp_path = tmp_file.name
            
            uploaded_file = await asyncio.to_thread(
                client_gemini.files.upload,
                file=tmp_path
            )
            os.remove(tmp_path)
            
            # Wait for processing
            while uploaded_file.state.name == "PROCESSING":
                await asyncio.sleep(1)
                uploaded_file = await asyncio.to_thread(client_gemini.files.get, name=uploaded_file.name)
            
            if uploaded_file.state.name == "ACTIVE":
                gemini_files.append(uploaded_file)
            else:
                print(f"File {attachment.filename} failed: {uploaded_file.state.name}")
        except Exception as e:
            print(f"Error uploading attachment {attachment.filename}: {e}")
    return gemini_files

def convert_history_for_gemini(history: list) -> list[types.Content]:
    contents = []
    for message in history:
        parts = []
        if "files" in message and message["files"]:
            for f in message["files"]:
                parts.append(types.Part.from_uri(
                    file_uri=f.uri,
                    mime_type=f.mime_type
                ))
        if "text" in message and message["text"]:
            parts.append(types.Part.from_text(text=message["text"]))
            
        if parts:
            contents.append(types.Content(
                role=message["role"],
                parts=parts
            ))
    return contents

# --- Slash Commands ---
@tree.command(name="reset", description="Clear chat history")
async def reset_conversation(interaction: discord.Interaction):
    if interaction.channel_id in channel_histories:
        del channel_histories[interaction.channel_id]
    await interaction.response.send_message("History cleared! 🧠✨", ephemeral=True)

@tree.command(name="ask", description="Ask Gemini (Text + Attachments)")
@app_commands.describe(question="Your question")
async def ask(interaction: discord.Interaction, question: str, image: discord.Attachment = None):
    await interaction.response.defer()
    
    channel_id = interaction.channel_id
    if channel_id not in channel_histories:
        channel_histories[channel_id] = []
        
    history = channel_histories[channel_id]
    gemini_files = []
    if image:
        gemini_files = await upload_attachments_to_gemini([image])
        
    user_text = f"{interaction.user.display_name}: {question}"
    history.append({
        "role": "user",
        "text": user_text,
        "files": gemini_files
    })
    
    await process_and_send_response(interaction.channel, history, interaction)

@tree.command(name="join_voice", description="Join voice channel and start Gemini voice chat")
async def join_voice(interaction: discord.Interaction):
    if not interaction.user.voice:
        await interaction.response.send_message("You must be in a voice channel!", ephemeral=True)
        return

    voice_channel = interaction.user.voice.channel
    try:
        voice_client = await voice_channel.connect()
    except discord.ClientException:
        await interaction.response.send_message("I am already in a voice channel here.", ephemeral=True)
        return
    except Exception as e:
         await interaction.response.send_message(f"Could not join voice: {e}", ephemeral=True)
         return

    await interaction.response.send_message(f"Joined {voice_channel.name}! **Type in this chat** to speak to Gemini in voice.")

    # Start voice session task
    asyncio.create_task(start_voice_session(voice_client, interaction.channel))

@tree.command(name="leave_voice", description="Leave voice channel")
async def leave_voice(interaction: discord.Interaction):
    if interaction.guild.voice_client:
        await interaction.guild.voice_client.disconnect()
        await interaction.response.send_message("Disconnected from voice.")
    else:
        await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)

# --- Core Logic for Text ---
async def process_and_send_response(channel, history, interaction=None):
    if not client_gemini:
        return

    # 1. Check if we have an active Live Session for this channel's guild
    # If so, route text there instead of standard generation
    guild_id = channel.guild.id if hasattr(channel, 'guild') else None
    active_voice_client = channel.guild.voice_client if guild_id else None
    
    if active_voice_client and active_voice_client.channel.id in voice_sessions:
        # Route to Live API (Text -> Voice Output)
        session_data = voice_sessions[active_voice_client.channel.id]
        session = session_data["session"]
        last_msg = history[-1]["text"]
        
        # Send text input to the Live Session
        # Note: 'turn_complete=True' triggers the model to generate audio response
        await session.send_client_content(
            turns=[types.Content(role="user", parts=[types.Part.from_text(text=last_msg)])],
            turn_complete=True
        )
        
        # Add visual confirmation
        if interaction:
            await interaction.followup.send("✅ Sent to voice session.")
        return

    # 2. Standard Text Generation (Fallback)
    last_user_text = history[-1]["text"] if history else ""
    lang_instruction = detect_language(last_user_text)
    
    system_instruction = (
        "You are a helpful Discord Bot. "
        "Use Google Search for current events. "
        f"{lang_instruction}"
    )
    
    try:
        api_contents = convert_history_for_gemini(history)
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0.7,
            max_output_tokens=1500,
            tools=[types.Tool(google_search=types.GoogleSearch())]
        )

        if not interaction:
            await channel.typing()
            
        response = await asyncio.to_thread(
            client_gemini.models.generate_content,
            model=TEXT_MODEL,
            contents=api_contents,
            config=config
        )

        bot_reply = response.text.strip() if response.text else "[Found info, but no text response generated.]"
        
        history.append({
            "role": "model",
            "text": bot_reply,
            "files": []
        })
        
        if len(history) > MAX_HISTORY_MESSAGES:
            channel_histories[channel.id] = history[-MAX_HISTORY_MESSAGES:]

        async def send_chunks(text, is_interaction=False):
            if not text: return
            for i in range(0, len(text), 1900):
                chunk = text[i:i+1900]
                if is_interaction and i == 0:
                    await interaction.followup.send(chunk)
                elif is_interaction:
                    await interaction.channel.send(chunk)
                else:
                    await channel.send(chunk)
        
        await send_chunks(bot_reply, is_interaction=(interaction is not None))

    except Exception as e:
        err = f"Error: {e}"
        print(err)
        if interaction:
            await interaction.followup.send(err)
        else:
            await channel.send(err)
        if history and history[-1]["role"] == "user":
            history.pop()

# --- Voice Chat Logic ---
async def start_voice_session(voice_client: discord.VoiceClient, text_channel):
    voice_channel_id = voice_client.channel.id
    
    # Correct Config for Gemini Live (removed unsupported proactive_audio)
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],  # We want audio back
        system_instruction=types.Content(parts=[types.Part.from_text(text="You are a helpful voice assistant.")])
    )

    try:
        async with client_gemini.aio.live.connect(model=VOICE_MODEL, config=config) as session:
            voice_sessions[voice_channel_id] = {"session": session, "text_channel": text_channel}
            await text_channel.send(f"🔊 Gemini Live Connected! Type in this channel to make me speak.")

            # NOTE: Standard discord.py CANNOT listen to audio.
            # We are using a Text-to-Voice loop here. 
            # (User types -> Bot Speaks)
            
            # Receiver Loop (Gemini -> Discord Audio)
            async for response in session.receive():
                if response.server_content is None:
                    continue

                # 1. Handle Audio Output
                if response.server_content.model_turn:
                     for part in response.server_content.model_turn.parts:
                        if part.inline_data:
                            audio_data = part.inline_data.data # PCM bytes
                            # Gemini Live sends 24kHz PCM. Discord wants 48kHz stereo.
                            # We play it as is (might be slightly pitch-shifted) or need resampling.
                            # For simplicity/speed, we treat it as raw PCM.
                            # Note: To do this perfectly, use audioop to resample 24k -> 48k.
                            
                            # Simple Resample attempt (24k to 48k)
                            # audio_data_48k = audioop.ratecv(audio_data, 2, 1, 24000, 48000, None)[0]
                            # Using standard PCM audio source (expects 48k stereo usually)
                            
                            # Create a temporary file or stream for FFmpeg
                            with tempfile.NamedTemporaryFile(suffix=".pcm", delete=False) as f:
                                f.write(audio_data)
                                fname = f.name
                            
                            # Play audio
                            # FFmpeg needs to know it's raw PCM, 24k rate, 1 channel, signed 16-bit little endian
                            source = discord.FFmpegPCMAudio(
                                fname, 
                                before_options="-f s16le -ar 24000 -ac 1", 
                                options="-loglevel panic"
                            )
                            
                            # Wait for previous audio to finish slightly to avoid cutting off? 
                            # Discord.py play() stops previous source. We queue if needed, but here we just play.
                            if voice_client.is_playing():
                                voice_client.stop()
                                
                            voice_client.play(source, after=lambda e: os.remove(fname))
                            
                            # Wait loop to prevent overwriting stream immediately if multiple chunks come fast
                            while voice_client.is_playing():
                                await asyncio.sleep(0.1)

                # 2. Handle Transcription (Text logs)
                if response.server_content.turn_complete:
                    # Optional: Log that turn is done
                    pass

    except Exception as e:
        print(f"Voice session error: {e}")
        await text_channel.send(f"Voice session ended due to error: {e}")
    finally:
        if voice_channel_id in voice_sessions:
            del voice_sessions[voice_channel_id]
        if voice_client.is_connected():
            await voice_client.disconnect()

# --- Discord Events ---
@bot.event
async def on_ready():
    await tree.sync()
    print(f"Bot {bot.user} is online! Text: {TEXT_MODEL} | Voice: {VOICE_MODEL}")

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    # Check triggers: Mention, Reply, DM, OR active Voice Session in the channel
    is_triggered = False
    
    # If this channel is associated with an active voice session, TRIGGER IT
    guild_id = message.guild.id if message.guild else None
    if guild_id and message.guild.voice_client:
         vc_channel_id = message.guild.voice_client.channel.id
         if vc_channel_id in voice_sessions and voice_sessions[vc_channel_id]["text_channel"] == message.channel:
             is_triggered = True

    if bot.user in message.mentions:
        is_triggered = True
    elif message.reference and message.reference.resolved and message.reference.resolved.author == bot.user:
        is_triggered = True
    elif isinstance(message.channel, discord.DMChannel):
        is_triggered = True

    if not is_triggered:
        return

    channel_id = message.channel.id
    if channel_id not in channel_histories:
        channel_histories[channel_id] = []
    
    history = channel_histories[channel_id]
    content = message.content.replace(f"<@{bot.user.id}>", "").strip()
    
    gemini_files = []
    if message.attachments:
        await message.channel.typing()
        gemini_files = await upload_attachments_to_gemini(message.attachments)

    if not content and not gemini_files:
        return

    user_text = f"{message.author.display_name}: {content}"
    history.append({
        "role": "user",
        "text": user_text,
        "files": gemini_files
    })
    
    await process_and_send_response(message.channel, history)

# --- Run ---
if DISCORD_TOKEN and GEMINI_API_KEY:
    bot.run(DISCORD_TOKEN)
