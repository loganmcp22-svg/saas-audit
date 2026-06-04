from flask import Flask, render_template, request, session, redirect, url_for

app = Flask(__name__)
app.secret_key = 'saas-audit-dev-key-change-in-prod'


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


if __name__ == '__main__':
    app.run(debug=True)
