import os
import requests
from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from extensions import db, login_manager
from email_utils import send_change_summary

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'saas-audit-dev-key-change-in-prod')

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


@login_manager.user_loader
def load_user(user_id):
    return models.User.query.get(int(user_id))


@app.route('/', methods=['GET', 'POST'])
@login_required
def index():
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
        if submitted_names:
            models.Subscription.query.filter(
                models.Subscription.user_id == current_user.id,
                ~models.Subscription.name.in_(submitted_names),
            ).delete(synchronize_session='fetch')
        else:
            models.Subscription.query.filter_by(user_id=current_user.id).delete()

        db.session.commit()
        return redirect(url_for('results'))

    db_subs = current_user.subscriptions.order_by(models.Subscription.created_at).all()
    subs_data = [
        {'name': s.name, 'cost': float(s.monthly_cost), 'using': 'yes' if s.is_active else 'no'}
        for s in db_subs
    ]
    return render_template('index.html', subscriptions=subs_data)


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
            entries.append(f'{sub_name} added at ${float(h.new_value):.2f}/mo on {date_str}')
        elif h.changed_field == 'monthly_cost':
            old = float(h.old_value)
            new = float(h.new_value)
            direction = 'increased' if new > old else 'decreased'
            entries.append(f'{sub_name} price {direction} from ${old:.2f} to ${new:.2f} on {date_str}')
        elif h.changed_field == 'is_active':
            if h.new_value == 'False':
                entries.append(f'{sub_name} marked as no longer in use on {date_str}')
            else:
                entries.append(f'{sub_name} marked as in use again on {date_str}')
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
            user = models.User(email=email, password_hash=generate_password_hash(password))
            db.session.add(user)
            db.session.commit()
            login_user(user)
            return redirect(url_for('index'))
    return render_template('signup.html', error=error)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    error = None
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user = models.User.query.filter_by(email=email).first()
        if not user or not check_password_hash(user.password_hash, password):
            error = 'Invalid email or password.'
        else:
            login_user(user)
            return redirect(url_for('index'))
    return render_template('login.html', error=error)


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
