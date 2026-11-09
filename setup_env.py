"""Create local credentials without printing them or replacing an existing file."""
import os
import secrets
from pathlib import Path


def main():
    content = Path(".env.example").read_text(encoding="utf-8")
    for placeholder in (
        "REPLACE_WITH_RANDOM_ADMIN_SECRET", "REPLACE_WITH_RANDOM_APP_SECRET",
        "REPLACE_WITH_RANDOM_API_SECRET_AT_LEAST_32_CHARACTERS",
    ):
        content = content.replace(placeholder, secrets.token_hex(32))
    descriptor = os.open(".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    print("Created .env with random credentials. Keep it private.")


if __name__ == "__main__":
    main()
