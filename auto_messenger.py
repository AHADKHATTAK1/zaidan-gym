import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from twilio.rest import Client
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# Load .env if present
load_dotenv()

# --- EMAIL CONFIG ---
EMAIL_ADDRESS = os.getenv("GMAIL_EMAIL")
EMAIL_PASSWORD = os.getenv("GMAIL_PASSWORD")

# --- WHATSAPP CONFIG ---
TWILIO_SID = os.getenv("TWILIO_SID")
TWILIO_AUTH = os.getenv("TWILIO_AUTH")
TWILIO_WHATSAPP = os.getenv("TWILIO_WHATSAPP", "whatsapp:+14155238886")

client = Client(TWILIO_SID, TWILIO_AUTH) if (TWILIO_SID and TWILIO_AUTH) else None

app = Flask(__name__)

# ==============================
# SEND EMAIL FUNCTION
# ==============================
def send_email(to_email: str, subject: str, message: str):
    if not (EMAIL_ADDRESS and EMAIL_PASSWORD):
        raise RuntimeError("Gmail credentials not configured. Set GMAIL_EMAIL and GMAIL_PASSWORD (App Password).")
    msg = MIMEMultipart()
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = to_email
    msg["Subject"] = subject

    msg.attach(MIMEText(message, "plain"))

    server = smtplib.SMTP("smtp.gmail.com", 587)
    server.starttls()
    server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
    server.sendmail(EMAIL_ADDRESS, to_email, msg.as_string())
    server.quit()

# ==============================
# SEND WHATSAPP FUNCTION
# ==============================

def send_whatsapp(to_number: str, message: str):
    if not client:
        raise RuntimeError("Twilio client not configured. Set TWILIO_SID and TWILIO_AUTH.")
    client.messages.create(
        body=message,
        from_=TWILIO_WHATSAPP,
        to=f"whatsapp:{to_number}"
    )

# ==============================
# FLASK API — AUTO SEND
# ==============================
@app.route("/send", methods=["POST"])
def send_message():
    data = request.get_json(silent=True) or {}
    # Inputs
    name = data.get("name") or "Member"
    single_email = data.get("email")
    email_list = data.get("emails") if isinstance(data.get("emails"), list) else []
    single_whatsapp = data.get("whatsapp")
    whatsapp_list = data.get("whatsapps") if isinstance(data.get("whatsapps"), list) else []
    subject = data.get("subject") or "Welcome to Zaidan Fitness Gym"
    provided_message = data.get("message")
    dry_run = bool(data.get("dry_run"))

    # Build final message (allow custom override)
    default_template = f"""
Assalam o Alaikum {name} 👋
Welcome to Zaidan Fitness Gym! 💪🔥

Your registration is completed.
If you have any questions, feel free to ask.

Regards,
Zaidan Fitness Gym
"""
    final_message = provided_message.strip() if isinstance(provided_message, str) and provided_message.strip() else default_template

    # Merge recipients
    all_emails = []
    if single_email:
        all_emails.append(single_email)
    all_emails.extend(email_list)

    all_whatsapp = []
    if single_whatsapp:
        all_whatsapp.append(single_whatsapp)
    all_whatsapp.extend(whatsapp_list)

    if not all_emails and not all_whatsapp:
        return jsonify({"ok": False, "error": "Provide at least one recipient via email/whatsapp/emails/whatsapps"}), 400

    results = {"email": [], "whatsapp": []}

    # Dry run support (just report what would happen)
    if dry_run:
        return jsonify({
            "ok": True,
            "dry_run": True,
            "subject": subject,
            "message_preview": final_message[:160],
            "targets": {"emails": all_emails, "whatsapps": all_whatsapp}
        }), 200

    # Send emails
    for em in all_emails:
        try:
            send_email(em, subject, final_message)
            results["email"].append({"to": em, "ok": True})
        except Exception as e:
            results["email"].append({"to": em, "ok": False, "error": str(e)})

    # Send WhatsApp messages
    for wa in all_whatsapp:
        try:
            # Accept both raw number or already prefixed whatsapp:+
            wa_clean = wa.replace("whatsapp:", "") if isinstance(wa, str) else wa
            send_whatsapp(wa_clean, final_message)
            results["whatsapp"].append({"to": wa_clean, "ok": True})
        except Exception as e:
            results["whatsapp"].append({"to": wa, "ok": False, "error": str(e)})

    return jsonify({"ok": True, "results": results}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(debug=True, port=port)
