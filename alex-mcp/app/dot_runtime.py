"""Runtime composition for optional local dot transport. No model API calls."""
from __future__ import annotations

import os
import json
import re
import subprocess
import urllib.request
import stat
import threading
import time

from config import DATA_DIR, get_settings


def configured(settings):
    return bool(settings.dot_enabled and settings.dot_owner_subject
                and settings.dot_transport in ("private_tunnel", "oauth"))


def get_bridge():
    import db
    from dot_bridge import DotBridge, execute_shopping
    from dot_events import EventSubscriptions
    settings = get_settings()
    if not configured(settings):
        raise ValueError("dot is not configured; no command was executed")
    owner = settings.dot_owner_subject
    def enabled():
        current = get_settings()
        return (configured(current) and current.ai_provider == "dot"
                and current.dot_owner_subject == owner)
    event_path = os.path.join(DATA_DIR, "dot_events.db")
    try:
        fd = os.open(event_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except FileExistsError:
        pass
    info = os.stat(event_path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("dot subscription storage must be an owner-only regular file")
    events = EventSubscriptions(event_path, authorized=lambda p: enabled() and p == owner)
    return DotBridge(db.DB_PATH, principal=owner, enabled=enabled,
                     resolve_actor=db.resolve_actor, execute=execute_shopping, events=events)


def submit(actor, text, *, has_quote=False, has_media=False):
    if has_quote or has_media:
        return "The dot experiment currently accepts direct text for the shared shopping list only; no action was taken."
    try:
        receipt = get_bridge().enqueue(actor, text)
    except (ValueError, PermissionError):
        return "Dot is not ready for this request. No new action was taken. Check the dot connection/status, or switch AI brain back to your previous provider."
    if receipt['state'] != 'PENDING':
        return "This dot request was already handled or expired; I did not repeat it."
    return "Sent to dot for the shared-shopping test. This is pending, not confirmation that an action completed."


def write_status(state, *, subscription=False, tunnel=False):
    data = {"state": state, "subscription_verified": subscription,
            "tunnel_ready": tunnel, "updated_at": time.time()}
    path = os.path.join(DATA_DIR, "dot_status.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(path + ".tmp", path)


def tunnel_command(settings):
    if not re.fullmatch(r"tunnel_[0-9a-f]{32}", settings.dot_tunnel_id):
        raise ValueError("valid private tunnel ID required")
    key_path = os.path.join(DATA_DIR, "dot-tunnel-api-key")
    info = os.stat(key_path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise ValueError("tunnel runtime key must be an owner-only regular file")
    return ["/usr/local/bin/tunnel-client-runtime", "run",
            "--control-plane.tunnel-id=" + settings.dot_tunnel_id,
            "--control-plane.api-key=file:" + key_path,
            "--mcp.server-url=http://127.0.0.1:5002/mcp/",
            "--mcp.extra-headers=X-Alex-Bridge-Key: file:" + os.path.join(DATA_DIR, "dot-bridge-token"),
            "--health.listen-addr=127.0.0.1:5003"]


def main():
    # Dormant by default; never binds a public/LAN socket or uses proxy headers.
    while not configured(get_settings()):
        write_status("disabled")
        time.sleep(10)
    settings = get_settings()
    bridge = get_bridge()
    from dot_http import create_app, create_tunnel_app
    tunnel = None
    if settings.dot_transport == 'private_tunnel':
        app = create_tunnel_app(bridge, credential_path=os.path.join(DATA_DIR,'dot-bridge-token'),
                                owner_subject=settings.dot_owner_subject)
        # Only runtime permissions; no administration/provisioning command. No
        # key is in argv, stdout, status JSON, or inherited HA secret environment.
        tunnel = subprocess.Popen(tunnel_command(settings),
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": DATA_DIR},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        app = create_app(bridge,issuer=settings.dot_oauth_issuer,
                         jwks_url=settings.dot_oauth_jwks_url,
                         resource=settings.dot_resource_url,
                         owner_subject=settings.dot_owner_subject)
    stop = threading.Event()
    def drain():
        while not stop.is_set():
            try:
                bridge.publish_pending()
                subscribed = bridge.ready()
                tunnel_ready = False
                if tunnel and tunnel.poll() is None:
                    try:
                        with urllib.request.urlopen("http://127.0.0.1:5003/readyz",timeout=2) as response:
                            tunnel_ready = response.status == 200
                    except OSError:
                        pass
                state = "subscription_verified" if subscribed else "waiting_for_subscription"
                if tunnel and not tunnel_ready:
                    state = "tunnel_not_ready"
                write_status(state, subscription=subscribed, tunnel=tunnel_ready)
            except Exception:
                write_status("disabled_or_connection_error")
            stop.wait(5)
    threading.Thread(target=drain,daemon=True).start()
    import uvicorn
    try:
        uvicorn.run(app,host='127.0.0.1',port=5002,proxy_headers=False,access_log=False)
    finally:
        stop.set()
        if tunnel:
            tunnel.terminate()
            try:
                tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tunnel.kill()
        write_status("stopped")

if __name__=='__main__':
    try:
        main()
    except Exception:
        write_status("setup_required")
        print("[Alex dot] Setup required; see DOT_EXPERIMENT.md. No fallback was invoked.",flush=True)
        time.sleep(30)
