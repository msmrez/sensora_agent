# Use an official lightweight Python image as a base
FROM python:3.12-slim

# Set metadata labels for the image
LABEL org.opencontainers.image.source="git@github.com:msmrez/sensora_agent.git"
LABEL org.opencontainers.image.description="Autonomous IoT data provider for the Sensora Network."

# Set the working directory inside the container
WORKDIR /app

# Copy only the requirements file first to leverage Docker's build cache
COPY requirements.txt .

# Install the Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application source code
COPY src/ ./src/
COPY app.py .
COPY templates/ ./templates/

# Expose the port the agent will listen on internally
EXPOSE 8083

# The command that will be run when the container starts.
# It expects the config.ini file to be available at /app/config.ini
CMD ["python", "app.py"]


