# 1. Use an official lightweight Python runtime as a parent image
FROM python:3.11-slim

# 2. Set the working directory in the container
WORKDIR /app

# 3. Install system dependencies (THIS IS WHERE WE INSTALL FFMPEG)
# We update the package list, install ffmpeg, and clean up to keep it small.
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# 4. Copy the requirements file into the container
COPY requirements.txt .

# 5. Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# 6. Copy the rest of your bot's code
COPY . .

# 7. Run the bot
# IMPORTANT: Change 'bot.py' to whatever your main file is named (e.g., main.py)
CMD ["python", "gg.py"]
