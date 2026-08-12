"""Local user accounts for PerKan.

A lightweight username/password store backed by a JSON file (same
pattern as kanban.json), used to gate the board behind a login and to
give each person their own Google Calendar connection. There is no
notion of roles/admins: any logged-in user can manage any other user,
matching the trusted-small-team spirit of the rest of the app.
"""
import os
import json
import re
import time
import uuid
import threading

from werkzeug.security import generate_password_hash, check_password_hash

DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
USERS_FILE = os.path.join(DATA_DIR, 'users.json')
# Reentrant: create/update/delete hold the lock while calling _save(),
# which also acquires it.
_lock = threading.RLock()

USERNAME_RE = re.compile(r'^[a-zA-Z0-9_.-]{2,32}$')


def _ensure_data_dir():
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)


def _load():
    _ensure_data_dir()
    if not os.path.exists(USERS_FILE):
        return []
    try:
        with open(USERS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(users):
    _ensure_data_dir()
    with _lock:
        tmp = USERS_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(users, f, indent=2)
        os.replace(tmp, USERS_FILE)


def _public(user):
    return {
        'id': user['id'],
        'username': user['username'],
        'display_name': user.get('display_name') or user['username'],
        'created_at': user.get('created_at'),
    }


class UserError(Exception):
    pass


def count_users():
    return len(_load())


def list_users():
    return [_public(u) for u in _load()]


def get_user(user_id):
    if not user_id:
        return None
    for u in _load():
        if u['id'] == user_id:
            return _public(u)
    return None


def _find_by_username(users, username):
    username = (username or '').strip().lower()
    return next((u for u in users if u['username'].lower() == username), None)


def create_user(username, password, display_name=None):
    username = (username or '').strip()
    if not USERNAME_RE.match(username or ''):
        raise UserError('username must be 2-32 characters: letters, numbers, "_", "." or "-"')
    if not password or len(password) < 8:
        raise UserError('password must be at least 8 characters')

    with _lock:
        users = _load()
        if _find_by_username(users, username):
            raise UserError('username already taken')
        user = {
            'id': str(uuid.uuid4()),
            'username': username,
            'display_name': (display_name or '').strip() or username,
            'password_hash': generate_password_hash(password),
            'created_at': time.time(),
        }
        users.append(user)
        _save(users)
    return _public(user)


def verify_login(username, password):
    users = _load()
    user = _find_by_username(users, username)
    if not user:
        return None
    if not check_password_hash(user['password_hash'], password or ''):
        return None
    return _public(user)


def update_user(user_id, display_name=None, password=None):
    with _lock:
        users = _load()
        user = next((u for u in users if u['id'] == user_id), None)
        if not user:
            raise UserError('user not found')
        if display_name is not None:
            display_name = display_name.strip()
            user['display_name'] = display_name or user['username']
        if password is not None:
            if len(password) < 8:
                raise UserError('password must be at least 8 characters')
            user['password_hash'] = generate_password_hash(password)
        _save(users)
        return _public(user)


def delete_user(user_id):
    with _lock:
        users = _load()
        remaining = [u for u in users if u['id'] != user_id]
        if len(remaining) == len(users):
            return False
        _save(remaining)
        return True
