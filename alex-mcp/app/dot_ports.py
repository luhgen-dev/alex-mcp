"""Dedicated loopback endpoints; existing WhatsApp egress owns port 5002."""
DOT_HOST = "127.0.0.1"
DOT_PORT = 5012
DOT_HEALTH_PORT = 5013
DOT_RESOURCE = f"http://{DOT_HOST}:{DOT_PORT}/mcp/"
DOT_HEALTH_URL = f"http://{DOT_HOST}:{DOT_HEALTH_PORT}/readyz"
