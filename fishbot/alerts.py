from email.message import EmailMessage
from pathlib import Path
import logging
import smtplib
import time

from .client import tls_context
from .config import private_file


class Alerts:
    def __init__(self, store, config):
        self.store, self.cfg = store, config

    def set(self, key, message, active=True, now=None):
        now = int(time.time()) if now is None else int(now)
        row = self.store.db.execute("SELECT active,sent,last_sent FROM alerts WHERE key=?", (key,)).fetchone()
        changed = row is None or bool(row[0]) != active
        if not active and row is None:
            return
        due = changed or (not row[1] and now - row[2] >= 60) or (active and now - row[2] >= 3600)
        if not due:
            return
        logging.warning("%s %s: %s", "ALERT" if active else "RECOVERED", key, message)
        sent = False
        if self.cfg.smtp_host and self.cfg.smtp_to:
            try:
                self.send(("[Fishbot] " if active else "[Fishbot recovered] ") + key, message)
                sent = True
            except Exception as exc:
                # Never include SMTP exception text: it can contain credentials or server echo.
                logging.error("email delivery failed: %s", type(exc).__name__)
        with self.store.db:
            self.store.db.execute('''INSERT INTO alerts VALUES (?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET active=excluded.active,sent=excluded.sent,
                last_sent=excluded.last_sent,message=excluded.message''',
                (key, int(active), int(sent), now, message))

    def send(self, subject, text):
        cfg = self.cfg
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, cfg.smtp_from, cfg.smtp_to
        msg.set_content(text)
        password = private_file(cfg.smtp_password_file).read_text().strip() if cfg.smtp_user else ""
        if cfg.smtp_security == "ssl":
            server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=15, context=tls_context())
        else:
            server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15)
            server.starttls(context=tls_context())
        with server:
            if cfg.smtp_user:
                server.login(cfg.smtp_user, password)
            server.send_message(msg)
