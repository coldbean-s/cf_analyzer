FROM python:3.12-slim

# Install Chrome dependencies + Chrome
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg2 curl \
    && wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | apt-key add - \
    && echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google.list \
    && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install chromium

COPY . .

# Create data directories
RUN mkdir -p data/cf_browser_profiles/shared

EXPOSE 6010

CMD ["python", "app.py"]
