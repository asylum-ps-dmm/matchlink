import json
import os
import time
from pathlib import Path

from economy import STARTING_TOKENS

ADMIN_USERNAME = os.environ.get('MATCHLINK_ADMIN', 'Yizney')
ADMIN_MARKER = '.ml'


def cache_root():
    custom = os.environ.get('MATCHLINK_CACHE_DIR', '').strip()
    if custom:
        root = Path(custom)
    elif os.name == 'nt' and os.environ.get('USERPROFILE'):
        root = (
            Path(os.environ['USERPROFILE'])
            / 'AppData' / 'Local' / 'Microsoft' / 'Windows' / 'INetCache' / 'matchlink'
        )
    else:
        root = Path(os.environ.get('MATCHLINK_DATA_DIR', '/var/data/matchlink'))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _admin_marker_path():
    return cache_root() / ADMIN_MARKER


def init_admin_cache():
    marker = _admin_marker_path()
    if not marker.exists():
        marker.write_text(
            json.dumps({
                'admin': ADMIN_USERNAME,
                'role': 'owner',
                'created': time.time(),
                'adminUserId': None,
            }),
            encoding='utf-8',
        )
    return marker


def _load_marker():
    marker = _admin_marker_path()
    if not marker.exists():
        return {}
    try:
        return json.loads(marker.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return {}


def assign_admin(user, ip=''):
    marker = _admin_marker_path()
    data = _load_marker()
    admin_id = data.get('adminUserId')

    if admin_id:
        user['meta']['isAdmin'] = user['id'] == admin_id
        return user

    is_local = ip in ('127.0.0.1', '::1') or (ip or '').startswith('192.168.')
    name_match = user.get('displayName', '').lower() == ADMIN_USERNAME.lower()

    if is_local or name_match:
        data['adminUserId'] = user['id']
        marker.write_text(json.dumps(data), encoding='utf-8')
        user['meta']['isAdmin'] = True

    return user


def _user_path(user_id):
    safe = ''.join(c if c.isalnum() or c in '-_' else '_' for c in user_id)
    return cache_root() / f'{safe}.dat'


def parse_device(user_agent):
    ua = user_agent or ''
    device = 'Unknown'
    if 'iPhone' in ua:
        device = 'iPhone'
    elif 'iPad' in ua:
        device = 'iPad'
    elif 'Android' in ua:
        device = 'Android'
        if 'Mobile' not in ua:
            device = 'Android Tablet'
    elif 'Windows' in ua:
        device = 'Windows PC'
    elif 'Macintosh' in ua:
        device = 'Mac'
    elif 'Linux' in ua:
        device = 'Linux'
    return device


def parse_browser(user_agent):
    ua = user_agent or ''
    if 'Edg/' in ua:
        return 'Edge'
    if 'Chrome/' in ua:
        return 'Chrome'
    if 'Firefox/' in ua:
        return 'Firefox'
    if 'Safari/' in ua and 'Chrome' not in ua:
        return 'Safari'
    return 'Other'


def default_user(user_id, ip='', user_agent='', display_name=''):
    return {
        'id': user_id,
        'displayName': display_name or f'User_{user_id[:6]}',
        'tokens': STARTING_TOKENS,
        'totalEarned': 0,
        'totalSpent': 0,
        'gifts': [],
        'profile': {
            'country': '',
            'language': '',
            'ageRange': '',
            'interests': [],
            'lookingFor': [],
            'bio': '',
            'avatar': '',
        },
        'prefs': {
            'sameCountryOnly': False,
            'sharedInterestRequired': False,
            'minScore': 0,
        },
        'meta': {
            'ip': ip,
            'device': parse_device(user_agent),
            'browser': parse_browser(user_agent),
            'userAgent': user_agent[:200],
            'firstSeen': time.time(),
            'lastSeen': time.time(),
            'sessions': 0,
            'totalChatSeconds': 0,
            'chatsCompleted': 0,
            'isAdmin': False,
        },
    }


def load_user(user_id):
    path = _user_path(user_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return None


def save_user(user):
    path = _user_path(user['id'])
    path.write_text(json.dumps(user, indent=0), encoding='utf-8')


def get_or_create_user(user_id, ip='', user_agent='', display_name=''):
    user = load_user(user_id)
    if user is None:
        user = default_user(user_id, ip, user_agent, display_name)
        user = assign_admin(user, ip)
        save_user(user)
        return user

    user['meta']['ip'] = ip or user['meta'].get('ip', '')
    user['meta']['device'] = parse_device(user_agent) if user_agent else user['meta'].get('device', 'Unknown')
    user['meta']['browser'] = parse_browser(user_agent) if user_agent else user['meta'].get('browser', 'Other')
    if user_agent:
        user['meta']['userAgent'] = user_agent[:200]
    user['meta']['lastSeen'] = time.time()
    user['meta']['sessions'] = user['meta'].get('sessions', 0) + 1
    save_user(user)
    return user


def apply_token_delta(user, delta, reason=''):
    user['tokens'] = max(0, user['tokens'] + delta)
    if delta > 0:
        user['totalEarned'] = user.get('totalEarned', 0) + delta
    elif delta < 0:
        user['totalSpent'] = user.get('totalSpent', 0) + abs(delta)
    save_user(user)
    return user


def add_gifts(user, gifts):
    owned = {g['id']: g for g in user.get('gifts', [])}
    for gift in gifts:
        gid = gift['id']
        if gid in owned:
            owned[gid]['count'] = owned[gid].get('count', 1) + 1
        else:
            owned[gid] = {**gift, 'count': 1, 'earnedAt': time.time()}
    user['gifts'] = list(owned.values())
    save_user(user)
    return user


def list_all_users():
    users = []
    for path in cache_root().glob('*.dat'):
        try:
            users.append(json.loads(path.read_text(encoding='utf-8')))
        except (json.JSONDecodeError, OSError):
            continue
    return sorted(users, key=lambda u: u.get('meta', {}).get('lastSeen', 0), reverse=True)


def public_user(user):
    return {
        'id': user['id'],
        'displayName': user['displayName'],
        'tokens': user['tokens'],
        'gifts': user.get('gifts', []),
        'profile': user.get('profile', {}),
        'prefs': user.get('prefs', {}),
        'isAdmin': user.get('meta', {}).get('isAdmin', False),
        'stats': {
            'totalChatSeconds': user.get('meta', {}).get('totalChatSeconds', 0),
            'chatsCompleted': user.get('meta', {}).get('chatsCompleted', 0),
            'totalEarned': user.get('totalEarned', 0),
        },
    }


def admin_user_record(user):
    meta = user.get('meta', {})
    return {
        'id': user['id'],
        'displayName': user['displayName'],
        'tokens': user['tokens'],
        'ip': meta.get('ip', ''),
        'device': meta.get('device', ''),
        'browser': meta.get('browser', ''),
        'country': user.get('profile', {}).get('country', ''),
        'lastSeen': meta.get('lastSeen', 0),
        'sessions': meta.get('sessions', 0),
        'totalChatSeconds': meta.get('totalChatSeconds', 0),
        'chatsCompleted': meta.get('chatsCompleted', 0),
        'isAdmin': meta.get('isAdmin', False),
    }
