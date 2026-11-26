"""Delivery of emailed sign-in links (companies without SSO).

CAPTAIN_SMTP_HOST / _PORT / _FROM / _USER / _PASSWORD   real email (STARTTLS)
CAPTAIN_DEV_MAIL_DIR                                     development: write each email to a file
Neither set = email sign-in is off."""

import smtplib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Protocol


class SignInMailer(Protocol):
    def send(self, to: str, link: str, minutes: int) -> None: ...


def _message(sender: str, to: str, link: str, minutes: int) -> EmailMessage:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, to, "Your Captain sign-in link"
    m.set_content(
        "Someone (hopefully you) asked to sign in to Captain with this email address.\n\n"
        f"Sign in: {link}\n\n"
        f"The link works once and expires in {minutes} minutes. If you did not ask for it, "
        "ignore this email: nobody can sign in without the link.\n",
        cte="7bit",  # the link stays readable and clickable as-is
    )
    return m


class SmtpSignInMailer:
    def __init__(
        self,
        host: str,
        port: int,
        sender: str,
        username: str | None = None,
        password: str | None = None,
        smtp_factory: Callable[[str, int], smtplib.SMTP] = smtplib.SMTP,
    ) -> None:
        self._host, self._port, self._sender = host, port, sender
        self._user, self._pw, self._factory = username, password, smtp_factory

    def send(self, to: str, link: str, minutes: int) -> None:
        with self._factory(self._host, self._port) as s:
            s.starttls()
            if self._user and self._pw:
                s.login(self._user, self._pw)
            s.send_message(_message(self._sender, to, link, minutes))


class FileSignInMailer:
    """Development only: each email becomes a .eml file in a folder."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def send(self, to: str, link: str, minutes: int) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        safe = "".join(c if c.isalnum() or c in "@.-" else "_" for c in to)
        msg = _message("captain@localhost", to, link, minutes)
        (self.folder / f"{stamp}-{safe}.eml").write_bytes(msg.as_bytes())


def mailer_from_env(env: Mapping[str, str]) -> SignInMailer | None:
    if env.get("CAPTAIN_DEV_MAIL_DIR"):
        return FileSignInMailer(Path(env["CAPTAIN_DEV_MAIL_DIR"]))
    if env.get("CAPTAIN_SMTP_HOST"):
        return SmtpSignInMailer(
            env["CAPTAIN_SMTP_HOST"],
            int(env.get("CAPTAIN_SMTP_PORT") or "587"),
            env.get("CAPTAIN_SMTP_FROM") or "captain@localhost",
            env.get("CAPTAIN_SMTP_USER"),
            env.get("CAPTAIN_SMTP_PASSWORD"),
        )
    return None
