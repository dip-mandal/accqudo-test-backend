import logging
from html import escape

import httpx

from app.core.config import settings


logger = logging.getLogger("accqudo_email")


class EmailService:

    @staticmethod
    def _build_html_template(
        title: str,
        user_name: str,
        otp_code: str,
        action_desc: str,
    ) -> str:
        """
        Build the HTML email used for registration and password-reset OTPs.
        """

        safe_user_name = escape(user_name or "Student")
        safe_otp = escape(otp_code)
        safe_title = escape(title)
        safe_action_desc = escape(action_desc)

        return f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">

    <style>
        body {{
            font-family:
                -apple-system,
                BlinkMacSystemFont,
                'Segoe UI',
                Roboto,
                sans-serif;
            background-color: #0b0f19;
            margin: 0;
            padding: 24px;
            color: #f8fafc;
        }}

        .card {{
            max-width: 460px;
            margin: 0 auto;
            background: #131b2e;
            border: 1px solid #1e293b;
            border-radius: 16px;
            padding: 32px;
        }}

        .brand {{
            font-size: 22px;
            font-weight: 900;
            color: #6366f1;
            letter-spacing: -0.5px;
            margin-bottom: 20px;
            display: inline-block;
        }}

        .h1 {{
            font-size: 18px;
            font-weight: 700;
            color: #ffffff;
            margin-bottom: 12px;
        }}

        .text {{
            font-size: 13px;
            color: #94a3b8;
            line-height: 1.6;
            margin-bottom: 24px;
        }}

        .otp-box {{
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 12px;
            padding: 18px;
            text-align: center;
            margin-bottom: 24px;
        }}

        .otp-val {{
            font-family: monospace;
            font-size: 34px;
            font-weight: 900;
            letter-spacing: 8px;
            color: #38bdf8;
        }}

        .footer {{
            font-size: 11px;
            color: #64748b;
            line-height: 1.5;
            border-top: 1px solid #1e293b;
            padding-top: 16px;
        }}
    </style>
</head>

<body>
    <div class="card">

        <span class="brand">accqudo</span>

        <div class="h1">
            {safe_title}
        </div>

        <p class="text">
            Hello <b>{safe_user_name}</b>,<br>
            {safe_action_desc}
            Enter this one-time code to proceed.
            Valid for <b>5 minutes</b>.
        </p>

        <div class="otp-box">
            <span class="otp-val">{safe_otp}</span>
        </div>

        <p class="text">
            If you did not request this code,
            you can safely ignore this email.
        </p>

        <div class="footer">
            Accqudo Assessment Platform -
            Automated Security Dispatch
        </div>

    </div>
</body>
</html>
        """


    @classmethod
    async def _send_via_brevo(
        cls,
        to_email: str,
        subject: str,
        html_body: str,
        user_name: str,
    ) -> bool:
        """
        Send email through Brevo's HTTPS API.

        This replaces SMTP so the service works on Render Free,
        where outbound SMTP ports are blocked.
        """

        api_key = getattr(settings, "BREVO_API_KEY", None)

        sender_email = getattr(
            settings,
            "BREVO_FROM_EMAIL",
            None,
        )

        sender_name = getattr(
            settings,
            "BREVO_FROM_NAME",
            "Accqudo Security",
        )

        if not api_key:
            logger.error(
                "BREVO_API_KEY is not configured."
            )
            return False

        if not sender_email:
            logger.error(
                "BREVO_FROM_EMAIL is not configured."
            )
            return False

        payload = {
            "sender": {
                "name": sender_name,
                "email": sender_email,
            },
            "to": [
                {
                    "email": to_email,
                    "name": user_name or "Student",
                }
            ],
            "subject": subject,
            "htmlContent": html_body,
        }

        headers = {
            "accept": "application/json",
            "api-key": api_key,
            "content-type": "application/json",
        }

        try:
            async with httpx.AsyncClient(
                timeout=15.0
            ) as client:

                response = await client.post(
                    "https://api.brevo.com/v3/smtp/email",
                    headers=headers,
                    json=payload,
                )

            if 200 <= response.status_code < 300:
                try:
                    response_data = response.json()
                except Exception:
                    response_data = {}

                message_id = response_data.get(
                    "messageId"
                )

                if message_id:
                    logger.info(
                        "Successfully submitted OTP email to %s "
                        "via Brevo. messageId=%s",
                        to_email,
                        message_id,
                    )
                else:
                    logger.info(
                        "Successfully submitted OTP email to %s "
                        "via Brevo.",
                        to_email,
                    )

                return True

            logger.error(
                "Brevo email API failed for %s. "
                "HTTP %s: %s",
                to_email,
                response.status_code,
                response.text[:1000],
            )

            return False

        except httpx.TimeoutException:
            logger.error(
                "Brevo email API timed out while sending to %s.",
                to_email,
            )
            return False

        except httpx.RequestError as exc:
            logger.error(
                "Network error while contacting Brevo for %s: %s",
                to_email,
                str(exc),
            )
            return False

        except Exception as exc:
            logger.exception(
                "Unexpected error while sending email to %s: %s",
                to_email,
                str(exc),
            )
            return False


    @classmethod
    async def send_otp_email(
        cls,
        email: str,
        user_name: str,
        otp: str,
        purpose: str = "registration",
    ) -> bool:
        """
        Send an OTP email for registration or password reset.

        Returns:
            True  -> email was successfully submitted to Brevo
            False -> email could not be submitted
        """

        if purpose == "registration":
            title = "Verify Your Accqudo Account"
            desc = (
                "Thank you for creating an account on Accqudo."
            )
        else:
            title = "Reset Your Account Password"
            desc = (
                "We received a request to reset your password."
            )

        # Development logging.
        #
        # IMPORTANT:
        # Keep this only if you are comfortable with OTPs appearing
        # in Render logs. For production security, you may remove it.
        logger.info(
            "[AUTH EMAIL OTP] -> %s (%s) | CODE: %s",
            email,
            purpose,
            otp,
        )

        # Optional development/testing switch.
        if getattr(
            settings,
            "MAIL_SUPPRESS_SEND",
            False,
        ):
            logger.info(
                "MAIL_SUPPRESS_SEND=True. "
                "Skipping actual email dispatch."
            )
            return True

        html_content = cls._build_html_template(
            title=title,
            user_name=user_name,
            otp_code=otp,
            action_desc=desc,
        )

        subject = (
            f"{otp} is your Accqudo verification code"
        )

        return await cls._send_via_brevo(
            to_email=email,
            subject=subject,
            html_body=html_content,
            user_name=user_name,
        )