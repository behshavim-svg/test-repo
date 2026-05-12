# Use a stable Debian 12 (Bookworm) based Python image instead of the floating slim tag
# This prevents pulling 'trixie' (testing) which often has expired release files on local mirrors
FROM python:3.10-slim-bookworm

# Replace default Debian APT repositories with ArvanCloud mirrors
# Bookworm uses the debian.sources format natively
RUN sed -i 's/deb.debian.org/mirror.arvancloud.ir/g' /etc/apt/sources.list.d/debian.sources || true && \
    sed -i 's/security.debian.org\/debian-security/mirror.arvancloud.ir\/debian-security/g' /etc/apt/sources.list.d/debian.sources || true

# Install Git (Required for resetting the repository)
# Using 'Acquire::Check-Valid-Until=false' to safely bypass mirror sync delays
RUN apt-get -o Acquire::Check-Valid-Until=false update && \
    apt-get install -y git && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Set default PyPI mirror to an Iranian provider (e.g., Runflare)
# This environment variable forces pip to use the mirror globally
ENV PIP_INDEX_URL=https://mirror-pypi.runflare.com/simple/
ENV PIP_TRUSTED_HOST=mirror-pypi.runflare.com

# Install required Python packages via the configured mirror
RUN pip install --no-cache-dir requests

# Copy the consumer script into the container
COPY consumer.py /app/consumer.py

# Run the consumer script with unbuffered output for real-time Docker logging
CMD ["python", "-u", "consumer.py"]
