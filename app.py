import math
import os
import re
import secrets
import requests
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from flask_login import login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFProtect, CSRFError
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import inspect, text
from extensions import db, login_manager
from email_utils import send_change_summary, send_verification_email, send_password_reset_email

app = Flask(__name__)
# SECRET_KEY must be set in production. Locally, fall back to a random
# per-process key (sessions reset on restart) rather than a hardcoded value.
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config['WTF_CSRF_SECRET_KEY'] = os.environ.get('WTF_CSRF_SECRET_KEY') or app.secret_key
app.config['WTF_CSRF_TIME_LIMIT'] = None  # token valid for the whole session

csrf = CSRFProtect(app)

database_url = os.environ.get('DATABASE_URL', '')
# Railway (and Heroku) may supply postgres:// which SQLAlchemy 2.x requires as postgresql://
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url or 'sqlite:///local.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)
login_manager.init_app(app)

import models  # noqa: E402 — registers models with SQLAlchemy metadata

with app.app_context():
    db.create_all()
    # create_all() only creates missing tables — add new columns to an
    # existing users table by hand.
    existing_cols = {c['name'] for c in inspect(db.engine).get_columns('users')}
    with db.engine.begin() as conn:
        if 'verified' not in existing_cols:
            conn.execute(text('ALTER TABLE users ADD COLUMN verified BOOLEAN NOT NULL DEFAULT FALSE'))
            # Grandfather in accounts that existed before verification —
            # they have no token, so they could never log in otherwise.
            conn.execute(text('UPDATE users SET verified = TRUE'))
        if 'email_verification_token' not in existing_cols:
            conn.execute(text('ALTER TABLE users ADD COLUMN email_verification_token VARCHAR(64)'))
        if 'password_reset_token' not in existing_cols:
            conn.execute(text('ALTER TABLE users ADD COLUMN password_reset_token VARCHAR(64)'))
        if 'password_reset_expires' not in existing_cols:
            conn.execute(text('ALTER TABLE users ADD COLUMN password_reset_expires TIMESTAMP'))


@login_manager.user_loader
def load_user(user_id):
    return models.User.query.get(int(user_id))


@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    # fetch() callers expect JSON; browser form posts get a plain message
    if request.path == '/send-test-email' or request.accept_mimetypes.best == 'application/json':
        return jsonify({'error': 'Your session expired. Refresh the page and try again.'}), 400
    return 'Your session expired. Please go back, refresh the page, and try again.', 400


# Subscription input limits (server-side; the form also enforces these client-side)
NAME_MAX_LENGTH = 100
COST_MAX = 99999
TAG_RE = re.compile(r'<[^>]*>')


def clean_subscription_name(raw):
    """Strip HTML tags and whitespace, cap length. Returns '' if nothing is left."""
    return TAG_RE.sub('', raw).strip()[:NAME_MAX_LENGTH]


def parse_cost(raw):
    """Parse a monthly cost, coercing anything invalid (nan/inf/negative/too large) to 0.0."""
    try:
        cost = float(raw)
    except (ValueError, TypeError):
        return 0.0
    if not math.isfinite(cost) or cost < 0 or cost > COST_MAX:
        return 0.0
    return cost


