import json
from datetime import datetime

import pytest
from app import app, db, Member, PaymentTransaction, _ensure_schema

@pytest.fixture(scope="module")
def test_client():
    app.config['TESTING'] = True
    with app.test_client() as c:
        with app.app_context():
            _ensure_schema()
            # Ensure a user session (simulate login)
            with c.session_transaction() as sess:
                sess['user_id'] = 1
                sess['username'] = 'tester'
        yield c


def create_member(client, **kwargs):
    payload = {
        'name': 'Test User',
        'phone': '03001234567',
        'admission_date': datetime.now().date().isoformat(),
        'training_type': 'other',
        'custom_training': 'CrossFit',
        'monthly_fee': 123.45,
        'special_tag': True,
    }
    payload.update(kwargs)
    res = client.post('/api/members', json=payload)
    assert res.status_code == 201, res.data
    return res.get_json()


def test_member_creation_with_custom_fields(test_client):
    data = create_member(test_client)
    assert data['custom_training'] == 'CrossFit'
    assert data['monthly_fee'] == 123.45
    assert data['display_training_type'] == 'CrossFit'


def test_update_member_fields(test_client):
    m = create_member(test_client)
    mid = m['id']
    res = test_client.put(f'/api/members/{mid}', json={
        'training_type': 'personal',
        'custom_training': '',
        'monthly_fee': 200,
        'special_tag': False,
    })
    assert res.status_code == 200
    updated = res.get_json()['member']
    assert updated['display_training_type'] == 'Personal'
    assert updated['monthly_fee'] == 200.0
    assert updated['custom_training'] == ''


def test_payment_record_and_status(test_client):
    m = create_member(test_client)
    mid = m['id']
    # Record a monthly payment
    now = datetime.now()
    res = test_client.post(f'/api/members/{mid}/pay', json={
        'plan_type': 'monthly',
        'year': now.year,
        'month': now.month,
        'amount': 500,
        'method': 'cash'
    })
    assert res.status_code == 200
    # Fetch member again
    res2 = test_client.get(f'/api/members/{mid}')
    member = res2.get_json()
    assert member['current_fee_status'] == 'Paid'
    assert member['last_tx_amount'] == 500


def test_list_members_contains_new_fields(test_client):
    m = create_member(test_client)
    res = test_client.get('/api/members')
    assert res.status_code == 200
    arr = res.get_json()
    assert any('custom_training' in x and x['id'] == m['id'] for x in arr)
    assert any('monthly_fee' in x and x['id'] == m['id'] for x in arr)
