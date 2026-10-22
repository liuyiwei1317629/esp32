import os
import threading
import time
import tomllib
from dataclasses import dataclass, field

import config

DEFAULT_MAX_TOKENS = 320
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_FIRST_TOKEN_TIMEOUT_S = 3.0
COOLDOWN_SECONDS = (5 * 60, 15 * 60, 30 * 60)


class BrainError(Exception):
    """A profile failed. retryable means the same profile deserves one retry."""

    def __init__(self, message, retryable=False, status=None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass(frozen=True)
class Profile:
    name: str
    base_url: str
    model: str
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout_s: float = DEFAULT_TIMEOUT_S
    first_token_timeout_s: float = DEFAULT_FIRST_TOKEN_TIMEOUT_S
    key_file: str = ""
    price_in_cny_per_mtok: float | None = None
    price_out_cny_per_mtok: float | None = None
    extra_body: dict = field(default_factory=dict)

    def key(self):
        if not self.key_file:
            return None
        if not os.path.isfile(self.key_file):
            raise BrainError("key file missing for profile %s" % self.name)
        with open(self.key_file, "r", encoding="utf-8") as fh:
            key = fh.read().strip()
        if not key:
            raise BrainError("empty key file for profile %s" % self.name)
        return key


@dataclass(frozen=True)
class BrainConfig:
    active: str
    fallback: tuple
    profiles: dict


def _price(raw, key, name):
    value = raw.get(key)
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise BrainError("profile %s %s must be a number" % (name, key)) from None
    if value < 0:
        raise BrainError("profile %s %s must not be negative" % (name, key))
    return value


def _resolve_path(value, base_dir):
    if not value:
        return ""
    value = os.path.expanduser(value)
    if not os.path.isabs(value):
        value = os.path.join(base_dir, value)
    return os.path.normpath(value)


def legacy_config():
    extra = {}
    if config.DEEPSEEK_THINKING:
        extra["thinking"] = {"type": config.DEEPSEEK_THINKING}
    profile = Profile(
        name="deepseek",
        base_url=config.DEEPSEEK_BASE_URL,
        model=config.DEEPSEEK_MODEL,
        max_tokens=config.DEEPSEEK_MAX_TOKENS,
        timeout_s=DEFAULT_TIMEOUT_S,
        key_file=config.DEEPSEEK_KEY_FILE,
        price_in_cny_per_mtok=config.DEEPSEEK_PRICE_IN_CNY_PER_MTOK,
        price_out_cny_per_mtok=config.DEEPSEEK_PRICE_OUT_CNY_PER_MTOK,
        extra_body=extra,
    )
    return BrainConfig(active="deepseek", fallback=(), profiles={"deepseek": profile})


def load_config(path=None):
    path = os.path.expanduser(path or config.BRAINS_CONFIG_FILE)
    if not os.path.isfile(path):
        return legacy_config()
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise BrainError("cannot read %s: %s" % (path, exc)) from exc

    brain = data.get("brain")
    if not isinstance(brain, dict):
        raise BrainError("%s has no [brain] table" % path)
    raw_profiles = brain.get("profiles")
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise BrainError("%s has no brain profiles" % path)

    base_dir = os.path.dirname(os.path.abspath(path))
    profiles = {}
    for name, raw in raw_profiles.items():
        if not isinstance(raw, dict):
            raise BrainError("profile %s is not a table" % name)
        base_url = str(raw.get("base_url") or "").strip()
        model = str(raw.get("model") or "").strip()
        if not base_url or not model:
            raise BrainError("profile %s needs base_url and model" % name)
        extra = raw.get("extra_body") or {}
        if not isinstance(extra, dict):
            raise BrainError("profile %s extra_body must be a table" % name)
        profiles[name] = Profile(
            name=name,
            base_url=base_url,
            model=model,
            max_tokens=int(raw.get("max_tokens", DEFAULT_MAX_TOKENS)),
            timeout_s=float(raw.get("timeout_s", DEFAULT_TIMEOUT_S)),
            first_token_timeout_s=float(
                raw.get("first_token_timeout_s", DEFAULT_FIRST_TOKEN_TIMEOUT_S)),
            key_file=_resolve_path(raw.get("key_file") or "", base_dir),
            price_in_cny_per_mtok=_price(raw, "price_in_cny_per_mtok", name),
            price_out_cny_per_mtok=_price(raw, "price_out_cny_per_mtok", name),
            extra_body=extra,
        )

    active = brain.get("active")
    if not active:
        if len(profiles) == 1:
            active = next(iter(profiles))
        else:
            raise BrainError("%s brain.active is required" % path)
    if active not in profiles:
        raise BrainError("brain.active %s has no profile" % active)

    fallback = brain.get("fallback") or []
    if isinstance(fallback, str):
        fallback = [fallback]
    fallback = tuple(fallback)
    for name in fallback:
        if name not in profiles:
            raise BrainError("brain.fallback %s has no profile" % name)
    return BrainConfig(active=active, fallback=fallback, profiles=profiles)


class BrainRouter:
    """Try the active profile, then fallbacks, with cooldown and one probe per failure."""

    def __init__(self, cfg, streamer, prober=None, clock=time.monotonic, sleep=time.sleep):
        self.cfg = cfg
        self.active = cfg.active
        self._streamer = streamer
        self._prober = prober
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.RLock()
        self._cooldowns = {}
        self._probe_threads = {}
        self._pinned = False
        self._stopped = False

    def _cooling_locked(self, name):
        state = self._cooldowns.get(name)
        return bool(state and state[0] > self._clock())

    def is_cooling(self, name):
        with self._lock:
            return self._cooling_locked(name)

    def status(self):
        with self._lock:
            now = self._clock()
            cooling = {name: round(state[0] - now, 1)
                       for name, state in self._cooldowns.items()
                       if state[0] > now}
            return {"active": self.active, "pinned": self._pinned, "cooling": cooling}

    def set_active(self, name):
        if name not in self.cfg.profiles:
            raise ValueError("unknown brain profile: %s" % name)
        with self._lock:
            self.active = name
            self._pinned = name != self.cfg.active
            self._cooldowns.pop(name, None)
        return name

    def _attempt_order(self):
        with self._lock:
            names = [self.active]
            names.extend(self.cfg.fallback)
            names.append(self.cfg.active)
            seen = set()
            order = []
            for name in names:
                if name in seen or name not in self.cfg.profiles:
                    continue
                seen.add(name)
                if self._cooling_locked(name):
                    continue
                order.append(self.cfg.profiles[name])
            return order

    def _record_success(self, name):
        with self._lock:
            self._cooldowns.pop(name, None)

    def _record_failure(self, name):
        with self._lock:
            previous = self._cooldowns.get(name)
            failures = (previous[1] if previous else 0) + 1
            seconds = COOLDOWN_SECONDS[min(failures, len(COOLDOWN_SECONDS)) - 1]
            self._cooldowns[name] = (self._clock() + seconds, failures)
            should_probe = (self._prober is not None and not self._pinned
                            and name == self.cfg.active and not self._stopped)
        if should_probe:
            self._schedule_probe(name)

    def _schedule_probe(self, name):
        with self._lock:
            thread = self._probe_threads.get(name)
            if thread is not None and thread.is_alive():
                return
            thread = threading.Thread(target=self._probe_loop, args=(name,), daemon=True,
                                      name="brain-probe-%s" % name)
            self._probe_threads[name] = thread
            thread.start()

    def _probe_loop(self, name):
        while not self._stopped:
            with self._lock:
                state = self._cooldowns.get(name)
                if not state or self._pinned:
                    return
                wait = state[0] - self._clock()
            if wait > 0:
                self._sleep(wait)
            if self.probe_now(name):
                return

    def probe_now(self, name):
        if name not in self.cfg.profiles:
            raise ValueError("unknown brain profile: %s" % name)
        if self._prober is None:
            return False
        try:
            healthy = bool(self._prober(self.cfg.profiles[name]))
        except Exception:
            healthy = False
        if healthy:
            self._record_success(name)
        else:
            self._record_failure(name)
        return healthy

    def stream_reply(self, messages, on_content, abort=None):
        with self._lock:
            primary = self.active
        order = self._attempt_order()
        if not order:
            raise BrainError("no usable brain profile: all are cooling down")

        attempts = 0
        emitted = False
        failures = []
        for prof in order:
            retries_left = 1
            while True:
                attempts += 1

                def forward(text):
                    nonlocal emitted
                    emitted = True
                    on_content(text)

                try:
                    stats = self._streamer(prof, messages, forward, abort)
                    stats = dict(stats or {})
                    stats["profile"] = prof.name
                    stats["fallback_used"] = prof.name != primary
                    stats["attempts"] = attempts
                    self._record_success(prof.name)
                    return stats
                except Exception as exc:
                    if abort is not None and abort.is_set():
                        raise
                    self._record_failure(prof.name)
                    failures.append("%s: %s" % (prof.name, exc))
                    if emitted:
                        raise
                    if getattr(exc, "retryable", False) and retries_left > 0:
                        retries_left -= 1
                        continue
                    break
        raise BrainError("all brain profiles failed: " + "; ".join(failures))