@app.route('/', methods=['GET', 'POST'])
def index():
    # Logged-out visitors see the marketing hero instead of the audit form
    if not current_user.is_authenticated:
        if request.method == 'POST':
            return redirect(url_for('login'))
        return render_template('index.html', subscriptions=[])

    if request.method == 'POST':
        names = request.form.getlist('name')
        costs = request.form.getlist('cost')
        usings = request.form.getlist('using')

        submitted_names = set()
        for name, cost_str, using in zip(names, costs, usings):
            name = clean_subscription_name(name)
            if not name:
                continue
            cost = parse_cost(cost_str)
            submitted_names.add(name)

            existing = models.Subscription.query.filter_by(
                user_id=current_user.id, name=name
            ).first()
            if existing:
                old_cost = float(existing.monthly_cost)
                old_is_active = existing.is_active
                existing.monthly_cost = cost
                existing.is_active = using == 'yes'
                if old_cost != cost:
                    db.session.add(models.SubscriptionHistory(
                        subscription_id=existing.id,
                        changed_field='monthly_cost',
                        old_value=str(old_cost),
                        new_value=str(cost),
                    ))
                if old_is_active != (using == 'yes'):
                    db.session.add(models.SubscriptionHistory(
                        subscription_id=existing.id,
                        changed_field='is_active',
                        old_value=str(old_is_active),
                        new_value=str(using == 'yes'),
                    ))
            else:
                sub = models.Subscription(
                    user_id=current_user.id,
                    name=name,
                    monthly_cost=cost,
                    is_active=using == 'yes',
                )
                db.session.add(sub)
                db.session.flush()
                db.session.add(models.SubscriptionHistory(
                    subscription_id=sub.id,
                    changed_field='created',
                    old_value=None,
                    new_value=str(cost),
                ))

        # Remove subscriptions the user deleted from the form
        # (delete their history rows first to satisfy the FK constraint)
        removed_q = models.Subscription.query.filter(
            models.Subscription.user_id == current_user.id
        )
        if submitted_names:
            removed_q = removed_q.filter(~models.Subscription.name.in_(submitted_names))
        removed_ids = [s.id for s in removed_q.all()]
        if removed_ids:
            models.SubscriptionHistory.query.filter(
                models.SubscriptionHistory.subscription_id.in_(removed_ids)
            ).delete(synchronize_session=False)
            models.Subscription.query.filter(
                models.Subscription.id.in_(removed_ids)
            ).delete(synchronize_session=False)

        db.session.commit()
        # Deliver the payoff directly instead of bouncing back to the form
        if models.Subscription.query.filter_by(user_id=current_user.id).count():
            return redirect(url_for('results'))
        return redirect(url_for('index', saved=1))

    db_subs = current_user.subscriptions.order_by(models.Subscription.created_at).all()
    subs_data = [
        {'name': s.name, 'cost': float(s.monthly_cost), 'using': 'yes' if s.is_active else 'no'}
        for s in db_subs
    ]
    total = sum(float(s.monthly_cost) for s in db_subs)
    waste = sum(float(s.monthly_cost) for s in db_subs if not s.is_active)
    last_updated = max((s.updated_at for s in db_subs), default=None)
    return render_template(
        'index.html',
        subscriptions=subs_data,
        total=total,
        waste=waste,
        last_updated=last_updated.strftime('%b %d, %Y') if last_updated else None,
        saved='saved' in request.args,
    )


@app.route('/results')
@login_required
def results():
    db_subs = current_user.subscriptions.order_by(models.Subscription.created_at).all()
    if not db_subs:
        return redirect(url_for('index'))

    subscriptions = [
        {'name': s.name, 'cost': float(s.monthly_cost), 'using': s.is_active}
        for s in db_subs
    ]

    total = sum(s['cost'] for s in subscriptions)
    waste = sum(s['cost'] for s in subscriptions if not s['using'])
    active = total - waste
    waste_pct = (waste / total * 100) if total > 0 else 0
    wasted_count = sum(1 for s in subscriptions if not s['using'])

    return render_template(
        'results.html',
        subscriptions=subscriptions,
        total=total,
        waste=waste,
        active=active,
        waste_pct=waste_pct,
        wasted_count=wasted_count,
    )


@app.route('/changes')
@login_required
def changes():
    history = (
        models.SubscriptionHistory.query
        .join(models.Subscription)
        .filter(models.Subscription.user_id == current_user.id)
        .order_by(models.SubscriptionHistory.changed_at.desc())
        .all()
    )
    entries = []
    for h in history:
        sub_name = h.subscription.name
        date_str = h.changed_at.strftime('%b %d, %Y')
        if h.changed_field == 'created':
            entries.append({
                'kind': 'added', 'date': date_str, 'name': sub_name,
                'text': f'added at ${float(h.new_value):.2f}/mo',
            })
        elif h.changed_field == 'monthly_cost':
            old = float(h.old_value)
            new = float(h.new_value)
            kind = 'increase' if new > old else 'decrease'
            direction = 'increased' if new > old else 'decreased'
            entries.append({
                'kind': kind, 'date': date_str, 'name': sub_name,
                'text': f'price {direction} from ${old:.2f} to ${new:.2f}',
            })
        elif h.changed_field == 'is_active':
            if h.new_value == 'False':
                entries.append({
                    'kind': 'paused', 'date': date_str, 'name': sub_name,
                    'text': 'marked as no longer in use',
                })
            else:
                entries.append({
                    'kind': 'resumed', 'date': date_str, 'name': sub_name,
                    'text': 'marked as in use again',
                })
    return render_template('changes.html', entries=entries)


