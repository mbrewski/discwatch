"""Send Discwatch alerts over SMTP (iCloud app-specific password)."""
from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

log = logging.getLogger("discwatch.mail")

ENV_FILE = Path(__file__).resolve().parent / "data" / "smtp.env"
APPLE_ENV = Path.home() / "secrets" / "apple.env"
VAULT_CONFIG = Path.home() / ".config" / "vault"


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _vault_kv(path: str) -> dict:
    cfg = _parse_env_file(VAULT_CONFIG / "config")
    addr = os.environ.get("VAULT_ADDR") or cfg.get("VAULT_ADDR")
    if not addr:
        return {}
    role_id = (VAULT_CONFIG / "role-id").read_text().strip()
    secret_id = (VAULT_CONFIG / "secret-id").read_text().strip()
    import urllib.request

    ctx = ssl._create_unverified_context()
    req = urllib.request.Request(
        f"{addr}/v1/auth/approle/login",
        data=json.dumps({"role_id": role_id, "secret_id": secret_id}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, context=ctx, timeout=20) as r:
        token = json.load(r).get("auth", {}).get("client_token")
    if not token:
        return {}
    req = urllib.request.Request(
        f"{addr}/v1/secret/data/{path}",
        headers={"X-Vault-Token": token},
    )
    with urllib.request.urlopen(req, context=ctx, timeout=20) as r:
        body = json.load(r)
    return (body.get("data") or {}).get("data") or {}


def smtp_settings() -> dict | None:
    file_env = _parse_env_file(ENV_FILE)
    apple_env = _parse_env_file(APPLE_ENV)
    host = os.environ.get("SMTP_HOST") or file_env.get("SMTP_HOST") or "smtp.mail.me.com"
    port = int(os.environ.get("SMTP_PORT") or file_env.get("SMTP_PORT") or "587")
    user = os.environ.get("SMTP_USER") or file_env.get("SMTP_USER")
    password = (
        os.environ.get("SMTP_PASSWORD")
        or file_env.get("SMTP_PASSWORD")
        or apple_env.get("PASSWORD")
    )
    from_addr = os.environ.get("SMTP_FROM") or file_env.get("SMTP_FROM") or user
    if not user or not password:
        try:
            apple = _vault_kv("apple")
            disc = {}
            try:
                disc = _vault_kv("discwatch")
            except Exception:
                disc = {}
            user = user or disc.get("smtp_user") or disc.get("SMTP_USER")
            password = password or apple.get("PASSWORD")
            from_addr = from_addr or disc.get("smtp_from") or user
        except Exception as e:
            log.warning("vault smtp lookup failed: %s", e)
            return None
    if not user or not password:
        return None
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "from_addr": from_addr,
    }


def send_mail(
    to_addrs: list[str],
    subject: str,
    body: str,
    cc_addrs: list[str] | None = None,
) -> None:
    cfg = smtp_settings()
    if not cfg:
        raise RuntimeError(
            "SMTP is not configured. Set SMTP_USER (Apple ID) and put the "
            "app-specific password in Vault secret/apple or data/smtp.env."
        )
    if not to_addrs:
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["from_addr"]
    msg["To"] = ", ".join(to_addrs)
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    msg.set_content(body)
    with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()
        smtp.login(cfg["user"], cfg["password"])
        smtp.send_message(msg)


def format_preorder_digest(items: list[dict]) -> tuple[str, str]:
    n = len(items)
    subject = (
        f"Discwatch: {n} new pre-order" if n == 1 else f"Discwatch: {n} new pre-orders"
    )
    lines = ["These titles just showed up as pre-orders:", ""]
    for it in items:
        title = it.get("title") or "Untitled"
        fmt = "4K" if it.get("fmt") == "4k" else "Blu-ray"
        date = it.get("release_date") or "date TBA"
        url = it.get("url") or ""
        lines.append(f"- {title} ({fmt}, {date})")
        if url:
            lines.append(f"  {url}")
    lines.append("")
    lines.append("https://discwatch.mattbrewer.me/inbox")
    return subject, "\n".join(lines)


def notify_preorders(to_addrs: list[str], items: list[dict]) -> int:
    if not to_addrs or not items:
        return 0
    subject, body = format_preorder_digest(items)
    send_mail(to_addrs, subject, body)
    log.info("pre-order alert sent to %s (%s titles)", ",".join(to_addrs), len(items))
    return len(items)
