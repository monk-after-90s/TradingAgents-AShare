# Stage 1: Build Frontend
FROM node:20-slim AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# Stage 2: Final Image
FROM ghcr.io/astral-sh/uv:python3.10-bookworm-slim AS runtime
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy backend dependency files
COPY pyproject.toml uv.lock ./
# Install backend dependencies (optimized with uv)
RUN uv sync --frozen --no-install-project --no-dev

# Copy backend source code
COPY api/ ./api/
COPY tradingagents/ ./tradingagents/
COPY main.py ./

# Copy built frontend from Stage 1
COPY --from=frontend-builder /app/frontend/dist ./frontend/dist

# Expose port 8000
EXPOSE 8000

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Command to run the application
# We use 'uv run' to ensure dependencies are loaded from the sync environment
CMD ["uv", "run", "python", "main.py"]
