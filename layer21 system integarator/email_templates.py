# ─────────────────────────────────────────────────────────────────────────────
# Replace _IT_EMAIL_STYLE and all html_template strings in it_helpdesk_routes.py
# Email-safe: table-based layout, no flexbox/grid, inline-friendly styles
# ─────────────────────────────────────────────────────────────────────────────

_IT_EMAIL_STYLE = """
<style>
  @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600&display=swap');

  body, table, td, p, a, li, blockquote {
    -webkit-text-size-adjust: 100%;
    -ms-text-size-adjust: 100%;
    margin: 0; padding: 0;
  }

  body {
    background-color: #F4F9FF;
    font-family: 'DM Sans', Arial, sans-serif;
    color: #0A2540;
  }

  table { border-collapse: collapse; }
  img   { border: 0; outline: none; text-decoration: none; }

  /* ── Outer wrapper ───────────────────────── */
  .email-outer {
    width: 100%;
    background-color: #F4F9FF;
    padding: 40px 16px;
  }

  /* ── Card ───────────────────────── */
  .email-card {
    width: 100%;
    max-width: 580px;
    margin: 0 auto;
    background: #FFFFFF;
    border-radius: 16px;
    overflow: hidden;
    box-shadow: 0 6px 26px rgba(10,37,64,0.12);
  }

  /* ── Header ───────────────────────── */
  .email-header {
    background: linear-gradient(135deg, #0A2540 0%, #00CFFF 100%);
    padding: 36px 40px 28px;
    text-align: center;
  }

  .email-header__logo-wrap {
    display: inline-block;
    background: rgba(255,255,255,0.18);
    border-radius: 12px;
    padding: 10px 14px;
    margin-bottom: 18px;
  }

  .email-header__logo-text {
    color: #FFFFFF;
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .email-header__title {
    color: #FFFFFF;
    font-size: 22px;
    font-weight: 700;
    margin: 0;
    line-height: 1.3;
  }

  .email-header__sub {
    color: rgba(255,255,255,0.75);
    font-size: 13px;
    margin-top: 6px;
  }

  /* ── Tag strip ───────────────────────── */
  .email-tag-strip {
    background: #F4F9FF;
    border-bottom: 1px solid #E3EEF8;
    padding: 14px 40px;
    text-align: center;
  }

  .email-tag {
    display: inline-block;
    padding: 4px 14px;
    border-radius: 20px;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.06em;
    margin: 0 4px;
  }

  .tag-id { background: rgba(0,207,255,.12); color: #009AC0; }
  .tag-high { background: rgba(0,207,255,.15); color: #009AC0; }
  .tag-medium { background: rgba(10,37,64,.10); color: #0A2540; }
  .tag-low { background: rgba(0,207,255,.10); color: #00A5CC; }
  .tag-critical { background: rgba(10,37,64,.12); color: #0A2540; }
  .tag-open { background: rgba(0,207,255,.15); color: #009AC0; }
  .tag-closed { background: rgba(10,37,64,.10); color: #0A2540; }
  .tag-info { background: rgba(0,207,255,.12); color: #009AC0; }

  /* ── Body ───────────────────────── */
  .email-body {
    padding: 36px 40px;
  }

  .email-greeting {
    font-size: 17px;
    font-weight: 600;
    color: #0A2540;
    margin-bottom: 12px;
  }

  .email-text {
    font-size: 14px;
    line-height: 1.7;
    color: #4A5B70;
  }

  /* ── Divider ───────────────────────── */
  .email-divider {
    border: none;
    border-top: 1px solid #E3EEF8;
    margin: 28px 0;
  }

  /* ── Info table ───────────────────────── */
  .email-info-table {
    width: 100%;
    border: 1px solid #E3EEF8;
    border-radius: 10px;
    overflow: hidden;
    margin-bottom: 28px;
    font-size: 13px;
  }

  .email-info-table tr:not(:last-child) td {
    border-bottom: 1px solid #E3EEF8;
  }

  .email-info-label {
    padding: 11px 16px;
    background: #F4F9FF;
    color: #6C7A8C;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    font-size: 11px;
    width: 38%;
  }

  .email-info-value {
    padding: 11px 16px;
    color: #0A2540;
    font-weight: 500;
  }

  /* ── Message quote block ───────────────────────── */
  .email-quote {
    background: #F4F9FF;
    border-left: 3px solid #00CFFF;
    border-radius: 0 8px 8px 0;
    padding: 14px 18px;
    font-size: 14px;
    color: #4A5B70;
    line-height: 1.65;
    margin-bottom: 28px;
    font-style: italic;
  }

  /* ── CTA Button ───────────────────────── */
  .email-cta-wrap {
    text-align: center;
    margin-bottom: 28px;
  }

  .email-cta {
    display: inline-block;
    padding: 13px 34px;
    background: #00CFFF;
    color: #FFFFFF !important;
    font-size: 14px;
    font-weight: 700;
    text-decoration: none;
    border-radius: 8px;
    letter-spacing: 0.04em;
    box-shadow: 0 4px 14px rgba(0,207,255,0.35);
  }

  /* ── Alert banner ───────────────────────── */
  .email-alert-banner {
    background: #E9F8FF;
    border: 1px solid rgba(0,207,255,.30);
    border-radius: 8px;
    padding: 13px 18px;
    display: flex;
    align-items: flex-start;
    gap: 10px;
    font-size: 13px;
    color: #009AC0;
    font-weight: 500;
    margin-bottom: 24px;
  }

  /* ── Footer ───────────────────────── */
  .email-footer {
    background: #F4F9FF;
    border-top: 1px solid #E3EEF8;
    padding: 24px 40px;
    text-align: center;
  }

  .email-footer p {
    font-size: 12px;
    color: #7A8899;
    line-height: 1.6;
    margin: 0;
  }

  .email-footer a {
    color: #00CFFF;
    text-decoration: none;
  }

  .email-footer__brand {
    font-size: 13px;
    font-weight: 700;
    color: #0A2540;
    margin-bottom: 4px;
  }

  /* ── Responsive ───────────────────────── */
  @media only screen and (max-width: 600px) {
    .email-body,
    .email-header,
    .email-footer,
    .email-tag-strip {
      padding-left: 22px !important;
      padding-right: 22px !important;
    }

    .email-header__title {
      font-size: 18px !important;
    }
  }
</style>
"""


