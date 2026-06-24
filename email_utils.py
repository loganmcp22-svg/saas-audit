import os
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

FROM_EMAIL = 'noreply@subaudit.io'


def _format_entry(h):
    sub_name = h.subscription.name
    date_str = h.changed_at.strftime('%b %d, %Y')
    if h.changed_field == 'created':
        return f'{sub_name} added at ${float(h.new_value):.2f}/mo on {date_str}'
    elif h.changed_field == 'monthly_cost':
        old = float(h.old_value)
        new = float(h.new_value)
        direction = 'increased' if new > old else 'decreased'
        return f'{sub_name} price {direction} from ${old:.2f} to ${new:.2f} on {date_str}'
    elif h.changed_field == 'is_active':
        if h.new_value == 'False':
            return f'{sub_name} marked as no longer in use on {date_str}'
        else:
            return f'{sub_name} marked as in use again on {date_str}'
    return None


def send_change_summary(user_email, history_entries):
    """Send a subscription change summary email. Returns (ok: bool, error: str|None)."""
    api_key = os.environ.get('SENDGRID_API_KEY', '')
    if not api_key:
        return False, 'SENDGRID_API_KEY is not set'

    entries = [e for e in (_format_entry(h) for h in history_entries) if e]

    if entries:
        items_html = '\n'.join(
            f'<li style="padding:10px 0;border-bottom:1px solid #f1f5f9;font-size:15px;color:#0f172a;">{e}</li>'
            for e in entries
        )
        list_section = f'<ul style="list-style:none;margin:0;padding:0;">{items_html}</ul>'
    else:
        list_section = '<p style="color:#64748b;font-size:15px;">No changes recorded yet.</p>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f8fafc;padding:40px 20px;">
    <tr><td align="center">
      <table width="100%" style="max-width:560px;background:#ffffff;border-radius:16px;border:1px solid #e2e8f0;box-shadow:0 1px 4px rgba(0,0,0,0.05);">
        <tr>
          <td style="background:#0f172a;border-radius:16px 16px 0 0;padding:24px 32px;">
            <span style="font-size:1.2rem;font-weight:800;color:#ffffff;letter-spacing:-0.03em;">
              Sub<span style="color:#10b981;">Audit</span>
            </span>
          </td>
        </tr>
        <tr>
          <td style="padding:32px;">
            <h1 style="font-size:1.3rem;font-weight:800;color:#0f172a;margin:0 0 8px;letter-spacing:-0.02em;">
              Here's what changed with your subscriptions
            </h1>
            <p style="font-size:14px;color:#64748b;margin:0 0 28px;">
              A summary of all changes recorded in your SubAudit account.
            </p>
            <div style="border:1px solid #e2e8f0;border-radius:12px;padding:24px;">
              <p style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#94a3b8;margin:0 0 16px;">
                Change History
              </p>
              {list_section}
            </div>
            <p style="font-size:13px;color:#94a3b8;margin:28px 0 0;">
              You're receiving this because you requested a summary from SubAudit.
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

    text = 'Here\'s what changed with your subscriptions:\n\n' + '\n'.join(f'• {e}' for e in entries)

    message = Mail(
        from_email=FROM_EMAIL,
        to_emails=user_email,
        subject="Here's what changed with your subscriptions",
        html_content=html,
        plain_text_content=text,
    )

    try:
        sg = SendGridAPIClient(api_key)
        sg.send(message)
        return True, None
    except Exception as exc:
        return False, str(exc)
