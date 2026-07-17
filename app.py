import os
import secrets
import requests
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import inspect, text
from extensions import db, login_manager
from email_utils import send_change_summary, send_verification_email

app = Flask(__name__)
# SECRET_KEY must be set in production. Locally, fall back to a random
# per-process key (sessions reset on restart) rather than a hardcoded value.
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)

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


@login_manager.user_loader
def load_user(user_id):
    return models.User.query.get(int(user_id))


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
            name = name.strip()
            if not name:
                continue
            try:
                cost = float(cost_str)
            except (ValueError, TypeError):
                cost = 0.0
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
                return redirect(url_for('login', registered=1, email_failed=1))
            return redirect(url_for('login', registered=1))
    return render_template('signup.html', error=error)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    error = None
    notice = None
    show_resend = False
    resend_email = ''
    if 'registered' in request.args:
        notice = ('Account created! Please check your email to verify your '
                  'account before logging in.')
        if 'email_failed' in request.args:
            notice = ('Account created, but we could not send the verification '
                      'email. Please contact support.')
    elif 'verified' in request.args:
        notice = 'Email verified! You can now log in.'
    elif 'invalid_token' in request.args:
        error = 'That verification link is invalid or has already been used.'
    if request.method == 'POST':
        notice = None
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user = models.User.query.filter_by(email=email).first()
        if not user or not check_password_hash(user.password_hash, password):
            error = 'Invalid email or password.'
        elif not user.verified:
            error = 'Please verify your email before logging in. Check your inbox.'
            show_resend = True
            resend_email = email
        else:
            login_user(user)
            return redirect(url_for('index'))
    return render_template('login.html', error=error, notice=notice,
                           show_resend=show_resend, resend_email=resend_email)


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


@app.route('/verify/<token>')
def verify_email(token):
    user = models.User.query.filter_by(email_verification_token=token).first()
    if not user:
        return redirect(url_for('login', invalid_token=1))
    user.verified = True
    user.email_verification_token = None
    db.session.commit()
    return redirect(url_for('login', verified=1))


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


@app.route('/api/waitlist', methods=['POST'])
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
