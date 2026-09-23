"""Outgoing email over SMTP."""
import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

from . import config


def configured() -> bool:
    return bool(config.get("smtp_host") and config.get("smtp_from"))


def send(to: str, subject: str, text: str, html: str | None = None, headers: dict | None = None) -> None:
    msg = EmailMessage()
    msg["From"] = config.get("smtp_from")
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=config.get("smtp_from").split("@")[-1].strip("> "))
    for key, value in (headers or {}).items():
        msg[key] = value
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    host, port, security = config.get("smtp_host"), config.get("smtp_port"), config.get("smtp_security")
    context = ssl.create_default_context()
    if security == "ssl":
        server = smtplib.SMTP_SSL(host, port, context=context, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
    with server:
        if security == "starttls":
            server.starttls(context=context)
        if config.get("smtp_user"):
            server.login(config.get("smtp_user"), config.get("smtp_password"))
        server.send_message(msg)


async def send_async(*args, **kwargs) -> None:
    await asyncio.to_thread(send, *args, **kwargs)