@app.route('/send-test-email', methods=['POST'])
@login_required
def send_test_email():
    history = (
        models.SubscriptionHistory.query
        .join(models.Subscription)
        .filter(models.Subscription.user_id == current_user.id)
        .order_by(models.SubscriptionHistory.changed_at.desc())
        .all()
    )
    ok, error = send_change_summary(current_user.email, history)
    if ok:
        return jsonify({'ok': True})
    return jsonify({'error': error}), 500


@app.route('/cron/send-monthly-emails', methods=['POST'])
@csrf.exempt
def cron_send_monthly_emails():
    secret = os.environ.get('CRON_SECRET', '')
    if not secret or request.headers.get('X-Cron-Secret') != secret:
        return jsonify({'error': 'Unauthorized'}), 401

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    users = models.User.query.all()

    sent = []
    failed = []
    skipped = []

    for user in users:
        history = (
            models.SubscriptionHistory.query
            .join(models.Subscription)
            .filter(
                models.Subscription.user_id == user.id,
                models.SubscriptionHistory.changed_at >= cutoff,
            )
            .order_by(models.SubscriptionHistory.changed_at.desc())
            .all()
        )
        if not history:
            skipped.append(user.email)
            continue

        ok, error = send_change_summary(user.email, history)
        if ok:
            sent.append(user.email)
        else:
            failed.append({'email': user.email, 'error': error})

    return jsonify({'sent': sent, 'failed': failed, 'skipped': skipped})


@app.route('/pricing')
def pricing():
    return render_template('pricing.html')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    error = None
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        if not email or not password:
            error = 'Email and password are required.'
        elif models.User.query.filter_by(email=email).first():
            error = 'An account with that email already exists.'
        else:
            token = secrets.token_urlsafe(32)
            user = models.User(
                email=email,
                password_hash=generate_password_hash(password),
                email_verification_token=token,
            )
            db.session.add(user)
            db.session.commit()
            ok, send_error = send_verification_email(email, token)
            if not ok:
                app.logger.error('Verification email to %s failed: %s', email, send_error)
            # Log the user straight in — verification gates email features
            # (via the banner), not the product itself.
            login_user(user)
            return redirect(url_for('index'))
    return render_template('signup.html', error=error)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    error = None
    notice = None
    if 'verified' in request.args:
        notice = 'Email verified! You can now log in.'
    elif 'invalid_token' in request.args:
        error = 'That verification link is invalid or has already been used.'
    elif 'reset' in request.args:
        notice = 'Password updated! You can now log in with your new password.'
    elif 'reset_invalid' in request.args:
        error = 'That password reset link is invalid or has expired. Please request a new one.'
    if request.method == 'POST':
        notice = None
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user = models.User.query.filter_by(email=email).first()
        if not user or not check_password_hash(user.password_hash, password):
            error = 'Invalid email or password.'
        else:
            login_user(user)
            return redirect(url_for('index'))
    return render_template('login.html', error=error, notice=notice)


