# WhatsApp & Email Setup Guide for ZAIDAN FITNESS RECORD

## ✅ Your Configuration

- **Email**: zaidanfitnessgym@gmail.com
- **WhatsApp Number**: +92 317 9880100

---

## 📧 Email Setup (SMTP)

Your email is already configured in `.env` file. You just need to add the Gmail App Password:

### Steps to Get Gmail App Password:

1. **Enable 2-Step Verification** on your Gmail account:

   - Go to: https://myaccount.google.com/security
   - Click "2-Step Verification" and turn it ON

2. **Generate App Password**:

   - Go to: https://myaccount.google.com/apppasswords
   - Select "Mail" and "Windows Computer"
   - Click "Generate"
   - Copy the 16-character password (example: `abcd efgh ijkl mnop`)

3. **Update .env file**:

   ```
   SMTP_PASSWORD=abcdefghijklmnop
   ```

   (Remove spaces from the app password)

4. **Test Email**:
   - Restart your Flask server
   - Go to Dashboard → Click "Email Backup" button
   - Check if backup email arrives at zaidanfitnessgym@gmail.com

---

## 📱 WhatsApp Business API Setup

WhatsApp requires Business API credentials. Here are your options:

### Option 1: WhatsApp Business Cloud API (FREE - Recommended)

1. **Create Meta Developer Account**:

   - Go to: https://developers.facebook.com/
   - Sign up with your Facebook account

2. **Create a Meta App**:

   - Click "My Apps" → "Create App"
   - Select "Business" type
   - Add app name: "ZAIDAN FITNESS GYM"

3. **Add WhatsApp Product**:

   - In your app dashboard, click "Add Product"
   - Select "WhatsApp" → "Set Up"

4. **Get Your Credentials**:

   - **Phone Number ID**: Found in WhatsApp → Getting Started
   - **Access Token**: Temporary token shown on the page (valid 24 hours)
   - For permanent token: Go to System Users → Generate Token

5. **Verify Your Number** (+92 317 9880100):

   - Follow the verification process in Meta Business
   - You'll receive a verification code via SMS

6. **Update .env file**:

   ```
   WHATSAPP_TOKEN=your_permanent_access_token_here
   WHATSAPP_PHONE_NUMBER_ID=your_phone_number_id_here
   ```

7. **Test WhatsApp**:
   - Restart Flask server
   - Go to Dashboard → WhatsApp Test section
   - Enter your number: +923179880100
   - Send test message

### Option 2: Twilio WhatsApp (Paid)

1. Go to: https://www.twilio.com/console
2. Sign up and get WhatsApp Sandbox credentials
3. Update code to use Twilio API (alternative implementation)

---

## 🔄 Auto Reminders Configuration

Already configured in `.env`:

```
SCHEDULE_REMINDERS_ENABLED=1
SCHEDULE_TIME_HH=9
SCHEDULE_TIME_MM=0
```

This will send automatic reminders daily at 9:00 AM to members with unpaid fees.

---

## 🧪 Testing Your Setup

### Test Email:

```powershell
# Make sure server is running
python app.py

# Open browser: http://localhost:5000/dashboard
# Click "Email Backup" button
# Check zaidanfitnessgym@gmail.com inbox
```

### Test WhatsApp:

```powershell
# After configuring WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID
# Restart server
python app.py

# Open browser: http://localhost:5000/dashboard
# Scroll to "WhatsApp Test" section
# Phone: +923179880100
# Message: "Test from ZAIDAN FITNESS"
# Click Send
```

---

## 📋 Current .env Status

✅ SMTP_HOST=smtp.gmail.com
✅ SMTP_PORT=587
✅ SMTP_USER=zaidanfitnessgym@gmail.com
✅ SMTP_TLS=1
✅ BACKUP_TO_EMAIL=zaidanfitnessgym@gmail.com
✅ WHATSAPP_DEFAULT_COUNTRY_CODE=92
⚠️ SMTP_PASSWORD= (NEEDS GMAIL APP PASSWORD)
⚠️ WHATSAPP_TOKEN= (NEEDS META BUSINESS TOKEN)
⚠️ WHATSAPP_PHONE_NUMBER_ID= (NEEDS META PHONE NUMBER ID)

---

## 🆘 Troubleshooting

### Email Not Working:

- Check if 2-Step Verification is enabled
- Verify App Password is correct (no spaces)
- Check SMTP_USER matches your Gmail
- Look at server logs for error messages

### WhatsApp Not Working:

- Verify WHATSAPP_TOKEN is valid (not expired)
- Check WHATSAPP_PHONE_NUMBER_ID is correct
- Ensure phone number is verified in Meta Business
- Check if recipient number is in correct format (+923179880100)

### Auto Reminders Not Sending:

- Check SCHEDULE_REMINDERS_ENABLED=1
- Verify both Email and WhatsApp are working
- Server must be running continuously for scheduler
- Check server logs for scheduler activity

---

## 📞 Support

If you need help:

1. Check server logs (terminal output)
2. Test each feature individually
3. Verify all credentials are correct in .env
4. Restart server after any .env changes

---

**Next Steps:**

1. Get Gmail App Password → Update SMTP_PASSWORD
2. Create Meta Developer Account → Get WhatsApp credentials
3. Update WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID
4. Restart server: `python app.py`
5. Test both Email and WhatsApp features
