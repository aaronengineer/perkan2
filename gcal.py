"""Two-way sync between the PerKan board and a single Google Calendar.

PerKan has no user accounts, so this module connects the whole board to
exactly one Google account (the token lives in data/gcal_token.json,
which is already gitignored alongside kanban.json). Cards with a
due_date are pushed to Google Calendar as events; changes made on the
Google Calendar side (including brand new events) are pulled back in by
a background polling loop, since a self-hosted/local instance generally
has no public HTTPS endpoint for Google's push-notification webhooks.
"""
import os
import json
import time
import uuid
import random
import string
import logging
import threading
import datetime

import requests

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
TOKEN_FILE = os.path.join(DATA_DIR, 'gcal_token.json')
_token_lock = threading.Lock()

AUTH_ENDPOINT = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_ENDPOINT = 'https://oauth2.googleapis.com/token'
API_BASE = 'https://www.googleapis.com/calendar/v3'
SCOPE = 'https://www.googleapis.com/auth/calendar'
EXTENDED_PROP_KEY = 'perkan_card_id'
DEFAULT_EVENT_DURATION_MINUTES = 30


def _config():
    return {
        'client_id': os.environ.get('GOOGLE_CLIENT_ID', ''),
        'client_secret': os.environ.get('GOOGLE_CLIENT_SECRET', ''),
        'redirect_uri': os.environ.get('GOOGLE_REDIRECT_URI', 'http://127.0.0.1:5000/auth/google/callback'),
        'calendar_id': os.environ.get('GOOGLE_CALENDAR_ID', 'primary'),
        'poll_interval': int(os.environ.get('GCAL_SYNC_INTERVAL_SECONDS', '60')),
        'import_new_events': os.environ.get('GCAL_IMPORT_NEW_EVENTS', 'true').lower() not in ('0', 'false', 'no'),
        'default_column': os.environ.get('GCAL_DEFAULT_COLUMN', 'todo'),
        'sync_window_days': int(os.environ.get('GCAL_SYNC_WINDOW_DAYS', '90')),
        # Cards store naive "wall clock" datetimes (no offset). Google needs
        # to know which IANA zone that wall clock is in, or it assumes UTC.
        'timezone': os.environ.get('GCAL_TIMEZONE', 'UTC'),
    }


def is_configured():
    cfg = _config()
    return bool(cfg['client_id'] and cfg['client_secret'])


def _ensure_data_dir():
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)


