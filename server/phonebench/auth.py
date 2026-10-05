"""Who is calling: the website's signed-in users (cloud mode).

The website signs people in with Supabase Auth and sends the session's access
token. The server asks Supabase who the token belongs to, then checks the
email against the `members` table, so only people added there get in.
"""

import threading
import time

import httpx

from .storage import UpstreamError

CACHE_SECONDS = 60
MAX_CACHED = 500
# Supabase's names for the sign-in methods we accept; add "azure" for Microsoft.
PROVIDERS = ("google",)


class AuthError(Exception):
    pass


class SupabaseAuth:
    def __init__(self, url: str, key: str, client: httpx.Client | None = None, providers: tuple[str, ...] = PROVIDERS):
        self.user_url = f"{url}/auth/v1/user"
        self.key = key
        self.providers = providers
        self.client = client or httpx.Client(timeout=15)
        self._cache: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def email_for(self, token: str) -> str:
        """The verified email address behind an access token, or AuthError."""
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(token)
            if cached and cached[0] > now:
                return cached[1]
        response = self.client.get(self.user_url, headers={"apikey": self.key, "Authorization": f"Bearer {token}"})
        if response.status_code in (401, 403):
            raise AuthError("Your sign-in has expired; sign in again")
        if response.status_code >= 400:
            raise UpstreamError(f"Supabase Auth answered {response.status_code}")
        user = response.json()
        email = user.get("email")
        if not email or not user.get("email_confirmed_at"):
            raise AuthError("This account has no confirmed email address")
        # Only accounts signed in through an allowed provider count. Otherwise
        # anyone could sign up with a member's address and a password if email
        # confirmation were ever turned off in Supabase.
        providers = (user.get("app_metadata") or {}).get("providers") or []
        if not set(providers) & set(self.providers):
            raise AuthError("Sign in with " + " or ".join(name.title() for name in self.providers))
        with self._lock:
            if len(self._cache) >= MAX_CACHED:
                self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
                if len(self._cache) >= MAX_CACHED:
                    self._cache.clear()
            self._cache[token] = (now + CACHE_SECONDS, email)
        return email