@app.route('/resend-verification', methods=['GET', 'POST'])
def resend_verification():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    error = None
    notice = None
    email = request.args.get('email', '').strip().lower()
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        if not email:
            error = 'Email is required.'
        else:
            user = models.User.query.filter_by(email=email).first()
            if user and user.verified:
                notice = 'That email is already verified — you can log in.'
            elif user:
                token = secrets.token_urlsafe(32)
                user.email_verification_token = token
                db.session.commit()
                ok, send_error = send_verification_email(email, token)
                if ok:
                    notice = 'Verification email sent! Check your inbox.'
                else:
                    app.logger.error('Resend verification to %s failed: %s', email, send_error)
                    error = 'We could not send the email right now. Please try again later.'
            else:
                # Don't reveal whether an account exists
                notice = 'Verification email sent! Check your inbox.'
    return render_template('resend.html', error=error, notice=notice, email=email)


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    error = None
    notice = None
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        if not email:
            error = 'Email is required.'
        else:
            user = models.User.query.filter_by(email=email).first()
            if user:
                token = secrets.token_urlsafe(32)
                user.password_reset_token = token
                user.password_reset_expires = datetime.now(timezone.utc) + timedelta(hours=1)
                db.session.commit()
                ok, send_error = send_password_reset_email(email, token)
                if not ok:
                    app.logger.error('Password reset email to %s failed: %s', email, send_error)
            # Same message whether or not the account exists — don't reveal it,
            # even when the send fails.
            notice = ('If an account exists for that email, we\'ve sent a link '
                      'to reset your password. Check your inbox.')
    return render_template('forgot.html', error=error, notice=notice)


def _user_for_reset_token(token):
    """Return the user for a reset token, or None if unknown/expired."""
    user = models.User.query.filter_by(password_reset_token=token).first()
    if not user or not user.password_reset_expires:
        return None
    expires = user.password_reset_expires
    # SQLite drops tzinfo on the way back out; stored values are always UTC
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        return None
    return user


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    user = _user_for_reset_token(token)
    if not user:
        return redirect(url_for('login', reset_invalid=1))

    error = None
    if request.method == 'POST':
        password = request.form.get('password', '')
        confirm = request.form.get('confirm', '')
        if len(password) < 8:
            error = 'Password must be at least 8 characters.'
        elif password != confirm:
            error = 'Passwords do not match.'
        else:
            user.password_hash = generate_password_hash(password)
            user.password_reset_token = None
            user.password_reset_expires = None
            db.session.commit()
            return redirect(url_for('login', reset=1))
    return render_template('reset.html', error=error, token=token)


@app.route('/verify/<token>')
def verify_email(token):
    user = models.User.query.filter_by(email_verification_token=token).first()
    if not user:
        if current_user.is_authenticated:
            return redirect(url_for('index'))
        return redirect(url_for('login', invalid_token=1))
    user.verified = True
    user.email_verification_token = None
    db.session.commit()
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    return redirect(url_for('login', verified=1))


@app.route('/resend-my-verification', methods=['POST'])
@login_required
def resend_my_verification():
    """Resend the verification email for the logged-in user (banner button)."""
    if not current_user.verified:
        token = secrets.token_urlsafe(32)
        current_user.email_verification_token = token
        db.session.commit()
        ok, send_error = send_verification_email(current_user.email, token)
        if not ok:
            app.logger.error('Resend verification to %s failed: %s',
                             current_user.email, send_error)
    return redirect(request.referrer or url_for('index'))


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


@app.route('/api/waitlist', methods=['POST'])
@csrf.exempt
def waitlist():
    data = request.get_json()
    email = (data or {}).get('email', '').strip()
    if not email:
        return jsonify({'error': 'Email is required.'}), 400

    api_key = os.environ.get('MAILCHIMP_API_KEY', '')
    audience_id = os.environ.get('MAILCHIMP_AUDIENCE_ID', '')
    if not api_key or not audience_id:
        with open('waitlist.txt', 'a') as f:
            f.write(email + '\n')
        return jsonify({'ok': True})

    dc = api_key.split('-')[-1]
    url = f'https://{dc}.api.mailchimp.com/3.0/lists/{audience_id}/members'
    try:
        resp = requests.post(
            url,
            auth=('anystring', api_key),
            json={'email_address': email, 'status': 'subscribed'},
            timeout=10,
        )
    except requests.RequestException:
        return jsonify({'error': 'Could not reach Mailchimp. Please try again.'}), 502

    if resp.status_code in (200, 201):
        return jsonify({'ok': True})
    body = resp.json()
    # 400 with title "Member Exists" means already subscribed — treat as success
    if resp.status_code == 400 and body.get('title') == 'Member Exists':
        return jsonify({'ok': True})
    return jsonify({'error': body.get('detail', 'Something went wrong. Please try again.')}), 400


if __name__ == '__main__':
    app.run(debug=True)
