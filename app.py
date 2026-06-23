import os
import requests
from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from extensions import db

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'saas-audit-dev-key-change-in-prod')

database_url = os.environ.get('DATABASE_URL', '')
# Railway (and Heroku) may supply postgres:// which SQLAlchemy 2.x requires as postgresql://
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url or 'sqlite:///local.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

import models  # noqa: E402 — registers models with SQLAlchemy metadata

with app.app_context():
    db.create_all()


@app.route('/', methods=['GET', 'POST'])
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
