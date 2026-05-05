# smtp_test_ssl.py
import smtplib, ssl
from email.message import EmailMessage
from flask import current_app as app

host = "mail.layer21.com"
port = 465
user = "support@layer21.com"
pwd  = "Pa55word@123"
sender = "support@layer21.com"
recipient = "info@layer21.com"   # change to a mailbox you control

msg = EmailMessage()
msg['Subject'] = 'SMTP SSL test'
msg['From'] = sender
msg['To'] = recipient
msg.set_content('This is a deliverability test (SSL 465).')

context = ssl.create_default_context()
try:
    s = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
    s.set_debuglevel(1)   # prints dialog
    s.login(user, pwd)
    s.send_message(msg)
    s.quit()
    print("SMTP SSL test: accepted by server.")
except Exception as e:
    print("SMTP SSL test failed:", type(e), e)
