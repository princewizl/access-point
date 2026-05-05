# smtp_test_starttls.py
import smtplib, ssl
from email.message import EmailMessage

host = "mail.layer21.com"
port = 587
user = "support@layer21.com"
pwd  = "Pa55word@123"
sender = "support@layer21.com"
recipient = "info@layer21.com"

msg = EmailMessage()
msg['Subject'] = 'SMTP STARTTLS test'
msg['From'] = sender
msg['To'] = recipient
msg.set_content('This is a deliverability test (STARTTLS 587).')

try:
    s = smtplib.SMTP(host, port, timeout=30)
    s.set_debuglevel(1)
    s.ehlo()
    s.starttls()
    s.ehlo()
    s.login(user, pwd)
    s.send_message(msg)
    s.quit()
    print("SMTP STARTTLS test: accepted by server.")
except Exception as e:
    print("SMTP STARTTLS test failed:", type(e), e)
