"""Runtime composition for optional local dot transport. No model API calls."""
from __future__ import annotations

import os
import json
import re
import secrets
import tempfile
import subprocess
import urllib.request
import stat
import threading
import time

from config import DATA_DIR, get_settings


def owner_binding(settings):
    if settings.dot_transport == "private_tunnel":
        return "private-tunnel:" + settings.dot_tunnel_id if settings.dot_tunnel_id else ""
    return settings.dot_owner_subject


def configured(settings):
    return bool(settings.dot_enabled is True and settings.ai_provider == "dot"
                and owner_binding(settings)
                and settings.dot_transport in ("private_tunnel", "oauth"))


def prepare_private_tunnel(settings, *, data_dir=None):
    """Opt-in provisioning from masked HA options; never returns a secret.

    No key generation, directories, reads or writes while disabled. Caller must
    select dot AND private_tunnel AND explicitly enable the connection first.
    """
    if not configured(settings) or settings.dot_transport != "private_tunnel":
        return {"prepared": False, "generated_bridge_token": False}
    if not re.fullmatch(r"tunnel_[0-9a-f]{32}", settings.dot_tunnel_id):
        raise ValueError("valid private tunnel ID required")
    key = settings.dot_tunnel_runtime_key
    if not isinstance(key, str) or not key.isascii() or not 20 <= len(key) <= 4096 or any(c.isspace() for c in key):
        raise ValueError("approved restricted runtime key required in masked HA configuration")
    directory = data_dir or DATA_DIR
    os.makedirs(directory, mode=0o700, exist_ok=True)
    token_path = os.path.join(directory, "dot-bridge-token")
    generated = False
    # Install a complete token atomically; a crash must not leave a zero-byte
    # canonical credential file that requires shell recovery.
    if not os.path.lexists(token_path):
        fd, token_tmp = tempfile.mkstemp(prefix=".dot-token-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(secrets.token_urlsafe(32))
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(token_tmp, token_path, follow_symlinks=False)
                generated = True
            except FileExistsError:
                pass
        finally:
            os.unlink(token_tmp)
    fd = os.open(token_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise ValueError("bridge credential must remain an owner-only regular file")
    finally:
        os.close(fd)
    # Atomically replace only our derived runtime-key file, never a symlink's
    # target. The key remains in Supervisor's protected options as with existing
    # provider password fields; masking is not a claim of encryption at rest.
    fd, tmp = tempfile.mkstemp(prefix=".dot-runtime-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(key)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(directory, "dot-tunnel-api-key"))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return {"prepared": True, "generated_bridge_token": generated}


def get_bridge():
    import db
    from dot_bridge import DotBridge, execute_shopping
    from dot_events import EventSubscriptions
    settings = get_settings()
    if not configured(settings):
        raise ValueError("dot is not configured; no command was executed")
    owner = owner_binding(settings)
    def enabled():
        current = get_settings()
        return (configured(current) and current.ai_provider == "dot"
                and owner_binding(current) == owner)
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


def watch_configuration(settings, stop, shutdown, *, read_settings=None, interval=5):
    """Independent of webhook IO: disable cannot wait behind callback delivery."""
    read_settings = read_settings or get_settings
    while not stop.is_set():
        current = read_settings()
        if not configured(current) or current != settings:
            shutdown()
            return
        stop.wait(interval)


def main():
    # Dormant by default; never binds a public/LAN socket or uses proxy headers.
    while not configured(get_settings()):
        write_status("disabled")
        time.sleep(10)
    settings = get_settings()
    prepare_private_tunnel(settings)
    bridge = get_bridge()
    from dot_http import create_app, create_tunnel_app
    tunnel = None
    if settings.dot_transport == 'private_tunnel':
        app = create_tunnel_app(bridge, credential_path=os.path.join(DATA_DIR,'dot-bridge-token'),
                                owner_subject=owner_binding(settings))
        # Only runtime permissions; no administration/provisioning command. No
        # key is in argv, stdout, status JSON, or inherited HA secret environment.
        tunnel = subprocess.Popen(tunnel_command(settings),
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": DATA_DIR},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        app = create_app(bridge,issuer=settings.dot_oauth_issuer,
                         jwks_url=settings.dot_oauth_jwks_url,
                         resource=settings.dot_resource_url,
                         owner_subject=owner_binding(settings))
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=5002,proxy_headers=False,access_log=False,timeout_graceful_shutdown=5))
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
    def shutdown():
        server.should_exit = True
        if tunnel and tunnel.poll() is None:
            tunnel.terminate()
    threading.Thread(target=watch_configuration,
                     args=(settings, stop, shutdown),daemon=True).start()
    threading.Thread(target=drain,daemon=True).start()
    try:
        server.run()
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
