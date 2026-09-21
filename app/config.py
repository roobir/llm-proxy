import os


def _env(name: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"missing required env var {name}")
    return val


def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    return int(val) if val else default


# --- Bootstrap gateway config ------------------------------------------
# All optional: the app no longer hard-requires a Cloudflare account.
# These exist only to *seed* the gateway config in Redis on first boot, for
# operators who prefer env vars/secrets over clicking through /admin/settings
# (and so the original Cloudflare-only deployment this app was built for
# keeps working unchanged). Once seeded, `/admin/settings` is the source of
# truth and these env vars are never read again -- see app/gateway.py.
CF_ACCOUNT_ID = _env("CF_ACCOUNT_ID")
CF_API_TOKEN = _env("CF_API_TOKEN")
CF_AIG_GATEWAY_ID = _env("CF_AIG_GATEWAY_ID")
CF_MODEL = _env("CF_MODEL", "@cf/qwen/qwen3.8-27b")

# Any other OpenAI-compatible provider can be bootstrapped directly instead
# of the Cloudflare-specific vars above -- same shape /admin/settings saves.
GATEWAY_NAME = _env("GATEWAY_NAME")
GATEWAY_BASE_URL = _env("GATEWAY_BASE_URL")
GATEWAY_API_KEY = _env("GATEWAY_API_KEY")
GATEWAY_EXTRA_HEADERS = _env("GATEWAY_EXTRA_HEADERS")  # "Header: value" per line
GATEWAY_DEFAULT_MODEL = _env("GATEWAY_DEFAULT_MODEL")

# Single-admin GUI login (bcrypt hash, not a plaintext password) -- matches
# this lab's established single-admin-homelab pattern (labber-pve-secrets).
DASHBOARD_USERNAME = _env("DASHBOARD_USERNAME", required=True)
DASHBOARD_PASSWORD_HASH = _env("DASHBOARD_PASSWORD_HASH", required=True)
SESSION_SECRET = _env("SESSION_SECRET", required=True)

# Sliding idle timeout (any admin-GUI request resets the clock) plus a hard
# absolute cap that activity can't extend -- so a forgotten-open tab signs
# itself out, but a stolen cookie still expires eventually either way.
SESSION_IDLE_MINUTES = _env_int("SESSION_IDLE_MINUTES", 30)
SESSION_ABSOLUTE_HOURS = _env_int("SESSION_ABSOLUTE_HOURS", 12)

# Failed /admin/login attempts, per source IP, before a temporary lockout.
LOGIN_MAX_ATTEMPTS = _env_int("LOGIN_MAX_ATTEMPTS", 5)
LOGIN_LOCKOUT_MINUTES = _env_int("LOGIN_LOCKOUT_MINUTES", 15)

# Shared state across replicas: in-flight counter (drives the dashboard's
# light-up indicator), last-used-model/usage, self-test result, the gateway
# config, and the per-client API key ACL. AOF-persisted so issued API keys
# and gateway settings survive a pod/container restart.
REDIS_URL = _env("REDIS_URL", "redis://redis:6379/0")

# Kept short and cheap on purpose -- the self-test is a real, logged/billed
# request against whatever gateway is configured, not something to run on
# every check.
SELFTEST_PROMPT = _env("SELFTEST_PROMPT", "Reply with the single word: ok")
