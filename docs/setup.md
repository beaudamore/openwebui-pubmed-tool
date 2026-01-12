# PubMed Deep Research Tool - Setup Guide

This tool requires additional Python packages (`pandas`, `spacy`, `nltk`). OpenWebUI will attempt to auto-install these from the `requirements:` field in the tool header.

## Automatic Installation

When you add this tool to OpenWebUI, it will automatically try to install the dependencies. If you see errors, the packages may need to be pre-installed using one of the methods below.

---

## Manual Installation Options

If automatic installation fails, use one of these methods:

```bash
# Enter your OpenWebUI container
docker exec -it open-webui bash

# Install required packages (use requirements.txt for pinned versions)
pip install pandas spacy nltk

# Alternative: Use the requirements.txt file
# pip install -r /path/to/requirements.txt

# Exit the container
exit
```

> **Note:** The spaCy language model (`en_core_web_sm`) and NLTK data will be downloaded automatically on first use.

### Troubleshooting Installation Errors

If you encounter installation errors, try these solutions:

**1. Install system build tools first (for containers based on Debian/Ubuntu):**
```bash
apt-get update
apt-get install -y gcc g++ python3-dev build-essential
pip install pandas spacy nltk
```

**2. Install packages one at a time:**
```bash
pip install pandas
pip install spacy
pip install nltk
```

**3. Use --no-cache-dir to avoid cache issues:**
```bash
pip install --no-cache-dir pandas spacy nltk
```

**4. Upgrade pip first:**
```bash
pip install --upgrade pip
pip install pandas spacy nltk
```

**5. Check available disk space:**
```bash
df -h
```

---

## Option 2: Custom Dockerfile (Persistent Installation)

If you have your own fork or custom deployment of OpenWebUI, add the dependencies directly to your Dockerfile for a persistent installation.

### Basic Dockerfile Addition

Add these lines to your existing OpenWebUI Dockerfile:

```dockerfile
# Install PubMed Tool dependencies
RUN pip install --no-cache-dir pandas spacy nltk

# Pre-download spaCy model (optional - will auto-download on first use)
RUN python -m spacy download en_core_web_sm

# Pre-download NLTK data (optional - will auto-download on first use)
RUN python -c "import nltk; nltk.download('punkt'); nltk.download('punkt_tab'); nltk.download('stopwords')"
```

### Complete Custom Dockerfile Example

If you're extending the official OpenWebUI image:

```dockerfile
FROM ghcr.io/open-webui/open-webui:main

# Install PubMed Deep Research Tool dependencies
RUN pip install --no-cache-dir \
    pandas \
    spacy \
    nltk

# Pre-download models and data for faster first-run experience
RUN python -m spacy download en_core_web_sm && \
    python -c "import nltk; nltk.download('punkt'); nltk.download('punkt_tab'); nltk.download('stopwords')"

# Continue with any other customizations...
```

### Build and Run Your Custom Image

```bash
# Build the custom image
docker build -t openwebui-custom:latest .

# Run with your custom image (adjust volume paths as needed)
docker run -d \
  --name open-webui \
  -p 3000:8080 \
  -v open-webui:/app/backend/data \
  openwebui-custom:latest
```

---

## Option 3: Docker Compose Override

If you use Docker Compose, you can create a custom build context:

### Directory Structure

```
my-openwebui/
├── docker-compose.yml
├── Dockerfile
└── ...
```

### docker-compose.yml

```yaml
version: '3.8'
services:
  open-webui:
    build:
      context: .
      dockerfile: Dockerfile
    ports:
      - "3000:8080"
    volumes:
      - open-webui:/app/backend/data
    restart: unless-stopped

volumes:
  open-webui:
```

### Dockerfile

```dockerfile
FROM ghcr.io/open-webui/open-webui:main

# PubMed Tool dependencies
RUN pip install --no-cache-dir pandas spacy nltk && \
    python -m spacy download en_core_web_sm && \
    python -c "import nltk; nltk.download('punkt'); nltk.download('punkt_tab'); nltk.download('stopwords')"
```

### Build and Start

```bash
docker-compose up -d --build
```

---

## Verifying Installation

After installation (any method), verify the packages are available:

```bash
docker exec -it open-webui python -c "import pandas, spacy, nltk; print('All dependencies installed!')"
```

---

## Troubleshooting

### Container name differs
If your container has a different name, find it with:
```bash
docker ps
```

### Permission errors
If you encounter permission errors, try:
```bash
docker exec -it --user root open-webui pip install pandas spacy nltk
```

### Disk space
The spaCy model and NLTK data require approximately 50MB of additional space.