"""Feedback service: CRUD operations and email notification on admin reply."""
from __future__ import annotations

import logging
import os
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from api.database import FeedbackDB, UserDB

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers (reuse SMTP env vars from email_report_service)
# ---------------------------------------------------------------------------

def _get_env_alias(keys: list[str], default: str = "") -> str:
    for k in keys:
        v = os.getenv(k)
        if v is not None:
            return v
    return default


def _infer_frontend_url() -> str:
    explicit = os.getenv("FRONTEND_URL", "").strip()
    if explicit:
        return explicit
    raw = os.getenv("CORS_ALLOW_ORIGINS", "").strip()
    if not raw:
        return ""
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    for o in origins:
        if "localhost" not in o and "127.0.0.1" not in o:
            return o
    return origins[0] if origins else ""


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_feedback(db: Session, user: UserDB, subject: str, content: str) -> FeedbackDB:
    fb = FeedbackDB(
        id=str(uuid4()),
        user_id=user.id,
        user_email=user.email,
        subject=subject,
        content=content,
    )
    db.add(fb)
    db.commit()
    db.refresh(fb)
    return fb


def list_feedbacks(db: Session, user_id: str, page: int = 1, page_size: int = 20) -> tuple[list[FeedbackDB], int]:
    q = db.query(FeedbackDB).filter(FeedbackDB.user_id == user_id).order_by(FeedbackDB.created_at.desc())
    total = q.count()
    items = q.offset((page - 1) * page_size).limit(page_size).all()
    return items, total


def get_feedback(db: Session, feedback_id: str) -> Optional[FeedbackDB]:
    return db.query(FeedbackDB).filter(FeedbackDB.id == feedback_id).first()


def list_all_feedbacks(db: Session, page: int = 1, page_size: int = 20) -> tuple[list[FeedbackDB], int]:
    """Admin: list all feedbacks across users."""
    q = db.query(FeedbackDB).order_by(FeedbackDB.created_at.desc())
    total = q.count()
    items = q.offset((page - 1) * page_size).limit(page_size).all()
    return items, total


def admin_reply(db: Session, feedback_id: str, reply: str) -> Optional[FeedbackDB]:
    fb = db.query(FeedbackDB).filter(FeedbackDB.id == feedback_id).first()
    if not fb:
        return None
    fb.admin_reply = reply
    fb.replied_at = datetime.now(timezone.utc)
    fb.is_read = False  # mark unread so user sees the new reply
    db.commit()
    db.refresh(fb)
    return fb


def mark_read(db: Session, feedback_id: str, user_id: str) -> Optional[FeedbackDB]:
    fb = db.query(FeedbackDB).filter(FeedbackDB.id == feedback_id, FeedbackDB.user_id == user_id).first()
    if fb:
        fb.is_read = True
        db.commit()
        db.refresh(fb)
    return fb


def unread_count(db: Session, user_id: str) -> int:
    return db.query(FeedbackDB).filter(
        FeedbackDB.user_id == user_id,
        FeedbackDB.admin_reply.isnot(None),
        FeedbackDB.is_read == False,
    ).count()


# ---------------------------------------------------------------------------
# Email notification on admin reply
# ---------------------------------------------------------------------------

def _build_reply_email_html(feedback: FeedbackDB, frontend_url: str) -> str:
    link = f"{frontend_url.rstrip('/')}/feedback" if frontend_url else ""
    link_html = f'<p style="margin-top:20px;"><a href="{link}" style="color:#3b82f6;text-decoration:underline;">查看详情</a></p>' if link else ""
    return f"""\
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:600px;margin:0 auto;padding:24px;background:#ffffff;">
  <h2 style="color:#0f172a;font-size:18px;margin-bottom:16px;">您的反馈收到了回复</h2>
  <div style="background:#f8fafc;border-radius:8px;padding:16px;margin-bottom:16px;">
    <p style="color:#64748b;font-size:13px;margin:0 0 4px;">您的留言：</p>
    <p style="color:#334155;font-size:14px;margin:0;"><strong>{feedback.subject}</strong></p>
    <p style="color:#475569;font-size:13px;margin:8px 0 0;white-space:pre-wrap;">{feedback.content}</p>
  </div>
  <div style="background:#eff6ff;border-left:3px solid #3b82f6;border-radius:4px;padding:16px;margin-bottom:16px;">
    <p style="color:#1e40af;font-size:13px;margin:0 0 4px;font-weight:600;">管理员回复：</p>
    <p style="color:#1e3a5f;font-size:14px;margin:0;white-space:pre-wrap;">{feedback.admin_reply}</p>
  </div>
  {link_html}
  <hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0 12px;">
  <p style="color:#94a3b8;font-size:12px;margin:0;">TradingAgents 多智能体投研系统</p>
</div>"""


def send_reply_notification(feedback: FeedbackDB) -> bool:
    """Send email to user notifying them of the admin reply. Never raises."""
    smtp_host = _get_env_alias(["MAIL_HOST", "MAIL_SERVER", "SMTP_HOST"]).strip()
    if not smtp_host:
        logger.info("[feedback_email] SMTP not configured, skipping")
        return False

    smtp_port = int(_get_env_alias(["MAIL_PORT", "SMTP_PORT"]) or "587")
    smtp_user = _get_env_alias(["MAIL_USER", "MAIL_USERNAME", "SMTP_USER"]).strip()
    smtp_password = _get_env_alias(["MAIL_PASS", "MAIL_PASSWORD", "SMTP_PASSWORD"]).strip()
    smtp_from = _get_env_alias(["MAIL_FROM", "SMTP_FROM"], smtp_user or "noreply@example.com").strip()

    smtp_starttls_str = _get_env_alias(["MAIL_STARTTLS", "SMTP_TLS"], "1").strip().lower()
    smtp_starttls = smtp_starttls_str not in ("0", "false", "off", "no")

    smtp_ssl_tls_str = _get_env_alias(["MAIL_SSL", "MAIL_SSL_TLS"], "0").strip().lower()
    smtp_ssl_tls = smtp_ssl_tls_str in ("1", "true", "on", "yes")

    frontend_url = _infer_frontend_url()
    html_body = _build_reply_email_html(feedback, frontend_url)

    msg = EmailMessage()
    msg["Subject"] = f"TradingAgents - 您的反馈已收到回复: {feedback.subject[:50]}"
    msg["From"] = smtp_from
    msg["To"] = feedback.user_email

    plain = f"您的反馈「{feedback.subject}」已收到管理员回复：\n\n{feedback.admin_reply}\n\n请登录 TradingAgents 查看详情。"
    msg.set_content(plain)
    msg.add_alternative(html_body, subtype="html")

    try:
        logger.info(f"[feedback_email] sending reply notification to {feedback.user_email}")
        smtp_cls = smtplib.SMTP_SSL if smtp_ssl_tls else smtplib.SMTP
        with smtp_cls(smtp_host, smtp_port, timeout=20) as server:
            if smtp_starttls and not smtp_ssl_tls:
                server.starttls()
            if smtp_user:
                server.login(smtp_user, smtp_password)
            server.send_message(msg)
        logger.info(f"[feedback_email] sent OK to {feedback.user_email}")
        return True
    except Exception as e:
        logger.error(f"[feedback_email] failed to send to {feedback.user_email}: {e}")
        return False
