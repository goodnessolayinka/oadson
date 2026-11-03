"""
Keeps secrets out of results. Any text in a result that contains a secret
value is replaced with *** before it leaves the worker.
"""
SENSITIVE_PREFIXES = ("CRED_",)
SENSITIVE_NAMES = ("INTERNAL_SECRET", "WORKER_STATE_KEY", "WORKER_MONGODB_URI", "MONGODB_URI")
MIN_LENGTH = 6  # shorter values match too much ordinary text to be useful


def secret_values(env) -> list:
    values = []
    for name, value in env.items():
        if (name.startswith(SENSITIVE_PREFIXES) or name in SENSITIVE_NAMES) \
                and value and len(value) >= MIN_LENGTH:
            values.append(value)
    return sorted(set(values), key=len, reverse=True)


def redact(obj, secrets: list):
    if not secrets:
        return obj
    if isinstance(obj, str):
        for s in secrets:
            obj = obj.replace(s, "***")
        return obj
    if isinstance(obj, dict):
        return {k: redact(v, secrets) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v, secrets) for v in obj]
    return obj
