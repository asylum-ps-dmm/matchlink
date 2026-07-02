import os
import socket as net_socket
import time

from flask import Flask, jsonify, request, send_from_directory
from flask_socketio import SocketIO, emit, leave_room

import cache_manager as cache
from economy import GIFTS, settle_chat

app = Flask(__name__, static_folder='public')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'matchlink-dev')
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='threading')

PORT = int(os.environ.get('PORT', 3000))
MATCH_INTERVAL_SEC = 1.0
DOMAIN = os.environ.get('MATCHLINK_DOMAIN', '')

waiting_queue = {}
connected_users = {}
blocked_pairs = set()
active_chats = {}
sid_to_user = {}


def get_local_ip():
    s = net_socket.socket(net_socket.AF_INET, net_socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except OSError:
        return '127.0.0.1'
    finally:
        s.close()


def client_ip():
    forwarded = request.headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.remote_addr or ''


def normalize_tags(value):
    if not value:
        return []
    if isinstance(value, list):
        items = value
    else:
        items = str(value).split(',')
    return [i.strip().lower() for i in items if i.strip()]


def get_prefs(data):
    return {
        'sameCountryOnly': bool(data.get('sameCountryOnly')),
        'sharedInterestRequired': bool(data.get('sharedInterestRequired')),
        'minScore': int(data.get('minScore') or 0),
    }


def shared_tags(a_tags, b_tags):
    return sorted(set(a_tags) & set(b_tags))


def score_pair(a, b):
    score = 0
    breakdown = []
    if a.get('country') and b.get('country') and a['country'] == b['country']:
        score += 40
        breakdown.append('same country')
    if a.get('language') and b.get('language') and a['language'] == b['language']:
        score += 25
        breakdown.append('same language')
    overlap = shared_tags(a.get('interests', []), b.get('interests', []))
    if overlap:
        score += len(overlap) * 15
        breakdown.append(f'{len(overlap)} shared interest(s)')
    if a.get('ageRange') and b.get('ageRange') and a['ageRange'] == b['ageRange']:
        score += 10
        breakdown.append('same age range')
    looking_overlap = shared_tags(a.get('lookingFor', []), b.get('lookingFor', []))
    if looking_overlap:
        score += len(looking_overlap) * 5
        breakdown.append(f'{len(looking_overlap)} shared goal(s)')
    return score, breakdown, overlap, looking_overlap


def pair_allowed(user_a, user_b):
    return tuple(sorted([user_a, user_b])) not in blocked_pairs


def passes_filters(profile_a, prefs_a, profile_b, prefs_b):
    if prefs_a['sameCountryOnly'] or prefs_b['sameCountryOnly']:
        if not profile_a.get('country') or not profile_b.get('country'):
            return False
        if profile_a['country'] != profile_b['country']:
            return False
    if prefs_a['sharedInterestRequired'] or prefs_b['sharedInterestRequired']:
        if not shared_tags(profile_a.get('interests', []), profile_b.get('interests', [])):
            return False
    return True


def find_best_match(user_id, entry):
    profile = entry['profile']
    prefs = entry['prefs']
    best = None
    best_score = -1
    for other_id, other in waiting_queue.items():
        if other_id == user_id:
            continue
        if not pair_allowed(user_id, other_id):
            continue
        if not passes_filters(profile, prefs, other['profile'], other['prefs']):
            continue
        forward, _, _, _ = score_pair(profile, other['profile'])
        reverse, _, _, _ = score_pair(other['profile'], profile)
        combined = forward + reverse
        min_required = max(prefs['minScore'], other['prefs']['minScore'])
        if combined < min_required:
            continue
        if combined > best_score:
            best_score = combined
            best = {'id': other_id, 'score': combined}
    return best


def build_match_payload(my_profile, partner_profile, score):
    _, breakdown, shared_interests, shared_goals = score_pair(my_profile, partner_profile)
    return {
        'country': partner_profile.get('country') or 'Any',
        'language': partner_profile.get('language') or 'Any',
        'interests': partner_profile.get('interests', []),
        'ageRange': partner_profile.get('ageRange') or 'Any',
        'lookingFor': partner_profile.get('lookingFor', []),
        'sharedInterests': shared_interests,
        'sharedGoals': shared_goals,
        'matchReasons': breakdown,
    }


def start_chat_session(room_id, user_a_sid, user_b_sid):
    active_chats[room_id] = {
        'startedAt': time.time(),
        'users': {user_a_sid: user_b_sid, user_b_sid: user_a_sid},
    }


def end_chat_session(room_id, leaver_sid, exit_type='normal'):
    chat = active_chats.pop(room_id, None)
    if not chat:
        return None

    duration = time.time() - chat['startedAt']
    results = {}

    for sid in chat['users']:
        uid = sid_to_user.get(sid)
        if not uid:
            continue
        user = cache.load_user(uid)
        if not user:
            continue

        etype = exit_type
        if sid == leaver_sid:
            if exit_type == 'decline':
                etype = 'decline'
            elif duration < 60:
                etype = 'early_hangup'
            elif exit_type in ('skip', 'stop', 'mid_exit'):
                etype = 'mid_exit'
        else:
            etype = 'partner_left'

        settlement = settle_chat(duration, etype)
        cache.apply_token_delta(user, settlement['tokensDelta'])
        if settlement['giftsEarned']:
            cache.add_gifts(user, settlement['giftsEarned'])
        user['meta']['totalChatSeconds'] = user['meta'].get('totalChatSeconds', 0) + int(duration)
        if settlement['tokensEarned'] > 0:
            user['meta']['chatsCompleted'] = user['meta'].get('chatsCompleted', 0) + 1
        cache.save_user(user)
        results[sid] = {**settlement, 'tokens': user['tokens'], 'gifts': user.get('gifts', [])}

    return results


def pair_users(user_a_id, user_b_id, score):
    entry_a = waiting_queue.pop(user_a_id, None)
    entry_b = waiting_queue.pop(user_b_id, None)
    if not entry_a or not entry_b:
        return

    room_id = f'room-{user_a_id}-{user_b_id}'
    socketio.server.enter_room(user_a_id, room_id)
    socketio.server.enter_room(user_b_id, room_id)
    start_chat_session(room_id, user_a_id, user_b_id)

    socketio.emit('matched', {
        'roomId': room_id,
        'partner': build_match_payload(entry_a['profile'], entry_b['profile'], score),
        'matchScore': score,
        'isInitiator': True,
    }, room=user_a_id)

    socketio.emit('matched', {
        'roomId': room_id,
        'partner': build_match_payload(entry_b['profile'], entry_a['profile'], score),
        'matchScore': score,
        'isInitiator': False,
    }, room=user_b_id)
    broadcast_stats()


def try_match_all():
    matched = set()
    for user_id in list(waiting_queue.keys()):
        if user_id in matched:
            continue
        entry = waiting_queue[user_id]
        best = find_best_match(user_id, entry)
        if not best or best['id'] in matched:
            continue
        matched.add(user_id)
        matched.add(best['id'])
        pair_users(user_id, best['id'], best['score'])


def broadcast_stats():
    socketio.emit('network-stats', {
        'online': len(connected_users),
        'waiting': len(waiting_queue),
    })


def emit_settlement(sid, settlement):
    if settlement:
        socketio.emit('chat-settled', settlement, room=sid)


@app.route('/')
def index():
    return send_from_directory('public', 'index.html')


@app.route('/api/info')
def api_info():
    return jsonify({
        'port': PORT,
        'localUrl': f'http://{get_local_ip()}:{PORT}',
        'domain': DOMAIN or None,
        'online': len(connected_users),
        'waiting': len(waiting_queue),
        'gifts': GIFTS,
    })


@app.route('/api/user/<user_id>')
def api_get_user(user_id):
    user = cache.load_user(user_id)
    if not user:
        return jsonify({'error': 'not found'}), 404
    return jsonify(cache.public_user(user))


@app.route('/api/user/<user_id>/profile', methods=['PUT', 'POST'])
def api_update_profile(user_id):
    data = request.get_json() or {}
    user = cache.get_or_create_user(user_id, client_ip(), request.headers.get('User-Agent', ''))
    profile = user.setdefault('profile', {})
    if 'displayName' in data:
        user['displayName'] = str(data['displayName'])[:32]
    for key in ('country', 'language', 'ageRange', 'bio', 'avatar'):
        if key in data:
            profile[key] = str(data[key])[:200]
    if 'interests' in data:
        profile['interests'] = normalize_tags(data['interests'])
    if 'lookingFor' in data:
        profile['lookingFor'] = normalize_tags(data['lookingFor'])
    if 'prefs' in data:
        user['prefs'] = {**user.get('prefs', {}), **get_prefs(data['prefs'])}
    cache.save_user(user)
    return jsonify(cache.public_user(user))


@app.route('/api/admin/users')
def api_admin_users():
    admin_id = request.args.get('adminId', '')
    admin = cache.load_user(admin_id)
    if not admin or not admin.get('meta', {}).get('isAdmin'):
        return jsonify({'error': 'unauthorized'}), 403
    return jsonify([cache.admin_user_record(u) for u in cache.list_all_users()])


@app.route('/<path:path>')
def static_files(path):
    return send_from_directory('public', path)


@socketio.on('connect')
def on_connect():
    connected_users[request.sid] = time.time()
    broadcast_stats()


@socketio.on('register')
def on_register(data):
    data = data or {}
    user_id = data.get('userId', '')
    if not user_id:
        return emit('error', {'message': 'userId required'})

    user = cache.get_or_create_user(
        user_id,
        client_ip(),
        request.headers.get('User-Agent', ''),
        data.get('displayName', ''),
    )
    sid_to_user[request.sid] = user_id
    emit('registered', cache.public_user(user))


@socketio.on('join-queue')
def on_join_queue(data):
    data = data or {}
    uid = sid_to_user.get(request.sid)
    user = cache.load_user(uid) if uid else None
    profile_data = user['profile'] if user else data

    waiting_queue[request.sid] = {
        'profile': {
            'country': (profile_data.get('country') or data.get('country') or '').strip(),
            'language': (profile_data.get('language') or data.get('language') or '').strip(),
            'interests': normalize_tags(profile_data.get('interests') or data.get('interests')),
            'ageRange': (profile_data.get('ageRange') or data.get('ageRange') or '').strip(),
            'lookingFor': normalize_tags(profile_data.get('lookingFor') or data.get('lookingFor')),
        },
        'prefs': user.get('prefs', get_prefs(data)) if user else get_prefs(data),
    }
    emit('searching', {'queueSize': len(waiting_queue)})
    broadcast_stats()


@socketio.on('leave-queue')
def on_leave_queue():
    waiting_queue.pop(request.sid, None)
    broadcast_stats()


@socketio.on('webrtc-offer')
def on_offer(data):
    socketio.emit('webrtc-offer', {'offer': data.get('offer'), 'from': request.sid},
                  room=data.get('roomId'), include_self=False)


@socketio.on('webrtc-answer')
def on_answer(data):
    socketio.emit('webrtc-answer', {'answer': data.get('answer'), 'from': request.sid},
                  room=data.get('roomId'), include_self=False)


@socketio.on('webrtc-ice')
def on_ice(data):
    socketio.emit('webrtc-ice', {'candidate': data.get('candidate'), 'from': request.sid},
                  room=data.get('roomId'), include_self=False)


@socketio.on('chat-message')
def on_chat(data):
    socketio.emit('chat-message', {'message': data.get('message'), 'from': request.sid},
                  room=data.get('roomId'), include_self=False)


@socketio.on('end-chat')
def on_end_chat(data):
    room_id = data.get('roomId')
    exit_type = data.get('exitType', 'mid_exit')
    results = end_chat_session(room_id, request.sid, exit_type)
    if results:
        for sid, settlement in results.items():
            emit_settlement(sid, settlement)
    socketio.emit('partner-ended', room=room_id, include_self=False)
    leave_room(room_id)
    broadcast_stats()


@socketio.on('skip')
def on_skip(data):
    room_id = data.get('roomId')
    partner_sid = data.get('partnerSid')
    if partner_sid:
        blocked_pairs.add(tuple(sorted([request.sid, partner_sid])))
    results = end_chat_session(room_id, request.sid, 'decline' if data.get('decline') else 'skip')
    if results:
        for sid, settlement in results.items():
            emit_settlement(sid, settlement)
    socketio.emit('partner-skipped', room=room_id, include_self=False)
    leave_room(room_id)
    emit('searching', {'queueSize': len(waiting_queue)})
    broadcast_stats()


@socketio.on('disconnect')
def on_disconnect():
    waiting_queue.pop(request.sid, None)
    connected_users.pop(request.sid, None)
    for room_id, chat in list(active_chats.items()):
        if request.sid in chat['users']:
            results = end_chat_session(room_id, request.sid, 'early_hangup')
            if results:
                for sid, settlement in results.items():
                    emit_settlement(sid, settlement)
            break
    sid_to_user.pop(request.sid, None)
    broadcast_stats()


def _match_loop():
    while True:
        socketio.sleep(MATCH_INTERVAL_SEC)
        try_match_all()


if __name__ == '__main__':
    cache.init_admin_cache()
    local_ip = get_local_ip()
    socketio.start_background_task(_match_loop)
    print(f'MatchLink running on port {PORT}')
    if DOMAIN:
        print(f'Public URL: {DOMAIN}')
    else:
        print(f'Local: http://localhost:{PORT}')
        print(f'LAN: http://{local_ip}:{PORT}')
    print(f'Admin cache: {cache.cache_root()}')
    socketio.run(app, host='0.0.0.0', port=PORT, debug=False, allow_unsafe_werkzeug=True)
