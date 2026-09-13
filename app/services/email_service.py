import asyncio
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from app.core.config import settings

logger = logging.getLogger("accqudo_email")

class EmailService:
    @staticmethod
    def _build_html_template(title: str, user_name: str, otp_code: str, action_desc: str) -> str:
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
          <meta charset="utf-8">
          <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background-color: #0b0f19; margin: 0; padding: 24px; color: #f8fafc; }}
            .card {{ max-width: 460px; margin: 0 auto; background: #131b2e; border: 1px solid #1e293b; border-radius: 16px; padding: 32px; }}
            .brand {{ font-size: 22px; font-weight: 900; color: #6366f1; letter-spacing: -0.5px; margin-bottom: 20px; display: inline-block; }}
            .h1 {{ font-size: 18px; font-weight: 700; color: #ffffff; margin-bottom: 12px; }}
            .text {{ font-size: 13px; color: #94a3b8; line-height: 1.6; margin-bottom: 24px; }}
            .otp-box {{ background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 18px; text-align: center; margin-bottom: 24px; }}
            .otp-val {{ font-family: monospace; font-size: 34px; font-weight: 900; letter-spacing: 8px; color: #38bdf8; }}
            .footer {{ font-size: 11px; color: #64748b; line-height: 1.5; border-top: 1px solid #1e293b; padding-top: 16px; }}
          </style>
        </head>
        <body>
          <div class="card">
            <span class="brand">accqudo</span>
            <div class="h1">{title}</div>
            <p class="text">Hello <b>{user_name}</b>,<br>{action_desc} Enter this one-time code to proceed. Valid for <b>5 minutes</b>.</p>
            <div class="otp-box">
              <span class="otp-val">{otp_code}</span>
            </div>
            <p class="text">If you did not request this code, you can safely ignore this email.</p>
            <div class="footer">Accqudo Assessment Platform - Automated Security Dispatch</div>
          </div>
        </body>
        </html>
        """

    @classmethod
    def _send_sync(cls, to_email: str, subject: str, html_body: str):
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
        msg["To"] = to_email
        msg.attach(MIMEText(html_body, "html"))

        if int(settings.SMTP_PORT) == 465:
            with smtplib.SMTP_SSL(settings.SMTP_HOST, int(settings.SMTP_PORT), timeout=15) as server:
                if settings.SMTP_USER and settings.SMTP_PASSWORD:
                    server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                server.sendmail(settings.SMTP_FROM_EMAIL, [to_email], msg.as_string())
        else:
            with smtplib.SMTP(settings.SMTP_HOST, int(settings.SMTP_PORT), timeout=15) as server:
                if settings.SMTP_TLS:
                    server.ehlo()
                    server.starttls()
                    server.ehlo()
                if settings.SMTP_USER and settings.SMTP_PASSWORD:
                    server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                server.sendmail(settings.SMTP_FROM_EMAIL, [to_email], msg.as_string())

    @classmethod
    async def send_otp_email(cls, email: str, user_name: str, otp: str, purpose: str = "registration"):
        if purpose == "registration":
            title = "Verify Your Accqudo Account"
            desc = "Thank you for creating an account on Accqudo."
        else:
            title = "Reset Your Account Password"
            desc = "We received a request to reset your password."

        # Always log to container console so you can copy-paste during development
        logger.info(f"\n[AUTH EMAIL OTP] -> {email} ({purpose}) | CODE: >>> {otp} <<<")

        if getattr(settings, "MAIL_SUPPRESS_SEND", False):
            logger.info("MAIL_SUPPRESS_SEND=True. Skipping actual SMTP dispatch.")
            return True

        html_content = cls._build_html_template(title, user_name, otp, desc)
        subject = f"{otp} is your Accqudo verification code"

        try:
            # Dispatch synchronously inside a separate worker thread
            await asyncio.to_thread(cls._send_sync, email, subject, html_content)
            logger.info(f"Successfully delivered OTP email to {email}")
            return True
        except Exception as e:
            logger.error(f"Failed to send email via SMTP to {email}: {str(e)}")
            return False