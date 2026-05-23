#!/bin/bash
# ==============================================================================
# AgentIC CLI Installer
# This script downloads and installs the AgentIC standalone binary.
# ==============================================================================

# Set installation path (requires sudo for /usr/local/bin)
INSTALL_PATH="/usr/local/bin/agentic"

# Replace this URL with your actual hosted link (e.g., from GitHub Releases)
# This is where the compiled binary will be downloaded from.
DOWNLOAD_URL="https://your-website.com/downloads/agentic"

echo "🚀 Installing AgentIC CLI tool..."

# Detect Operating System (Linux only for now)
if [[ "$OSTYPE" != "linux-gnu"* ]]; then
    echo "❌ Error: This installer currently only supports Linux."
    exit 1
fi

# Download the standalone binary
echo "📥 Downloading binary from $DOWNLOAD_URL..."
curl -L -o agentic_tmp "$DOWNLOAD_URL"

# Check if download succeeded
if [ $? -ne 0 ]; then
    echo "❌ Error: Failed to download AgentIC binary. Please check your internet connection."
    exit 1
fi

# Make it executable
chmod +x agentic_tmp

# Move to the system bin directory
echo "🔒 Finalizing installation (requires sudo password)..."
sudo mv agentic_tmp "$INSTALL_PATH"

# Verify installation
if command -v agentic >/dev/null 2>&1; then
    echo "✅ AgentIC installed successfully!"
    echo "👉 Run 'agentic login <license_key>' to authenticate."
    echo "👉 Run 'agentic build --help' to see all options."
else
    echo "❌ Error: Installation failed. Please ensure /usr/local/bin is in your PATH."
    exit 1
fi