# ─────────────────────────────────────────────────────────────────────────────
# Template snippets — copy-paste into your route html_template strings
# ─────────────────────────────────────────────────────────────────────────────

# ── 1. New ticket notification (to IT team) ───────────────────────────────────
NEW_TICKET_TEMPLATE = _IT_EMAIL_STYLE + """
<div class="email-outer">
  <div class="email-card">

    <div class="email-header">
      <div class="email-header__logo-wrap">
        <span class="email-header__logo-text">&#128187; Layer21 E-Invoice HelpDesk</span>
      </div>
      <h1 class="email-header__title">New Complaint</h1>
      <p class="email-header__sub">Action required — please review and attend</p>
    </div>

    <div class="email-tag-strip">
      <span class="email-tag tag-{{ priority | lower }}">{{ priority }}</span>
      <span class="email-tag tag-info">{{ category }}</span>
    </div>

    <div class="email-body">
      <p class="email-greeting">Hello Layer21,</p>
      <p class="email-text">
        A new Complaint has been submitted by
        <strong>{{name }}</strong> and is awaiting your review.
      </p>

      <hr class="email-divider">

      <table class="email-info-table">
        <tr>
          <td class="email-info-label">Raised By</td>
          <td class="email-info-value">{{ name }}</td>
        </tr>
        <tr>
          <td class="email-info-label">Issue Type</td>
          <td class="email-info-value">{{ category }}</td>
        </tr>
        <tr>
          <td class="email-info-label">Priority</td>
          <td class="email-info-value">{{ priority }}</td>
        </tr>
        {% if asset_tag %}
        <tr>
          <td class="email-info-label">Description</td>
          <td class="email-info-value">{{ description }}</td>
        </tr>
        {% endif %}
      </table>

      {% if priority | lower == 'critical' %}
      <div class="email-alert-banner">
        &#9888;&#65039; <span>This ticket is marked <strong>Critical</strong>. Please respond within <strong>1 hour</strong>.</span>
      </div>
      {% endif %}

      <div class="email-cta-wrap">
        <a href="#" class="email-cta">View &amp; Assign Ticket</a>
      </div>

      <p class="email-text" style="text-align:center;font-size:13px;color:#8a94a6;">
        Log in to the portal to respond and update the ticket status.
      </p>
    </div>

    <div class="email-footer">
      <p class="email-footer__brand">Layer21</p>
      <p>This is an automated notification. Please do not reply to this email.<br>
         For urgent issues, contact IT directly.</p>
    </div>

  </div>
</div>
"""