def _load_token():
    _ensure_data_dir()
    if not os.path.exists(TOKEN_FILE):
        return {}
    try:
        with open(TOKEN_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_token(token):
    _ensure_data_dir()
    with _token_lock:
        tmp = TOKEN_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(token, f, indent=2)
        os.replace(tmp, TOKEN_FILE)


def is_connected():
    token = _load_token()
    return bool(token.get('refresh_token'))


def disconnect():
    with _token_lock:
        if os.path.exists(TOKEN_FILE):
            os.remove(TOKEN_FILE)


def status():
    token = _load_token()
    return {
        'configured': is_configured(),
        'connected': bool(token.get('refresh_token')),
        'calendar_id': _config()['calendar_id'],
        'connected_email': token.get('connected_email'),
        'last_sync': token.get('last_sync'),
        'last_sync_error': token.get('last_sync_error'),
    }


def get_auth_url():
    cfg = _config()
    state = ''.join(random.choices(string.ascii_letters + string.digits, k=24))
    token = _load_token()
    token['oauth_state'] = state
    token['oauth_state_created'] = time.time()
    _save_token(token)
    params = {
        'client_id': cfg['client_id'],
        'redirect_uri': cfg['redirect_uri'],
        'response_type': 'code',
        'scope': SCOPE,
        'access_type': 'offline',
        'prompt': 'consent',
        'state': state,
        'include_granted_scopes': 'true',
    }
    query = '&'.join(f'{k}={requests.utils.quote(str(v), safe="")}' for k, v in params.items())
    return f'{AUTH_ENDPOINT}?{query}'


def verify_state(state):
    token = _load_token()
    saved = token.get('oauth_state')
    created = token.get('oauth_state_created') or 0
    if not saved or not state or saved != state:
        return False
    return (time.time() - created) < 600  # 10 minute window


def exchange_code(code):
    cfg = _config()
    resp = requests.post(TOKEN_ENDPOINT, data={
        'code': code,
        'client_id': cfg['client_id'],
        'client_secret': cfg['client_secret'],
        'redirect_uri': cfg['redirect_uri'],
        'grant_type': 'authorization_code',
    }, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    token = _load_token()
    token.pop('oauth_state', None)
    token.pop('oauth_state_created', None)
    token['access_token'] = payload['access_token']
    token['expires_at'] = time.time() + payload.get('expires_in', 3600) - 60
    if payload.get('refresh_token'):
        token['refresh_token'] = payload['refresh_token']
    token['connected_email'] = _fetch_email(payload['access_token'])
    token.pop('sync_token', None)  # force a fresh full sync with the new grant
    _save_token(token)
    return token


def _fetch_email(access_token):
    try:
        resp = requests.get(
            'https://www.googleapis.com/oauth2/v2/userinfo',
            headers={'Authorization': f'Bearer {access_token}'}, timeout=10)
        if resp.ok:
            return resp.json().get('email')
    except requests.RequestException:
        pass
    return None


def _refresh_access_token(token):
    cfg = _config()
    resp = requests.post(TOKEN_ENDPOINT, data={
        'refresh_token': token['refresh_token'],
        'client_id': cfg['client_id'],
        'client_secret': cfg['client_secret'],
        'grant_type': 'refresh_token',
    }, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    token['access_token'] = payload['access_token']
    token['expires_at'] = time.time() + payload.get('expires_in', 3600) - 60
    _save_token(token)
    return token


def _get_access_token():
    token = _load_token()
    if not token.get('refresh_token'):
        return None
    if token.get('access_token') and token.get('expires_at', 0) > time.time():
        return token['access_token']
    token = _refresh_access_token(token)
    return token.get('access_token')


class GcalError(Exception):
    pass


def _request(method, path, **kwargs):
    access_token = _get_access_token()
    if not access_token:
        raise GcalError('not connected')
    headers = kwargs.pop('headers', {})
    headers['Authorization'] = f'Bearer {access_token}'
    resp = requests.request(method, f'{API_BASE}{path}', headers=headers, timeout=15, **kwargs)
    return resp


# ---------------------------------------------------------------------------
# Card <-> event conversion
# ---------------------------------------------------------------------------

def _event_body_from_card(card, cfg):
    due_date = card.get('due_date')
    all_day = bool(card.get('all_day'))
    body = {
        'summary': card.get('title') or 'Untitled',
        'description': card.get('description') or '',
        'extendedProperties': {'private': {EXTENDED_PROP_KEY: card['id']}},
    }
    if due_date:
        if all_day:
            start_date = due_date[:10]
            end_date = (datetime.date.fromisoformat(start_date) + datetime.timedelta(days=1)).isoformat()
            body['start'] = {'date': start_date}
            body['end'] = {'date': end_date}
        else:
            start_dt = datetime.datetime.fromisoformat(due_date)
            end_dt = start_dt + datetime.timedelta(minutes=DEFAULT_EVENT_DURATION_MINUTES)
            # dateTime here is a naive wall-clock string; timeZone tells
            # Google how to interpret it instead of assuming UTC.
            body['start'] = {'dateTime': start_dt.isoformat(), 'timeZone': cfg['timezone']}
            body['end'] = {'dateTime': end_dt.isoformat(), 'timeZone': cfg['timezone']}
    return body


def _card_fields_from_event(event):
    start = event.get('start', {})
    if 'date' in start:
        return {'due_date': start['date'], 'all_day': True}
    if 'dateTime' in start:
        # Strip timezone offset for the simple local-time input the UI uses.
        dt = start['dateTime']
        return {'due_date': dt[:19], 'all_day': False}
    return {'due_date': None, 'all_day': False}


def upsert_event_for_card(card):
    """Create or update the Google Calendar event linked to a card.

    Mutates the card dict in place with gcal_event_id/gcal_updated on
    success. No-ops (returns False) if not connected or the card has no
    due_date. Raises GcalError on API failure so callers can decide
    whether to surface it (a sync failure should never block saving the
    card locally).
    """
    if not is_connected() or not card.get('due_date'):
        return False
    cfg = _config()
    body = _event_body_from_card(card, cfg)
    event_id = card.get('gcal_event_id')
    if event_id:
        resp = _request('PATCH', f'/calendars/{cfg["calendar_id"]}/events/{event_id}', json=body)
        if resp.status_code == 404:
            event_id = None  # event was deleted on the Google side; recreate it
    if not event_id:
        resp = _request('POST', f'/calendars/{cfg["calendar_id"]}/events', json=body)
    if not resp.ok:
        raise GcalError(f'calendar API error {resp.status_code}: {resp.text[:200]}')
    event = resp.json()
    card['gcal_event_id'] = event['id']
    card['gcal_updated'] = event.get('updated')
    return True


def delete_event_for_card(card):
    event_id = card.get('gcal_event_id')
    if not is_connected() or not event_id:
        return False
    cfg = _config()
    resp = _request('DELETE', f'/calendars/{cfg["calendar_id"]}/events/{event_id}')
    if resp.status_code not in (200, 204, 404, 410, 412):
        raise GcalError(f'calendar API error {resp.status_code}: {resp.text[:200]}')
    return True


# ---------------------------------------------------------------------------
# Pull sync
# ---------------------------------------------------------------------------

def _parse_rfc3339(value):
    if not value:
        return 0.0
    try:
        return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return 0.0


def pull_changes(board):
    """Apply Google Calendar changes to the in-memory board dict.

    Returns True if the board was modified (caller is responsible for
    persisting it). Card matching is by gcal_event_id first, falling
    back to the perkan_card_id extended property.
    """
    if not is_connected():
        return False
    cfg = _config()
    token = _load_token()
    sync_token = token.get('sync_token')

    cards_by_event = {}
    cards_by_id = {}
    for col in board.get('columns', []):
        for card in col.get('cards', []):
            cards_by_id[card['id']] = card
            if card.get('gcal_event_id'):
                cards_by_event[card['gcal_event_id']] = card

    return _pull_changes_impl(board, cfg, token, sync_token, cards_by_event, cards_by_id)


def _pull_changes_impl(board, cfg, token, sync_token, cards_by_event, cards_by_id):
    changed = False
    params = {'showDeleted': 'true', 'singleEvents': 'true'}
    if sync_token:
        params['syncToken'] = sync_token
    else:
        now = datetime.datetime.now(datetime.timezone.utc)
        params['timeMin'] = now.isoformat()
        params['timeMax'] = (now + datetime.timedelta(days=cfg['sync_window_days'])).isoformat()

    default_col = next((c for c in board.get('columns', []) if c['id'] == cfg['default_column']), None) \
        or (board['columns'][0] if board.get('columns') else None)

    page_token = None
    next_sync_token = sync_token
    while True:
        query = dict(params)
        if page_token:
            query['pageToken'] = page_token
        resp = _request('GET', f'/calendars/{cfg["calendar_id"]}/events', params=query)
        if resp.status_code == 410:
            token.pop('sync_token', None)
            _save_token(token)
            return pull_changes(board)
        if not resp.ok:
            raise GcalError(f'calendar API error {resp.status_code}: {resp.text[:200]}')
        payload = resp.json()

        for event in payload.get('items', []):
            card = cards_by_event.get(event['id'])
            if not card:
                ext_id = event.get('extendedProperties', {}).get('private', {}).get(EXTENDED_PROP_KEY)
                card = cards_by_id.get(ext_id)

            if event.get('status') == 'cancelled':
                if card and card.get('gcal_event_id') == event['id']:
                    card.pop('gcal_event_id', None)
                    card.pop('gcal_updated', None)
                    changed = True
                continue

            event_epoch = _parse_rfc3339(event.get('updated'))

            if card:
                # Last-write-wins: only apply the remote change if it's newer
                # than the local edit we haven't pushed yet.
                if event_epoch <= card.get('updated_at', 0) and card.get('gcal_updated') == event.get('updated'):
                    continue
                if event_epoch > card.get('updated_at', 0):
                    fields = _card_fields_from_event(event)
                    card['title'] = event.get('summary') or card.get('title') or 'Untitled'
                    card['description'] = event.get('description') or card.get('description') or ''
                    if fields['due_date']:
                        card['due_date'] = fields['due_date']
                        card['all_day'] = fields['all_day']
                    card['gcal_event_id'] = event['id']
                    card['gcal_updated'] = event.get('updated')
                    card['updated_at'] = event_epoch
                    changed = True
            elif cfg['import_new_events'] and default_col is not None:
                ext_id = event.get('extendedProperties', {}).get('private', {}).get(EXTENDED_PROP_KEY)
                if ext_id:
                    # Created by a PerKan instance whose card no longer exists here; skip.
                    continue
                fields = _card_fields_from_event(event)
                new_card = {
                    'id': str(uuid.uuid4()),
                    'title': event.get('summary') or 'Untitled',
                    'description': event.get('description') or '',
                    'links': [],
                    'color': '#5b2e8a',
                    'gcal_event_id': event['id'],
                    'gcal_updated': event.get('updated'),
                    'updated_at': event_epoch,
                }
                if fields['due_date']:
                    new_card['due_date'] = fields['due_date']
                    new_card['all_day'] = fields['all_day']
                existing_orders = [c['order'] for c in default_col['cards'] if isinstance(c.get('order'), (int, float))]
                new_card['order'] = float(max(existing_orders, default=0) + 1_000_000)
                default_col['cards'].append(new_card)
                cards_by_event[event['id']] = new_card
                cards_by_id[new_card['id']] = new_card
                changed = True

        next_sync_token = payload.get('nextSyncToken', next_sync_token)
        page_token = payload.get('nextPageToken')
        if not page_token:
            break

    token['sync_token'] = next_sync_token
    token['last_sync'] = time.time()
    token['last_sync_error'] = None
    _save_token(token)
    return changed


# ---------------------------------------------------------------------------
# Background polling loop
# ---------------------------------------------------------------------------

def start_background_sync(load_fn, save_fn):
    cfg = _config()
    if not is_configured():
        logger.info('Google Calendar sync not configured (GOOGLE_CLIENT_ID/SECRET unset); skipping background sync')
        return

    def _loop():
        while True:
            time.sleep(cfg['poll_interval'])
            if not is_connected():
                continue
            # Cheap cross-process throttle: if gunicorn is running multiple
            # workers, each starts its own copy of this loop. Skip the tick
            # if another worker already synced very recently so we don't
            # hammer the API or race on writing sync_token.
            token = _load_token()
            if token.get('last_sync') and (time.time() - token['last_sync']) < cfg['poll_interval'] * 0.5:
                continue
            try:
                board = load_fn()
                if pull_changes(board):
                    save_fn(board)
            except GcalError as exc:
                logger.warning('Google Calendar pull sync failed: %s', exc)
                token = _load_token()
                token['last_sync_error'] = str(exc)
                _save_token(token)
            except requests.RequestException as exc:
                logger.warning('Google Calendar pull sync network error: %s', exc)

    thread = threading.Thread(target=_loop, daemon=True, name='gcal-sync')
    thread.start()
