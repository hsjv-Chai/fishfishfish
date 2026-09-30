"""Interactive secret installer, run inside the one-shot login container."""
import getpass
import os
from pathlib import Path
import tempfile


def save_password(value, destination):
    password = "".join(value.split())
    if len(password) != 16 or not password.isascii() or not password.isalnum():
        raise ValueError("Enter the 16-character Google app password, not your account password.")
    path = Path(destination)
    fd, temp = tempfile.mkstemp(prefix=".smtp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(password + "\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def main():
    from fishbot.config import Config
    cfg = Config.load("/app/config.toml")
    if (cfg.smtp_host, cfg.smtp_port, cfg.smtp_security) != ("smtp.gmail.com", 465, "ssl"):
        raise SystemExit("Gmail SSL configuration does not match; check config.toml.")
    print(f"Gmail sender: {cfg.smtp_user}; alert recipient: {cfg.smtp_to}")
    try:
        save_password(getpass.getpass("Google app password (16 characters; hidden): "), cfg.smtp_password_file)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    print("App password saved on server with mode 600. Account password was not requested.")


if __name__ == "__main__":
    main()
