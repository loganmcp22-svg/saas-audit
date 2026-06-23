import os
import requests
from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from extensions import db, login_manager

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

        subscriptions = []
        for name, cost_str, using in zip(names, costs, usings):
            name = name.strip()
            if not name:
                continue
            try:
                cost = float(cost_str)
            except (ValueError, TypeError):
                cost = 0.0
            subscriptions.append({
                'name': name,
                'cost': cost,
                'using': using == 'yes',
            })

        session['subscriptions'] = subscriptions
        return redirect(url_for('results'))

    return render_template('index.html')


@app.route('/results')
@login_required
def results():
    subscriptions = session.get('subscriptions', [])
    if not subscriptions:
        return redirect(url_for('index'))

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
