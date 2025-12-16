import discord
from discord import app_commands
from google import genai
from google.genai import types
from google.genai.errors import APIError
import re
import os
import asyncio
import tempfile

# --- Configuration ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# IMPORTANT: 2.5 does not exist yet. Use 2.0-flash-exp or 1.5-flash.
# 2.0 is recommended for better search/grounding results.
MODEL = "gemini-2.0-flash-exp" 

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

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# Structure: {channel_id: [{"role": "user", "text": "...", "files": [...]}]}
channel_histories = {}
MAX_HISTORY_MESSAGES = 20

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
            # Download to temp file
            with tempfile.NamedTemporaryFile(delete=False, suffix=f"_{attachment.filename}") as tmp_file:
                await attachment.save(tmp_file.name)
                tmp_path = tmp_file.name

            # Upload to Gemini
            # Run in thread to not block Discord
            uploaded_file = await asyncio.to_thread(
                client_gemini.files.upload,
                file=tmp_path
            )
            
            # Clean up local temp file
            os.remove(tmp_path)

            # Wait for processing (Active state)
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
    """
    Converts internal history list to strict Gemini Content objects.
    Crucial Fix: Wraps files in types.Part.from_uri
    """
    contents = []
    
    for message in history:
        parts = []
        
        # 1. Add File Parts
        if "files" in message and message["files"]:
            for f in message["files"]:
                parts.append(types.Part.from_uri(
                    file_uri=f.uri,
                    mime_type=f.mime_type
                ))
        
        # 2. Add Text Part
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
    
    # Handle File
    gemini_files = []
    if image:
        gemini_files = await upload_attachments_to_gemini([image])
        
    user_text = f"{interaction.user.display_name}: {question}"
    
    # Append USER message to history
    history.append({
        "role": "user",
        "text": user_text,
        "files": gemini_files
    })
    
    await process_and_send_response(interaction.channel, history, interaction)

# --- Core Logic ---
async def process_and_send_response(channel, history, interaction=None):
    if not client_gemini:
        return

    # Prepare System Prompt
    last_user_text = history[-1]["text"] if history else ""
    lang_instruction = detect_language(last_user_text)
    
    system_instruction = (
        "You are a helpful Discord Bot. "
        "You have access to Google Search to find real-time information. "
        "If asked for current events, news, or specific data like Wordle answers, USE SEARCH to find it. "
        "When analyzing images, describe them in detail if asked. "
        f"{lang_instruction}"
    )

    try:
        # Convert history to strict API format
        api_contents = convert_history_for_gemini(history)
        
        # --- HERE IS THE FIX: Added 'tools' for Google Search ---
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0.7,
            max_output_tokens=1500,
            tools=[types.Tool(google_search=types.GoogleSearch())] # <--- ENABLE SEARCH
        )

        # Generate Response
        if interaction:
             pass 
        else:
            await channel.typing()

        response = await asyncio.to_thread(
            client_gemini.models.generate_content,
            model=MODEL,
            contents=api_contents,
            config=config
        )

        # Handle cases where search returns text differently
        bot_reply = ""
        if response.text:
            bot_reply = response.text.strip()
        else:
            # Fallback if the model returns only grounding metadata but no text (rare)
            bot_reply = "[I found some info, but couldn't generate a text response. Try asking again.]"
        
        # Add MODEL response to history
        history.append({
            "role": "model",
            "text": bot_reply,
            "files": []
        })
        
        # Trim history
        if len(history) > MAX_HISTORY_MESSAGES:
            channel_histories[channel.id] = history[-MAX_HISTORY_MESSAGES:]

        # Send to Discord (chunking if long)
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
        # Remove the failed user message so history doesn't break
        if history and history[-1]["role"] == "user":
            history.pop()

# --- Discord Events ---
@bot.event
async def on_ready():
    await tree.sync()
    print(f"Bot {bot.user} is online! Model: {MODEL}")

@bot.event
async def on_message(message: discord.Message):
    # Ignore own messages
    if message.author == bot.user:
        return

    # Check triggers: Mention, Reply to bot, or DM
    is_triggered = False
    if bot.user in message.mentions:
        is_triggered = True
    elif message.reference and message.reference.resolved:
        if message.reference.resolved.author == bot.user:
            is_triggered = True
    elif isinstance(message.channel, discord.DMChannel):
        is_triggered = True

    if not is_triggered:
        return

    channel_id = message.channel.id
    if channel_id not in channel_histories:
        channel_histories[channel_id] = []
    
    history = channel_histories[channel_id]

    # Clean content
    content = message.content.replace(f"<@{bot.user.id}>", "").strip()
    
    # Handle Attachments
    gemini_files = []
    if message.attachments:
        await message.channel.typing() # Show typing while uploading
        gemini_files = await upload_attachments_to_gemini(message.attachments)

    if not content and not gemini_files:
        return # Nothing to process

    user_text = f"{message.author.display_name}: {content}"

    # Add to history with ROLE 'user'
    history.append({
        "role": "user",
        "text": user_text,
        "files": gemini_files
    })

    await process_and_send_response(message.channel, history)

# --- Run ---
if DISCORD_TOKEN and GEMINI_API_KEY:
    bot.run(DISCORD_TOKEN)