NEW_TICKET_CUSTOMER_TEMPLATE = _IT_EMAIL_STYLE + """
<div class="email-outer">
  <div class="email-card">

    <div class="email-header">
      <div class="email-header__logo-wrap">
        <span class="email-header__logo-text">&#128187; Layer21 E-Invoice HelpDesk</span>
      </div>
      <h1 class="email-header__title">Complaint Recieved</h1>
      <p class="email-header__sub">We have recieved your complaint and are attending</p>
    </div>

    <div class="email-tag-strip">
      <span class="email-tag tag-{{ priority | lower }}">{{ priority }}</span>
      <span class="email-tag tag-info">{{ category }}</span>
    </div>

    <div class="email-body">
      <p class="email-greeting">Hello {{name }},</p>
      <p class="email-text">
        We have recieved a complaint from you with the following details
      </p>

      <hr class="email-divider">

      <table class="email-info-table">
        <tr>
          <td class="email-info-label">Raised By</td>
          <td class="email-info-value">{{ name }}</td>
        </tr>
        <tr>
          <td class="email-info-label">Issue Type</td>
          <td class="email-info-value">{{ category }}</td>
        </tr>
        <tr>
          <td class="email-info-label">Priority</td>
          <td class="email-info-value">{{ priority }}</td>
        </tr>
        {% if asset_tag %}
        <tr>
          <td class="email-info-label">Description</td>
          <td class="email-info-value">{{ description }}</td>
        </tr>
        {% endif %}
      </table>

      {% if priority | lower == 'critical' %}
      <div class="email-alert-banner">
        &#9888;&#65039; <span>This ticket is marked <strong>Critical</strong>. Please respond within <strong>1 hour</strong>.</span>
      </div>
      {% endif %}

      <div class="email-cta-wrap">
        <a href="#" class="email-cta">View &amp; Assign Ticket</a>
      </div>

      <p class="email-text" style="text-align:center;font-size:13px;color:#8a94a6;">
        Log in to the portal to respond and update the ticket status.
      </p>
    </div>

    <div class="email-footer">
      <p class="email-footer__brand">Layer21</p>
      <p>This is an automated notification. Please do not reply to this email.<br>
         For urgent issues, contact IT directly.</p>
    </div>

  </div>
</div>
"""


# ── 2. Invitation to collaborate ─────────────────────────────────────────────
INVITE_TEMPLATE = _IT_EMAIL_STYLE + """
<div class="email-outer">
  <div class="email-card">

    <div class="email-header">
      <div class="email-header__logo-wrap">
        <span class="email-header__logo-text">&#128187; HC App IT HelpDesk</span>
      </div>
      <h1 class="email-header__title">You've Been Invited</h1>
      <p class="email-header__sub">You have been added to an IT support ticket</p>
    </div>

    <div class="email-tag-strip">
      <span class="email-tag tag-id">Ticket #{{ ticket_id }}</span>
      <span class="email-tag tag-info">Collaboration Request</span>
    </div>

    <div class="email-body">
      <p class="email-greeting">Hello {{ name }},</p>
      <p class="email-text">
        You have been invited to collaborate on an IT HelpDesk ticket.
        Your input or assistance has been requested for the following reason:
      </p>

      <hr class="email-divider">

      <div class="email-quote">{{ reason or 'Your expertise is needed on this ticket.' }}</div>

      <table class="email-info-table">
        <tr>
          <td class="email-info-label">Ticket ID</td>
          <td class="email-info-value">#{{ ticket_id }}</td>
        </tr>
        <tr>
          <td class="email-info-label">Invited By</td>
          <td class="email-info-value">{{ invited_by or 'IT HelpDesk Team' }}</td>
        </tr>
      </table>

      <div class="email-cta-wrap">
        <a href="#" class="email-cta">View Ticket &amp; Respond</a>
      </div>
    </div>

    <div class="email-footer">
      <p class="email-footer__brand">HC App IT HelpDesk</p>
      <p>This is an automated notification. Please do not reply to this email.</p>
    </div>

  </div>
</div>
"""


# ── 3. New chat message notification ─────────────────────────────────────────
CHAT_NOTIFICATION_TEMPLATE = _IT_EMAIL_STYLE + """
<div class="email-outer">
  <div class="email-card">

    <div class="email-header">
      <div class="email-header__logo-wrap">
        <span class="email-header__logo-text">&#128187; HC App IT HelpDesk</span>
      </div>
      <h1 class="email-header__title">New Message on Ticket</h1>
      <p class="email-header__sub">Ticket #{{ ticket_id }} has been updated</p>
    </div>

    <div class="email-tag-strip">
      <span class="email-tag tag-id">Ticket #{{ ticket_id }}</span>
      <span class="email-tag tag-info">New Reply</span>
    </div>

    <div class="email-body">
      <p class="email-greeting">Hello,</p>
      <p class="email-text">
        <strong>{{ sender }}</strong> posted a new message on IT support ticket
        <strong>#{{ ticket_id }}</strong>:
      </p>

      <hr class="email-divider">

      <div class="email-quote">{{ message }}</div>

      <div class="email-cta-wrap">
        <a href="#" class="email-cta">Reply in Portal</a>
      </div>

      <p class="email-text" style="text-align:center;font-size:13px;color:#8a94a6;">
        Log in to view the full conversation thread and respond.
      </p>
    </div>

    <div class="email-footer">
      <p class="email-footer__brand">HC App IT HelpDesk</p>
      <p>You are receiving this because you are a participant on this ticket.<br>
         This is an automated notification — please do not reply to this email.</p>
    </div>

  </div>
</div>
"""
