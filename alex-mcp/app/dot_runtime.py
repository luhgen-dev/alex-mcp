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
import platform
from contextlib import contextmanager
import threading
import time

from config import DATA_DIR, get_settings


class StartupFailure(ValueError):
    def __init__(self, code, error_type):
        self.code, self.error_type = code, error_type
        super().__init__(code)


def checked(code, function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except StartupFailure:
        raise
    except Exception as exc:
        # Never retain or serialize exception messages: they may contain secrets.
        safe_types = {"ValueError", "PermissionError", "FileNotFoundError", "ImportError",
                      "ModuleNotFoundError", "OSError", "OperationalError", "TypeError",
                      "RuntimeError", "AttributeError"}
        name = type(exc).__name__
        raise StartupFailure(code, name if name in safe_types else "Exception") from None


PRIVATE_HINTS = {
    "DOT_ID_TYPE": "Saved tunnel ID is not text. Check HA Configuration and Save.",
    "DOT_ID_FORMAT": "Saved tunnel ID must be tunnel_ followed by 32 lowercase hexadecimal characters.",
    "DOT_KEY_TYPE": "Saved runtime key is not text. Check the masked HA field and Save.",
    "DOT_KEY_EMPTY": "Saved runtime key is empty. Check the masked HA field and Save.",
    "DOT_KEY_NONASCII": "Saved runtime key contains non-ASCII characters. Re-copy the secret locally; never send it in chat.",
    "DOT_KEY_LENGTH": "Saved runtime key length is outside the supported range. Verify the full secret was saved locally.",
    "DOT_KEY_WHITESPACE": "Saved runtime key contains whitespace. Remove accidental spaces or line breaks locally.",
    "DOT_DATA_DIRECTORY": "Could not prepare the private data directory. Report this code; do not loosen permissions.",
    "DOT_BRIDGE_CREATE": "Could not create the private bridge credential. Report this code; do not delete existing credentials.",
    "DOT_BRIDGE_OPEN": "Could not open the bridge credential safely. Report this code; do not follow links or loosen permissions.",
    "DOT_BRIDGE_STAT": "Could not inspect the private bridge credential. Report this code.",
    "DOT_BRIDGE_TYPE": "Bridge credential is not a regular file. Report this code; do not replace it manually.",
    "DOT_BRIDGE_MODE": "Bridge credential permissions are not private. Report this code; do not make permissions broader.",
    "DOT_BRIDGE_OWNER": "Bridge credential owner differs from the service. Report this code; do not change ownership blindly.",
    "DOT_BRIDGE_CONTENT": "Existing bridge credential is malformed. Report this code; do not paste or delete it.",
    "DOT_BRIDGE_READ": "Could not read the private bridge credential. Report this code without file contents.",
    "DOT_BRIDGE_CLOSE": "Could not close the private bridge credential. Report this code.",
    "DOT_RUNTIME_KEY_CREATE": "Could not create a private runtime-key staging file. Report this code.",
    "DOT_RUNTIME_KEY_WRITE": "Could not write the private runtime-key staging file. Report this code; do not share its contents.",
    "DOT_RUNTIME_KEY_INSTALL": "Could not install the private runtime-key file. Report this code.",
    "DOT_RUNTIME_KEY_CLEANUP": "Could not clean up a private staging file. Report this code; do not inspect its contents.",
}


@contextmanager
def private_stage(code):
    try:
        yield
    except StartupFailure:
        raise
    except Exception as exc:
        # Route through the same fixed-type sanitizer as other startup stages.
        checked(code, lambda: (_ for _ in ()).throw(exc))


def reject_private(code):
    raise StartupFailure(code, "ValueError")


def validate_runtime(path="/usr/local/bin/tunnel-client-runtime"):
    # Packaged release is Linux amd64 only. Detect packaging/host mismatch before
    # trying to start the daemon; never inspect the credential file here.
    if platform.system() != "Linux" or platform.machine().lower() not in ("x86_64", "amd64"):
        raise StartupFailure("DOT_RUNTIME_ARCH", "RuntimeError")
    if not os.path.isfile(path):
        raise StartupFailure("DOT_RUNTIME_MISSING", "FileNotFoundError")
    if not os.access(path, os.X_OK):
        raise StartupFailure("DOT_RUNTIME_PERMISSION", "PermissionError")
    with open(path, "rb") as f:
        header = f.read(20)
    if len(header) < 20 or header[:6] != b"\x7fELF\x02\x01" or int.from_bytes(header[18:20], "little") != 62:
        raise StartupFailure("DOT_RUNTIME_FORMAT", "RuntimeError")


def owner_binding(settings):
    if settings.dot_transport == "private_tunnel":
        return "private-tunnel:" + settings.dot_tunnel_id if isinstance(settings.dot_tunnel_id, str) and settings.dot_tunnel_id else ""
    return settings.dot_owner_subject


def configured(settings):
    return bool(settings.dot_enabled is True and settings.ai_provider == "dot"
                and owner_binding(settings)
                and settings.dot_transport in ("private_tunnel", "oauth"))


def private_opt_in(settings):
    # Permit the startup validator to diagnose missing/malformed fields after
    # explicit enablement, without weakening get_bridge's configured gate.
    return (settings.dot_enabled is True and settings.ai_provider == "dot"
            and settings.dot_transport == "private_tunnel")


def prepare_private_tunnel(settings, *, data_dir=None):
    """Opt-in provisioning from masked HA options; never returns a secret.

    No key generation, directories, reads or writes while disabled. Caller must
    select dot AND private_tunnel AND explicitly enable the connection first.
    """
    if not private_opt_in(settings):
        return {"prepared": False, "generated_bridge_token": False}
    if not isinstance(settings.dot_tunnel_id, str):
        reject_private("DOT_ID_TYPE")
    if not re.fullmatch(r"tunnel_[0-9a-f]{32}", settings.dot_tunnel_id):
        reject_private("DOT_ID_FORMAT")
    key = settings.dot_tunnel_runtime_key
    if not isinstance(key, str):
        reject_private("DOT_KEY_TYPE")
    if not key:
        reject_private("DOT_KEY_EMPTY")
    if not key.isascii():
        reject_private("DOT_KEY_NONASCII")
    if any(c.isspace() for c in key):
        reject_private("DOT_KEY_WHITESPACE")
    if not 20 <= len(key) <= 4096:
        reject_private("DOT_KEY_LENGTH")
    directory = data_dir or DATA_DIR
    with private_stage("DOT_DATA_DIRECTORY"):
        os.makedirs(directory, mode=0o700, exist_ok=True)
    token_path = os.path.join(directory, "dot-bridge-token")
    generated = False
    with private_stage("DOT_BRIDGE_CREATE"):
        # Install a complete token atomically, retaining the no-follow checks.
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
    with private_stage("DOT_BRIDGE_OPEN"):
        fd = os.open(token_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        with private_stage("DOT_BRIDGE_STAT"):
            info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            reject_private("DOT_BRIDGE_TYPE")
        if info.st_mode & 0o077:
            reject_private("DOT_BRIDGE_MODE")
        if info.st_uid != os.getuid():
            reject_private("DOT_BRIDGE_OWNER")
        with private_stage("DOT_BRIDGE_READ"):
            raw = os.read(fd, 4097)
        try:
            credential = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            reject_private("DOT_BRIDGE_CONTENT")
        # Same format requirement as private auth, checked here to distinguish
        # persisted-file corruption from a masked configuration problem.
        if (not credential.isascii() or not 43 <= len(credential) <= 4096
                or any(c.isspace() for c in credential)):
            reject_private("DOT_BRIDGE_CONTENT")
    finally:
        with private_stage("DOT_BRIDGE_CLOSE"):
            os.close(fd)
    with private_stage("DOT_RUNTIME_KEY_CREATE"):
        fd, tmp = tempfile.mkstemp(prefix=".dot-runtime-", dir=directory)
    try:
        with private_stage("DOT_RUNTIME_KEY_WRITE"):
            with os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(key)
                f.flush()
                os.fsync(f.fileno())
        with private_stage("DOT_RUNTIME_KEY_INSTALL"):
            os.replace(tmp, os.path.join(directory, "dot-tunnel-api-key"))
    finally:
        with private_stage("DOT_RUNTIME_KEY_CLEANUP"):
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


def write_status(state, *, subscription=False, tunnel=False, diagnostic=None):
    data = {"state": state, "subscription_verified": subscription,
            "tunnel_ready": tunnel, "updated_at": time.time()}
    if diagnostic:
        data["diagnostic"] = diagnostic
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
    while True:
        current = get_settings()
        if configured(current) or private_opt_in(current):
            break
        write_status("disabled")
        time.sleep(10)
    settings = get_settings()
    checked("DOT_PRIVATE_CONFIG", prepare_private_tunnel, settings)
    bridge = checked("DOT_LEDGER", get_bridge)
    http = checked("DOT_HTTP_IMPORT", __import__, "dot_http")
    create_app, create_tunnel_app = http.create_app, http.create_tunnel_app
    tunnel = None
    stop = threading.Event()
    try:
        if settings.dot_transport == 'private_tunnel':
            app = checked("DOT_PRIVATE_AUTH", create_tunnel_app, bridge, credential_path=os.path.join(DATA_DIR,'dot-bridge-token'),
                                    owner_subject=owner_binding(settings))
            # Only runtime permissions; no administration/provisioning command. No
            # key is in argv, stdout, status JSON, or inherited HA secret environment.
            checked("DOT_RUNTIME_CHECK", validate_runtime)
            command = checked("DOT_RUNTIME_CONFIG", tunnel_command, settings)
            tunnel = checked("DOT_RUNTIME_START", subprocess.Popen, command,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": DATA_DIR},
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            app = checked("DOT_OAUTH_CONFIG", create_app, bridge,issuer=settings.dot_oauth_issuer,
                             jwks_url=settings.dot_oauth_jwks_url,
                             resource=settings.dot_resource_url,
                             owner_subject=owner_binding(settings))
        uvicorn = checked("DOT_SERVER_IMPORT", __import__, "uvicorn")
        server = checked("DOT_SERVER_CONFIG", lambda: uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=5002,proxy_headers=False,access_log=False,timeout_graceful_shutdown=5)))
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
        checked("DOT_SERVER_RUN", server.run)
    finally:
        stop.set()
        if tunnel:
            tunnel.terminate()
            try:
                tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tunnel.kill()
        write_status("stopped")

def run_service():
    try:
        checked("DOT_STARTUP", main)
    except StartupFailure as exc:
        hint = PRIVATE_HINTS.get(exc.code, "See DOT_EXPERIMENT.md; report only this diagnostic code.")
        diagnostic = {"code": exc.code, "error_type": exc.error_type, "hint": hint}
        # Only fixed stage codes and allowlisted type names, never repr/str(exc).
        try:
            write_status("setup_required", diagnostic=diagnostic)
        except Exception:
            pass
        print(f"[Alex dot] Setup required [{exc.code}/{exc.error_type}]; "
              f"{hint} No fallback was invoked.", flush=True)
        time.sleep(30)


if __name__ == '__main__':
    run_service()
